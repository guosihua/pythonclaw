"""
Agent — 核心推理循环

核心职责
----------------
  - 维护对话历史 (messages list)
  - 构建会话工具集并调度工具调用
  - 三层渐进式技能加载（目录→指令→资源）
  - 触发上下文压缩（自动或手动）
  - 与内存管理器（MemoryManager）和知识库（KnowledgeRAG）交互

此类不负责的职责
---------------------------------------
  - 会话生命周期管理 (→ SessionManager)
  - 跨重启持久化 (→ PersistentAgent subclass)
  - I/O通道 (→ channels/)
  - 调度器 (→ scheduler/)
"""

from __future__ import annotations

import json
import logging
import os
import time
import httpx
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime

from .. import config
from .compaction import (
    DEFAULT_AUTO_THRESHOLD_TOKENS,
    DEFAULT_RECENT_KEEP,
    estimate_tokens,
)
from .compaction import (
    compact as _do_compact,
)
from .knowledge.rag import KnowledgeRAG
from .llm.base import LLMProvider
from .memory.manager import MemoryManager
from .skill_loader import SkillRegistry
from .tools import (
    AVAILABLE_TOOLS,
    CRON_TOOLS,
    KNOWLEDGE_TOOL,
    MEMORY_TOOLS,
    META_SKILL_TOOLS,
    PRIMITIVE_TOOLS,
    SKILL_TOOLS,
    WEB_SEARCH_TOOL,
    configure_venv,
    set_sandbox,
)

logger = logging.getLogger(__name__)


def _load_text_dir_or_file(path: str | None, label: str = "File") -> str:
    """
    功能 ：加载单个文件或目录下所有 .md / .txt 文件的内容。
    - 如果路径为 None 或不存在，返回空字符串
    - 如果是文件，直接读取
    - 如果是目录，遍历所有 .md / .txt 文件并拼接内容，每个文件之间用空行隔开
    """
    if not path or not os.path.exists(path):
        return ""
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    if os.path.isdir(path):
        parts = []
        for filename in sorted(os.listdir(path)):
            if filename.lower().endswith((".md", ".txt")):
                with open(os.path.join(path, filename), "r", encoding="utf-8") as f:
                    parts.append(f"\n\n--- {label}: {filename} ---\n" + f.read())
        return "".join(parts)
    return ""

"""
三个函数用于将交互详情记录到 JSONL 文件中：
- _detail_log_dir() ：返回日志目录路径
- _detail_log_file() ：返回日志文件路径
- _log_detail() ：将条目追加到日志文件（带时间戳）
"""
def _detail_log_dir() -> str:
    from .. import config as _cfg
    return os.path.join(str(_cfg.PYTHONCLAW_HOME), "context", "logs")


def _detail_log_file() -> str:
    return os.path.join(_detail_log_dir(), "history_detail.jsonl")


def _log_detail(entry: dict) -> None:
    """
    功能 ：将 JSON 行追加到详细交互日志文件中。
    """
    try:
        log_dir = _detail_log_dir()
        os.makedirs(log_dir, exist_ok=True)
        entry["ts"] = datetime.now().isoformat(timespec="milliseconds")
        with open(_detail_log_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def check_intent(user_input: str) -> str | None:
    """
    POST 请求意图识别 API，返回识别到的 intent 分类。
    请求地址: 通过配置项 intent.url 设置，默认 http://ip:8852/chatbot/intent/bert
    请求体: {"queryList": [user_input]}
    响应: {"code":1,"data":[{"intent":"...","type":""}]}
    支持环境变量: INTENT_API_URL
    """
    url = config.get("intent", "url")
    if not url:
        return None

    try:
        resp = httpx.post(
            url,
            json={"queryList": [user_input]},
            timeout=5.0,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") == 1 and body.get("data"):
            intent = body["data"][0].get("intent")
            if intent:
                logger.info("Intent identified: %s", intent)
                return intent
    except Exception as exc:
        logger.error("Intent recognition failed (non-fatal): %s", exc)
    return None


def search_manuals(user_input: str) -> str | None:
    """
    POST 请求手册搜索 API，返回拼接后的手册资料文本。
    请求地址: 通过配置项 manual.url 设置，默认 http://ip:8858/cloud/search/claw
    请求体: {"queryList": [user_input]}
    响应: {"code":1,"data":[{"titleTree":"...","context":"...","url":"...","manualName":"..."}]}
    """
    url = config.get("manual", "url")
    if not url:
        return None

    try:
        resp = httpx.post(
            url,
            json={"queryList": [user_input]},
            timeout=10.0,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") == 1 and body.get("data"):
            groups = [[], [], []]
            for item in body["data"]:
                item_url = item.get("url", "")
                context = item.get("context", "")
                if not item_url:
                    title_tree = item.get("titleTree", "")
                    groups[0].append(f"{title_tree}\n{context}")
                elif "zhiliao.h3c.com" in item_url:
                    title_tree = item.get("titleTree", "")
                    groups[1].append(f"{title_tree}\n{context}")
                elif "www.h3c.com" in item_url:
                    manual_name = item.get("manualName", "")
                    groups[2].append(f"{manual_name}\n{context}")

            parts = []
            max_len = 20000
            current_len = 0
            i = 0
            while any(i < len(g) for g in groups):
                for g in groups:
                    if i < len(g):
                        next_part = g[i]
                        sep = "\n\n" if parts else ""
                        if current_len + len(sep) + len(next_part) > max_len:
                            break
                        parts.append(next_part)
                        current_len += len(sep) + len(next_part)
                else:
                    i += 1
                    continue
                break

            result = "\n\n".join(parts)
            if result.strip():
                logger.info("Manual search result: %s", result[:30])
                return result
    except Exception as exc:
        logger.error("Manual search failed (non-fatal): %s", exc)
    return None


class Agent:
    """
    Agent 类是有状态的 LLM 代理，支持工具使用、三层技能加载、记忆和上下文压缩。

    Parameters
    ----------
    provider           : LLM 后端（DeepSeek、Grok、Claude、Gemini 等）
    session_id         : 会话标识符（启用分组上下文隔离）
    memory_dir         : 内存目录路径（自动检测）
    skills_dirs        : 技能目录路径列表（目录→指令→资源）
    knowledge_path     : 知识库目录路径（用于 RAG）
    persona_path       : 人物文件路径或目录路径
    soul_path          : SO.md 文件路径或目录路径
    verbose            : 是否打印调试信息
    show_full_context  : 是否在每次 LLM 调用前打印完整上下文窗口
    max_chat_history   : 最大对话历史记录数
    auto_compaction    : 是否自动触发上下文压缩
    compaction_threshold : 自动压缩阈值
    compaction_recent_keep : 压缩后保留的最近消息数
    cron_manager       : Cron 定理器（用于定时任务）（可选）
    """

    MAX_TOOL_ROUNDS = 12
    MAX_PARALLEL_SKILLS = 5
    TOOL_TIMEOUT = 300

    def __init__(
        self,
        provider: LLMProvider,
        session_id: str | None = None,
        memory_dir: str | None = None,
        skills_dirs: list[str] | None = None,
        knowledge_path: str | None = None,
        persona_path: str | None = None,
        soul_path: str | None = None,
        tools_path: str | None = None,
        verbose: bool = False,
        show_full_context: bool = False,
        max_chat_history: int = 10,
        auto_compaction: bool = True,
        compaction_threshold: int = DEFAULT_AUTO_THRESHOLD_TOKENS,
        compaction_recent_keep: int = DEFAULT_RECENT_KEEP,
        cron_manager=None,
    ) -> None:
        if memory_dir is None and skills_dirs is None and knowledge_path is None and persona_path is None:
            from .. import config as _cfg
            home = str(_cfg.PYTHONCLAW_HOME)
            context_dir = os.path.join(home, "context")
            if not os.path.exists(context_dir):
                if verbose:
                    print(f"[Agent] Context not found. Initialising default context in {context_dir}...")
                try:
                    from ..init import init
                    init(home)
                except ImportError:
                    try:
                        from pythonclaw.init import init
                        init(home)
                    except ImportError:
                        print("[Agent] Warning: Could not auto-initialise context.")
            if verbose:
                print(f"[Agent] Using default context at {context_dir}")

            # Per-group isolation: each session gets its own memory directory
            if session_id and _cfg.per_group_isolation():
                group_dir = str(_cfg.group_context_dir(session_id))
                os.makedirs(os.path.join(group_dir, "memory"), exist_ok=True)
                memory_dir = os.path.join(group_dir, "memory")
                if verbose:
                    print(f"[Agent] Per-group memory: {memory_dir}")
            else:
                memory_dir = os.path.join(context_dir, "memory")

            knowledge_path = os.path.join(context_dir, "knowledge")
            skills_dirs = [os.path.join(context_dir, "skills")]
            persona_path = os.path.join(context_dir, "persona")
            if soul_path is None:
                soul_path = os.path.join(context_dir, "soul")
            if tools_path is None:
                tools_path = os.path.join(context_dir, "tools")

        # Sandbox: restrict file-write tools to the home directory
        sandbox_root = str(config.PYTHONCLAW_HOME)
        set_sandbox([sandbox_root, os.path.expanduser("~")])
        if verbose:
            print(f"[Agent] Sandbox root: {sandbox_root}")

        # Venv: ensure all subprocesses use the project's virtual environment
        venv_path = configure_venv()
        if verbose and venv_path:
            print(f"[Agent] Virtual env: {venv_path}")

        self.provider = provider
        self.session_id = session_id
        self.messages: list[dict] = []
        self.verbose = verbose
        self.show_full_context = show_full_context
        self.max_chat_history = max_chat_history
        self.auto_compaction = auto_compaction
        self.compaction_threshold = compaction_threshold
        self.compaction_recent_keep = compaction_recent_keep
        self.compaction_count: int = 0
        self._cron_manager = cron_manager

        self.loaded_skill_names: set[str] = set()
        self.pending_injections: list[str] = []
        self.MAX_PARALLEL_SKILLS = config.get_int(
            "agent", "maxParallelSkills", default=5,
        )
        self._bg_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent-bg")

        # Memory — with optional global fallback for per-group isolation
        mem_dir = memory_dir or config.get("memory", "dir", env="PYTHONCLAW_MEMORY_DIR")
        global_mem_dir: str | None = None
        if session_id and config.per_group_isolation():
            global_mem_dir = os.path.join(str(config.PYTHONCLAW_HOME), "context", "memory")
        self.memory = MemoryManager(mem_dir, global_memory_dir=global_mem_dir)

        # Knowledge RAG (hybrid retrieval)
        self.rag: KnowledgeRAG | None = None
        if knowledge_path and os.path.exists(knowledge_path):
            self.rag = KnowledgeRAG(
                knowledge_dir=knowledge_path,
                provider=provider,
                use_reranker=True,
            )
            if verbose:
                print(f"[Agent] KnowledgeRAG: '{knowledge_path}' ({len(self.rag)} chunks)")

        # Web search (Tavily)
        self._web_search_enabled = bool(
            config.get("tavily", "apiKey", env="TAVILY_API_KEY")
        )
        if verbose and self._web_search_enabled:
            print("[Agent] Web search enabled (Tavily)")

        # Skills — always include the built-in templates + user context/skills
        self.skills_dirs: list[str] = []
        pkg_templates = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "templates", "skills",
        )
        if os.path.isdir(pkg_templates):
            self.skills_dirs.append(pkg_templates)
        if skills_dirs:
            for d in ([skills_dirs] if isinstance(skills_dirs, str) else skills_dirs):
                if d not in self.skills_dirs:
                    self.skills_dirs.append(d)

        # Identity layers
        self.soul_instruction = _load_text_dir_or_file(soul_path, label="Soul")
        self.persona_instruction = _load_text_dir_or_file(persona_path, label="Persona")
        self.tools_notes = _load_text_dir_or_file(tools_path, label="Tools")

        # Detect if the user has set up their own soul/persona (not template defaults)
        self._needs_onboarding = not self._has_user_identity(soul_path, persona_path)

        if verbose and self.soul_instruction:
            print(f"[Agent] Soul loaded ({len(self.soul_instruction)} chars)")
        if verbose and self.persona_instruction:
            print(f"[Agent] Persona loaded ({len(self.persona_instruction)} chars)")
        if verbose and self.tools_notes:
            print(f"[Agent] TOOLS.md loaded ({len(self.tools_notes)} chars)")
        if verbose and self._needs_onboarding:
            print("[Agent] No user identity found — onboarding will be triggered")

        self._init_system_prompt()

    @staticmethod
    def _has_user_identity(soul_path: str | None, persona_path: str | None) -> bool:
        """Return True if the user has customized soul or persona files."""
        for p in (soul_path, persona_path):
            if p is None:
                continue
            if os.path.isdir(p):
                for fname in os.listdir(p):
                    fpath = os.path.join(p, fname)
                    if os.path.isfile(fpath) and os.path.getsize(fpath) > 0:
                        return True
            elif os.path.isfile(p) and os.path.getsize(p) > 0:
                return True
        return False

    # ── Initialisation ────────────────────────────────────────────────────────

    def _init_system_prompt(self) -> None:
        """
        Build the initial system message with three-tier skill loading.

        Level 1 (Metadata) is injected here — the full skill catalog
        (name + description for every installed skill).  This lets the
        LLM decide when to activate a skill without any discovery calls.
        """
        self._registry = SkillRegistry(skills_dirs=self.skills_dirs)
        skill_catalog = self._registry.build_catalog()

        soul_section = f"\n\n## Core Identity (Soul)\n{self.soul_instruction}" if self.soul_instruction else ""
        persona_section = f"\n\n## Role & Persona\n{self.persona_instruction}" if self.persona_instruction else ""
        tools_section = f"\n\n## Local Notes (TOOLS.md)\n{self.tools_notes}" if self.tools_notes else ""

        web_search_section = ""
        if self._web_search_enabled:
            web_search_section = """
3. **Web Search**: `web_search` (powered by Tavily)
   Search the web for real-time information when you need up-to-date data,
   current events, facts you're unsure about, or technical documentation.
   Supports topic filters (general/news/finance) and time range filters."""

        bot_name = ""
        try:
            if hasattr(self, "memory"):
                bn = self.memory.list_all().get("bot_name", "")
                if bn:
                    bot_name = f' Your name is "{bn}".'
        except Exception:
            pass

        system_msg = f"""You are a H3C AI助手吱吱 — an autonomous AI assistant.{bot_name}{soul_section}{persona_section}{tools_section}

### Tools
- **Primitives**: `run_command`, `read_file`, `write_file`, `list_files`
- **Skills** — call `use_skill(name)` to activate. Catalog:
{skill_catalog}
- **Memory**: `remember(key,val)`, `recall(query)`, `memory_get(path)`, `memory_list_files()`, `forget(key)`, `update_index(content)`
- **Skill creation**: `create_skill` — create generic reusable skills when none fit{web_search_section}

### Task Execution Modes
Choose your approach based on task complexity:

**ReAct** (simple tasks, 1-2 steps): Act directly — call tools and respond immediately.

**Plan & Execute** (complex tasks, 3+ steps, research, multi-source analysis):
1. Output a short numbered plan (3-6 steps) as your first response
2. Execute each step using tools — call multiple tools in parallel when steps are independent (up to {self.MAX_PARALLEL_SKILLS} parallel skills)
3. After each step, briefly summarize what you found before moving on
4. After all steps, synthesize a concise final answer

You decide which mode fits. Don't announce the mode name.

### Rules
- Batch independent tool calls in one response (parallel execution).
- Minimize search rounds (1-3 max). Combine queries. Don't repeat.
- Proactively `remember` user preferences, decisions, key facts.
- Use `recall` when user references past context.
- Memory auto-loaded at session start. INDEX.md = curated system info.
- All downloaded/generated files go in the shared files directory (`~/.pythonclaw/context/files/`). The `run_command` tool uses this as its working directory.
- NEVER output tool calls as XML or text. Always use the function calling API.

### Response Guidelines
- **Language matching**: ALWAYS reply in the SAME language the user used in their message. If the user writes in Chinese, reply in Chinese. If in English, reply in English. Mirror the user's language exactly.
- Answer the user's question directly and concisely.
- Keep responses focused — under 300 words when possible. Break long answers into short paragraphs.
- Do NOT mention what skills or tools you have available, unless explicitly asked.
- Do NOT list other things you can do at the end of your response.
"""
        # ── Auto-inject memory context ────────────────────────────────────
        boot_mem = self.memory.boot_context(max_chars=3000)
        if boot_mem:
            system_msg += f"\n\n## Loaded Memory (auto-injected at session start)\n{boot_mem}\n"

        if getattr(self, "_needs_onboarding", False):
            system_msg += """
### First-Time Onboarding
**IMPORTANT**: No user identity (soul/persona) has been configured yet.
On the VERY FIRST user message, start a friendly onboarding conversation.

**Language rule**: Always conduct onboarding in **English** by default.
If the user replies in another language, switch to that language for
the rest of the onboarding (and set that as their language preference).

1. Greet the user warmly and introduce yourself as PythonClaw
2. Ask: "What would you like to name me?" (let the user give you a custom name)
3. Ask: "What should I call you?" (wait for response)
4. Ask: "What kind of personality would you like me to have? (e.g. professional, friendly, humorous, encouraging)"
5. Ask: "What area would you like me to focus on? (e.g. software development, finance, research, daily assistant)"

After collecting ALL answers, use the `onboarding` skill to write the
soul.md and persona.md files. Detect the user's language from their
replies (default to English if they replied in English) and pass it as
the `--language` argument. Then use `remember` to save:
- `bot_name`: the custom name the user gave you
- `user_name`: the user's name
- user preferences to long-term memory

Ask the questions ONE AT A TIME, waiting for each answer before asking the next.
If the user's first message already contains task content (not just "hi"),
still start onboarding but keep it brief — you can help with their task after.
"""
        elif getattr(self, "memory", None):
            try:
                all_mem = self.memory.list_all()
                if "bot_name" not in all_mem:
                    system_msg += """
### Bot Naming
The user hasn't given you a custom name yet. On the first message,
briefly ask: "By the way, would you like to give me a name? You can
call me anything you like!" If they give a name, `remember("bot_name", name)`.
If they say no or skip, `remember("bot_name", "PythonClaw")` and move on.
Don't repeat this if `bot_name` already exists in memory.
"""
            except Exception:
                pass

        self.messages.append({"role": "system", "content": system_msg})
        if self.verbose:
            logger.debug("System prompt built. Skill catalog: %d skills.", len(self._registry.discover()))

    # ── Tool management ───────────────────────────────────────────────────────

    def _normalize_input(self, user_input: str | list) -> str | list:
        """If provider doesn't support images, extract text from multimodal input."""
        if isinstance(user_input, str):
            return user_input
        if getattr(self.provider, "supports_images", False):
            return user_input
        text_parts = []
        for part in user_input:
            if isinstance(part, dict) and part.get("type") == "text":
                text_parts.append(part["text"])
            elif isinstance(part, dict) and part.get("type") == "image_url":
                text_parts.append("[image attached — your LLM provider does not support image input]")
        return "\n".join(text_parts) if text_parts else str(user_input)

    def _cap_parallel_skills(self, tool_calls: list) -> list:
        """Enforce MAX_PARALLEL_SKILLS — cap skill activations per round.

        Non-skill tool calls (run_command, remember, etc.) are not limited.
        Excess skill calls get stub responses appended to messages.
        """
        skill_names = {"use_skill"}
        skill_calls = [tc for tc in tool_calls if tc.function.name in skill_names]
        if len(skill_calls) <= self.MAX_PARALLEL_SKILLS:
            return tool_calls

        keep = set(id(tc) for tc in skill_calls[:self.MAX_PARALLEL_SKILLS])
        kept: list = []
        for tc in tool_calls:
            if tc.function.name in skill_names and id(tc) not in keep:
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": (
                        f"(skipped — max {self.MAX_PARALLEL_SKILLS} "
                        "parallel skills per round)"
                    ),
                })
            else:
                kept.append(tc)
        logger.info(
            "Capped parallel skills: %d → %d",
            len(skill_calls), self.MAX_PARALLEL_SKILLS,
        )
        return kept

    def _build_tools(self) -> list[dict]:
        """Assemble the full tool schema list for the current session."""
        tools = PRIMITIVE_TOOLS + SKILL_TOOLS + META_SKILL_TOOLS + MEMORY_TOOLS
        if self._web_search_enabled:
            tools = tools + [WEB_SEARCH_TOOL]
        if self.rag:
            tools = tools + [KNOWLEDGE_TOOL]
        if self._cron_manager:
            tools = tools + CRON_TOOLS
        return tools

    def _execute_tool_call(self, tool_call) -> str:
        """Dispatch a single tool call and return the string result."""
        func_name: str = tool_call.function.name
        try:
            args: dict = json.loads(tool_call.function.arguments)
        except json.JSONDecodeError as exc:
            return f"Error: could not parse tool arguments: {exc}"

        if self.verbose:
            logger.debug("Tool: %s  args=%s", func_name, args)

        try:
            if func_name == "use_skill":
                result = self._use_skill(args.get("skill_name"))
            elif func_name == "list_skill_resources":
                resources = self._registry.list_resources(args.get("skill_name", ""))
                if resources:
                    result = "Resources:\n" + "\n".join(f"  - {r}" for r in resources)
                else:
                    result = "No bundled resources found (or skill not found)."
            elif func_name == "remember":
                result = self.memory.remember(args.get("content"), args.get("key"))
            elif func_name == "recall":
                result = self.memory.recall(args.get("query", "*"))
            elif func_name == "memory_get":
                result = self.memory.memory_get(args.get("path", "MEMORY.md"))
                if not result:
                    result = "(file not found or empty)"
            elif func_name == "memory_list_files":
                files = self.memory.list_files()
                result = "\n".join(files) if files else "(no memory files)"
            elif func_name == "forget":
                result = self.memory.forget(args.get("key", ""))
            elif func_name == "update_index":
                path = self.memory.write_index(args.get("content", ""))
                result = f"INDEX.md updated at {path}"
            elif func_name == "consult_knowledge_base" and self.rag:
                hits = self.rag.retrieve(args.get("query"), top_k=5)
                if hits:
                    result = "Found relevant info:\n" + "\n".join(
                        f"- [{h['source']}]: {h['content']}" for h in hits
                    )
                else:
                    result = "No relevant information found in the knowledge base."
            elif func_name == "cron_add" and self._cron_manager:
                result = self._cron_manager.add_dynamic_job(
                    job_id=args.get("job_id"),
                    cron_expr=args.get("cron"),
                    prompt=args.get("prompt"),
                    deliver_to="telegram" if args.get("deliver_to_chat_id") else None,
                    chat_id=args.get("deliver_to_chat_id"),
                )
            elif func_name == "cron_remove" and self._cron_manager:
                result = self._cron_manager.remove_dynamic_job(args.get("job_id"))
            elif func_name == "cron_list" and self._cron_manager:
                result = self._cron_manager.list_jobs()
            elif func_name == "create_skill":
                result = AVAILABLE_TOOLS["create_skill"](**args)
                self._refresh_skill_registry()
            elif func_name in AVAILABLE_TOOLS:
                result = AVAILABLE_TOOLS[func_name](**args)
            else:
                result = f"Error: unknown tool '{func_name}'."
        except Exception as exc:
            result = f"Error executing '{func_name}': {exc}"

        if self.verbose:
            preview = str(result)[:200] + ("..." if len(str(result)) > 200 else "")
            logger.debug("Result: %s", preview)

        return str(result)

    # ── Tool call parsing fallbacks ────────────────────────────────────────────

    def _create_forced_tool_call(self):
        """Create a forced tool call when LLM fails to call tools despite strong prompts.
        
        This is used when:
        1. Troubleshooting intent is detected
        2. LLM returns empty content or no tool calls
        3. We need to force skill activation
        
        Returns MockToolCall object or None if no appropriate skill found.
        """
        from .llm.response import MockFunction, MockToolCall
        
        try:
            # Get the last user message to determine intent
            last_user_msg = ""
            for msg in reversed(self.messages):
                if msg.get("role") == "user":
                    last_user_msg = msg.get("content", "")
                    break
            
            # Detect troubleshooting intent
            troubleshooting_keywords = ['排查', '诊断', 'troubleshoot', 'diagnose', 'static route', '静态路由']
            has_troubleshooting_intent = any(kw in last_user_msg.lower() for kw in troubleshooting_keywords)
            
            if has_troubleshooting_intent:
                skills = self._registry.discover()
                
                # Extract skill names from SkillMetadata objects
                skill_names = [s.name for s in skills]
                
                # Prefer static-troubleshooting for static route issues
                if 'static-troubleshooting' in skill_names:
                    skill_name = 'static-troubleshooting'
                elif 'CT-AP-not-online-zhuwang' in skill_names:
                    skill_name = 'CT-AP-not-online-zhuwang'
                else:
                    # Find any troubleshooting skill by checking name field
                    troubleshooting_skills = [s.name for s in skills if 'troubleshoot' in s.name.lower()]
                    if troubleshooting_skills:
                        skill_name = troubleshooting_skills[0]
                    else:
                        logger.warning("[Agent] No troubleshooting skills available")
                        return None
                
                logger.info(f"[Agent] Force-activating skill: {skill_name}")
                arguments_json = json.dumps({"skill_name": skill_name})
                
                return MockToolCall(
                    id=f"call_forced_{int(time.time())}",
                    function=MockFunction(name="use_skill", arguments=arguments_json),
                    type="function"
                )
            
        except Exception as e:
            logger.warning(f"[Agent] Failed to create forced tool call: {e}")
            import traceback
            logger.debug(f"[Agent] Forced tool call error: {traceback.format_exc()}")
        
        return None

    # ── Skill registry refresh (after create_skill) ────────────────────────

    def _refresh_skill_registry(self) -> None:
        """Invalidate the registry cache so newly created skills are discovered."""
        self._registry.invalidate()
        new_catalog = self._registry.build_catalog()
        self.messages.append({
            "role": "system",
            "content": (
                "[Skill Registry Updated]\n"
                "A new skill has been created. Updated skill catalog:\n\n"
                f"{new_catalog}"
            ),
        })
        if self.verbose:
            count = len(self._registry.discover())
            logger.debug("Skill registry refreshed — %d skills now available.", count)

    # ── Skill loading (Level 2) ───────────────────────────────────────────────

    @staticmethod
    def _check_dependencies(deps: list[str]) -> list[str]:
        """Return the subset of *deps* (pip package names) that are NOT installed."""
        from importlib.metadata import PackageNotFoundError, distribution

        missing: list[str] = []
        for pkg in deps:
            try:
                distribution(pkg)
            except PackageNotFoundError:
                missing.append(pkg)
        return missing

    def _use_skill(self, skill_name: str) -> str:
        """
        Level 2: Load a skill's full instructions into context.

        Called when the LLM triggers ``use_skill``.  The SKILL.md body
        is injected as a system message so subsequent turns can follow
        the instructions.

        If the skill directory contains a ``check_setup.sh`` script, it
        is executed automatically before activation.  When the check fails
        (non-zero exit), the skill is still loaded but a prominent warning
        with the script output is included so the LLM can guide the user
        through the fix.
        """
        if skill_name in self.loaded_skill_names:
            return f"Skill '{skill_name}' is already active."

        skill = self._registry.load_skill(skill_name)
        if not skill:
            return f"Error: skill '{skill_name}' not found in catalog."

        # ── Dependency check ─────────────────────────────────────────────────
        dep_warning = ""
        if skill.metadata.dependencies:
            missing = self._check_dependencies(skill.metadata.dependencies)
            if missing:
                pip_cmd = f"pip install {' '.join(missing)}"
                dep_warning = (
                    f"\n\n⚠️ **MISSING DEPENDENCIES**: {', '.join(missing)}\n"
                    f"This skill requires packages that are not installed.\n"
                    f"Ask the user: \"This skill needs **{', '.join(missing)}**. "
                    f"Would you like me to install {'them' if len(missing) > 1 else 'it'}?\"\n"
                    f"If the user agrees, run: `{pip_cmd}`\n"
                    f"Do NOT proceed with skill commands until dependencies are installed.\n"
                )
                if self.verbose:
                    logger.debug("Skill '%s' missing deps: %s", skill_name, missing)

        # ── Pre-activation environment check ─────────────────────────────────
        setup_warning = ""
        check_script = os.path.join(skill.metadata.path, "check_setup.sh")
        if os.path.isfile(check_script):
            import subprocess

            from .tools import _venv_env
            try:
                proc = subprocess.run(
                    ["bash", check_script],
                    capture_output=True, text=True, timeout=15,
                    env=_venv_env(),
                )
                if proc.returncode != 0:
                    output = (proc.stdout + proc.stderr).strip()
                    setup_warning = (
                        f"\n\n⚠️ **SETUP CHECK FAILED** (exit code {proc.returncode}):\n"
                        f"```\n{output}\n```\n"
                        f"Please tell the user what went wrong and how to fix it "
                        f"before attempting to use this skill's commands.\n"
                    )
                    if self.verbose:
                        logger.debug("Skill '%s' setup check FAILED: %s", skill_name, output)
                else:
                    setup_info = proc.stdout.strip()
                    setup_warning = f"\n\n✅ Setup check passed:\n```\n{setup_info}\n```\n"
                    if self.verbose:
                        logger.debug("Skill '%s' setup check passed.", skill_name)
            except Exception as exc:
                setup_warning = f"\n\n⚠️ Setup check could not run: {exc}\n"

        resources = self._registry.list_resources(skill_name)
        resource_hint = ""
        if resources:
            resource_hint = (
                "\n\n**Bundled resources** (use `read_file` / `run_command` to access):\n"
                + "\n".join(f"  - `{skill.metadata.path}/{r}`" for r in resources)
            )

        injection = (
            f"\n[SKILL ACTIVATED: {skill.name}]\n"
            f"Path: {skill.metadata.path}\n\n"
            f"{skill.instructions}{resource_hint}{dep_warning}{setup_warning}\n"
        )
        self.pending_injections.append(injection)
        self.loaded_skill_names.add(skill_name)
        
        # Extract step markers from skill instructions for proactive notification
        import re
        step_pattern = r'\[STEP_START\](.*?)\[STEP_END\]'
        steps = re.findall(step_pattern, skill.instructions)
        if steps:
            # Store extracted steps for later use in chat_stream
            self._current_skill_steps = steps
            logger.info("[SkillSteps] Extracted %d steps from skill '%s': %s", len(steps), skill_name, steps[:3])
        else:
            self._current_skill_steps = []
        
        if self.verbose:
            logger.debug("Skill activated: %s (Level 2 loaded)", skill_name)

        status = "activated"
        if dep_warning:
            status = "activated but MISSING DEPENDENCIES — ask user to install"
        elif "FAILED" in setup_warning:
            status = "activated with setup warnings — tell the user how to fix"
        return (
            f"Skill '{skill_name}' {status}. "
            f"Instructions loaded into context. "
            f"Bundled resources: {resources or 'none'}."
        )

    # ── History management ────────────────────────────────────────────────────

    @staticmethod
    def _sanitize_tool_pairs(messages: list[dict]) -> list[dict]:
        """Ensure every assistant message with ``tool_calls`` is immediately
        followed by matching ``tool`` messages, and every ``tool`` message has
        a preceding assistant message with a matching ``tool_calls`` entry.

        Broken pairs (caused by pruning, failed restores, or errors) are
        removed so the LLM API never receives an invalid sequence.
        """
        result: list[dict] = []
        i = 0
        n = len(messages)
        while i < n:
            msg = messages[i]
            tool_calls = msg.get("tool_calls")

            if msg.get("role") == "assistant" and tool_calls:
                expected_ids: set[str] = set()
                for tc in tool_calls:
                    tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                    if tc_id:
                        expected_ids.add(tc_id)

                # Collect subsequent tool responses that belong to this batch
                j = i + 1
                collected_tool_msgs: list[dict] = []
                while j < n and messages[j].get("role") in ("tool", "system"):
                    if messages[j].get("role") == "tool":
                        collected_tool_msgs.append(messages[j])
                    else:
                        break  # system injection sits between tool batch and next turn
                    j += 1

                found_ids = {
                    m.get("tool_call_id")
                    for m in collected_tool_msgs
                    if m.get("tool_call_id")
                }

                if expected_ids and expected_ids <= found_ids:
                    # Valid pair — keep assistant + matching tool messages
                    result.append(msg)
                    result.extend(collected_tool_msgs)
                    i = j
                else:
                    # Broken pair — skip the assistant message and any
                    # orphaned tool responses
                    logger.debug(
                        "Dropping broken tool-call sequence: expected %s, got %s",
                        expected_ids, found_ids,
                    )
                    i = j
            elif msg.get("role") == "tool":
                # Orphaned tool message (no preceding assistant with tool_calls) — skip
                i += 1
            else:
                result.append(msg)
                i += 1
        return result

    def _get_pruned_messages(self) -> list[dict]:
        """
        Build a context window for the API call:
          - All system messages (system prompt + skill injections + compaction summaries)
          - The most recent `max_chat_history` non-system messages

        Ensures the window contains only valid tool-call/response pairs.
        """
        system_msgs = [m for m in self.messages if m.get("role") == "system"]
        chat_msgs   = [m for m in self.messages if m.get("role") != "system"]

        # 取最近10轮对话
        chat_msgs = chat_msgs[-self.compaction_recent_keep:]

        if len(chat_msgs) > self.max_chat_history:
            chat_msgs = chat_msgs[-self.max_chat_history:]

        chat_msgs = self._sanitize_tool_pairs(chat_msgs)
        return system_msgs + chat_msgs

    # ── Compaction ────────────────────────────────────────────────────────────

    def compact(self, instruction: str | None = None) -> str:
        """
        Manually compact conversation history.

        Summarises older messages into a single [Compaction Summary] system
        entry, flushes important facts to long-term memory, and persists the
        summary to context/compaction/history.jsonl.

        Parameters
        ----------
        instruction : optional focus hint, e.g. "focus on open tasks"
        """
        chat_msgs = [m for m in self.messages if m.get("role") != "system"]
        if len(chat_msgs) <= self.compaction_recent_keep:
            return (
                f"Nothing to compact yet — only {len(chat_msgs)} message(s) in history "
                f"(threshold: {self.compaction_recent_keep})."
            )
        try:
            new_messages, summary = _do_compact(
                messages=self.messages,
                provider=self.provider,
                memory=self.memory,
                recent_keep=self.compaction_recent_keep,
                instruction=instruction,
            )
        except Exception as exc:
            return f"Compaction failed: {exc}"

        self.messages = new_messages
        self.compaction_count += 1

        lines = summary.splitlines()
        preview = "\n".join(lines[:5])
        if len(lines) > 5:
            preview += f"\n... ({len(lines) - 5} more lines)"
        return f"Compaction #{self.compaction_count} complete.\n\nSummary:\n{preview}"

    _memory_flushed_this_cycle: bool = False

    def _maybe_auto_compact(self) -> bool:
        """实现了 自动上下文压缩 机制，是 Agent 管理 LLM 上下文窗口的核心策略。
        设计意图是：当消息历史（ self.messages ）积累过多 token 时，自动触发压缩，防止超出 LLM 的上下文窗口限制。
        """
        if not self.auto_compaction:
            return False
        # 估计当前消息历史的 token 数量
        tokens = estimate_tokens(self.messages)
        soft_threshold = int(self.compaction_threshold * 00.8)

        # _memory_flushed_this_cycle 本循环周期内，没有刷过内存
        if not self._memory_flushed_this_cycle and tokens >= soft_threshold:
            # 异步预刷内存
            self._bg_executor.submit(self._proactive_memory_flush)
            self._memory_flushed_this_cycle = True

        if tokens < self.compaction_threshold:
            return False

        if self.verbose:
            logger.debug("Auto-compaction triggered.")
        try:
            new_messages, _ = _do_compact(
                messages=self.messages,
                provider=self.provider,
                memory=self.memory,
                recent_keep=self.compaction_recent_keep,
            )
            self.messages = new_messages
            self.compaction_count += 1
            self._memory_flushed_this_cycle = False
            return True
        except Exception as exc:
            if self.verbose:
                logger.error("Auto-compaction failed (non-fatal): %s", exc)
            return False

    def _proactive_memory_flush(self) -> None:
        """自动上下文压缩机制的"预刷新"环节 ，在压缩发生之前，提前将对话中的重要事实异步保存到长期记忆中。
        在消息 token 数达到 软阈值（compaction_threshold × 80%） 时被触发的：
        """
        from .compaction import memory_flush

        chat_msgs = [m for m in self.messages if m.get("role") != "system"]
        if len(chat_msgs) < 4:
            return
        try:
            saved = memory_flush(chat_msgs, self.provider, self.memory)
            if self.verbose and saved:
                logger.debug("Proactive memory flush saved %d fact(s).", saved)
        except Exception as exc:
            logger.error("Proactive memory flush failed (non-fatal): %s", exc)

    # ── Session management ─────────────────────────────────────────────────

    def clear_history(self) -> None:
        """Clear conversation history but keep the agent intact.

        Preserves loaded skills, memory, RAG, provider, and all config.
        Only resets messages to a fresh system prompt and clears
        conversation-specific state.
        """
        self.messages.clear()
        self.loaded_skill_names.clear()
        self.compaction_count = 0
        self._init_system_prompt()

    # ── Main chat loop ────────────────────────────────────────────────────────

    def chat(self, user_input: str | list, **kwargs) -> str:
        """Send *user_input* to the LLM and return the final text response.

        *user_input* can be a plain string or a content-array for
        multimodal input (e.g. ``[{"type":"text","text":"..."}, {"type":"image_url",...}]``).
        """
        user_input = self._normalize_input(user_input)
        self.messages.append({"role": "user", "content": user_input})

        _log_detail({
            "event": "user_input",
            "content": user_input if isinstance(user_input, str) else "(multimodal)",
        })

        current_tools = self._build_tools()
        tool_rounds = 0
        chat_start = time.monotonic()

        while True:
            try:
                self._maybe_auto_compact()
                messages_to_send = self._get_pruned_messages()

                if self.show_full_context:
                    logger.debug(
                        "Context window (%d messages):\n%s",
                        len(messages_to_send),
                        json.dumps(messages_to_send, indent=2, ensure_ascii=False),
                    )

                response = self.provider.chat(
                    messages=messages_to_send,
                    tools=current_tools,
                    tool_choice="auto",
                )
                message = response.choices[0].message

                if not message.tool_calls:
                    self.messages.append(message.model_dump())
                    _log_detail({
                        "event": "response",
                        "tool_rounds": tool_rounds,
                        "elapsed_ms": int((time.monotonic() - chat_start) * 1000),
                        "response_len": len(message.content or ""),
                    })
                    return message.content

                tool_rounds += 1
                if tool_rounds > self.MAX_TOOL_ROUNDS:
                    msg_dump = message.model_dump()
                    self.messages.append(msg_dump)

                    # Provide stub responses for every tool_call so the
                    # history stays valid for the API (each tool_call_id
                    # MUST have a matching tool-role message).
                    for tc in message.tool_calls:
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": "(skipped — tool-call limit reached)",
                        })

                    limit_msg = (
                        f"Reached the maximum of {self.MAX_TOOL_ROUNDS} tool-call rounds. "
                        "Please provide a final answer with the information gathered so far."
                    )
                    self.messages.append({"role": "system", "content": limit_msg})
                    if self.verbose:
                        logger.debug("Tool round limit (%d) reached, forcing text reply.", self.MAX_TOOL_ROUNDS)
                    try:
                        final = self.provider.chat(
                            messages=self._get_pruned_messages(),
                            tools=current_tools,
                            tool_choice="none",
                        )
                        final_msg = final.choices[0].message
                        self.messages.append(final_msg.model_dump())
                        return final_msg.content
                    except Exception as exc:
                        return f"Error (after hitting tool limit): {exc}"

                self.messages.append(message.model_dump())
                self.pending_injections = []

                tool_calls = message.tool_calls
                tool_calls = self._cap_parallel_skills(tool_calls)

                _log_detail({
                    "event": "tool_calls",
                    "round": tool_rounds,
                    "calls": [
                        {"name": tc.function.name, "args": tc.function.arguments}
                        for tc in tool_calls
                    ],
                })

                t0 = time.monotonic()
                results: dict[str, str] = {}
                with ThreadPoolExecutor(max_workers=min(len(tool_calls), 16)) as pool:
                    futures = {
                        pool.submit(self._execute_tool_call, tc): tc
                        for tc in tool_calls
                    }
                    for future in as_completed(futures, timeout=self.TOOL_TIMEOUT):
                        tc = futures[future]
                        try:
                            results[tc.id] = future.result()
                        except Exception as exc:
                            results[tc.id] = f"Error: {exc}"
                for tc in tool_calls:
                    if tc.id not in results:
                        results[tc.id] = (
                            f"Error: tool '{tc.function.name}' timed out "
                            f"after {self.TOOL_TIMEOUT}s"
                        )
                _log_detail({
                    "event": "tool_results",
                    "round": tool_rounds,
                    "count": len(tool_calls),
                    "elapsed_ms": int((time.monotonic() - t0) * 1000),
                    "tools": [tc.function.name for tc in tool_calls],
                })
                for tc in tool_calls:
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": results[tc.id],
                    })

                for injection in self.pending_injections:
                    self.messages.append({"role": "system", "content": injection})
                self.pending_injections = []

            except FuturesTimeout:
                logger.warning("Tool execution timed out at round %d", tool_rounds)
                for tc in tool_calls:
                    if tc.id not in results:
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": f"Error: timed out after {self.TOOL_TIMEOUT}s",
                        })
                continue
            except Exception as exc:
                logger.error("Critical error in Agent.chat()")
                return f"Error: {exc}"

    def chat_stream(
        self,
        user_input: str | list,
        on_token: object = None,
    ) -> str:
        """chat() 的 流式版本
        - user_input ：可以是纯字符串，也可以是多模态内容数组（例如图文混合）。
        - on_token ：每次收到文本片段时调用的回调函数。
        - 返回值 ：完整的最终文本（与 chat() 一致）。
        """
        _t0 = time.monotonic() # 测试用

        user_input = self._normalize_input(user_input) # 处理多模态兼容性
        self.messages.append({"role": "user", "content": user_input})
        _log_detail({
            "event": "user_input",
            "content": user_input if isinstance(user_input, str) else "(multimodal)",
        })
        _t1 = time.monotonic() # 测试用
        _stage_input_norm = int((_t1 - _t0) * 1000) # 测试用

        current_tools = self._build_tools() # 组装当前会话可用的完整工具列表
        tool_rounds = 0
        skill_step_counter = 0  # Independent counter for skill steps (excludes use_skill)
        chat_start = time.monotonic()
        _t2 = time.monotonic() # 测试用
        _stage_build_tools = int((_t2 - _t1) * 1000) # 测试用
        
        # Flag to track if we should suppress conversational text
        # Always suppress in skill execution scenarios to prevent dialogue leakage
        suppress_conversational_text = hasattr(self, '_current_skill_steps') and bool(self._current_skill_steps)
        
        # Check if user input suggests skill execution (troubleshooting, diagnosis, etc.)
        # If so, proactively suppress conversational text from the start
        user_input_lower = ""
        if isinstance(user_input, str):
            user_input_lower = user_input.lower()
        
        troubleshooting_keywords = ['排查', '诊断', 'troubleshoot', 'diagnose', 'static route', '静态路由', '不通', 'not working']
        if any(kw in user_input_lower for kw in troubleshooting_keywords):
            logger.info("[Agent] Detected troubleshooting intent, enabling text suppression")
            suppress_conversational_text = True
        
        # Note: Initial step notification is handled proactively before tool execution
        # No need to send it here since steps are extracted during skill activation
        _t3 = time.monotonic() # 测试用
        _stage_intent = int((_t3 - _t2) * 1000) # 测试用

        _first_token_logged = False # 测试用

        while True:
            try:
                # self._maybe_auto_compact() # 自动检查上下文窗口的 token 数是否超过阈值，必要时触发压缩。
                messages_to_send = self._get_pruned_messages() # 构建system消息和user消息的提示词
                _t4 = time.monotonic() # 测试用
                _stage_pre_llm = int((_t4 - _t3) * 1000) # 测试用

                gen = self.provider.chat_stream(
                    messages=messages_to_send,
                    tools=current_tools,
                    tool_choice="auto",
                )
                response = None
                while True:
                    try:
                        chunk = next(gen)
                        if chunk.get("type") == "text_delta" and on_token:
                            # Skip sending text to frontend when skill is active or tool calls detected
                            # Only send step notifications (handled separately)
                            should_suppress = (
                                suppress_conversational_text or 
                                (hasattr(self, '_current_skill_steps') and self._current_skill_steps)
                            )
                            
                            if should_suppress:
                                # Suppress all streaming text
                                pass
                            else:
                                # No skill active, send text normally
                                if not _first_token_logged: # 测试用
                                    _first_token_logged = True
                                    _t5 = time.monotonic()
                                    _stage_llm_first = int((_t5 - _t4) * 1000)
                                    _stage_total = int((_t5 - _t0) * 1000)
                                    _log_detail({
                                        "event": "stream_first_token_timing",
                                        "stages_ms": {
                                            "input_normalization": _stage_input_norm,
                                            "build_tools": _stage_build_tools,
                                            "intent_manual_search": _stage_intent,
                                            "pre_llm_prep": _stage_pre_llm,
                                            "llm_first_token": _stage_llm_first,
                                            "total": _stage_total,
                                        },
                                    })
                                    logger.info(
                                        "首token时间 (ms): 多模态处理=%d |  构建工具=%d | 意图和搜索=%d | 构建提示词=%d | 主流程=%d | 总计=%d",
                                        _stage_input_norm, _stage_build_tools, _stage_intent,
                                        _stage_pre_llm, _stage_llm_first, _stage_total,
                                    )
                                on_token(chunk["text"])
                    except StopIteration as si:
                        response = si.value
                        break

                if response is None:
                    return ""

                message = response.choices[0].message
                
                # Log detailed response information for debugging
                logger.info("[Agent] === LLM Response Details ===")
                logger.info("[Agent] Content (first 500 chars): %s", repr((message.content or "")[:500]))
                logger.info("[Agent] Content length: %d chars", len(message.content or ""))
                logger.info("[Agent] Has tool_calls: %s", bool(message.tool_calls))
                if message.tool_calls:
                    logger.info("[Agent] Tool calls count: %d", len(message.tool_calls))
                    for i, tc in enumerate(message.tool_calls):
                        logger.info("[Agent]   Tool call %d: name=%s, args=%s", 
                                  i, tc.function.name, tc.function.arguments[:200])
                else:
                    logger.info("[Agent] Tool calls: None/Empty")
                
                # Log the full message object structure for debugging
                msg_dict = message.model_dump()
                logger.debug("[Agent] Full message keys: %s", list(msg_dict.keys()))
                logger.debug("[Agent] Message dict: %s", json.dumps(msg_dict, ensure_ascii=False, default=str)[:1000])
                
                print(message)

                # Fallback: If troubleshooting intent detected but no tool calls, force create tool call
                if suppress_conversational_text and not message.tool_calls:
                    logger.warning("[Agent] Troubleshooting intent detected but LLM did not call any tool")
                    logger.info("[Agent] Content preview: %s", repr((message.content or "")[:200]))
                    
                    # Force create use_skill tool call based on intent
                    forced_tool_call = self._create_forced_tool_call()
                    if forced_tool_call:
                        logger.info(f"[Agent] Force-created tool call: {forced_tool_call.function.name}")
                        logger.info(f"[Agent] Tool arguments: {forced_tool_call.function.arguments}")
                        
                        # IMPORTANT: Directly execute the forced tool call without going through another LLM round
                        # This avoids infinite loop since H3C AI doesn't support native function calling
                        logger.info("[Agent] Executing forced tool call directly...")
                        
                        # Execute the tool call
                        try:
                            result = self._execute_tool_call(forced_tool_call)
                            logger.info(f"[Agent] Forced tool execution result (first 200 chars): {result[:200]}")
                            
                            # Check if skill was successfully activated
                            if "activated" in result.lower() or "already active" in result.lower():
                                # Skill is now active, extract steps and start execution
                                logger.info("[Agent] Skill activated, starting step execution...")
                                
                                # Extract steps from the activated skill
                                if hasattr(self, '_current_skill_steps') and self._current_skill_steps:
                                    logger.info(f"[Agent] Found {len(self._current_skill_steps)} steps to execute")
                                    
                                    # IMPORTANT: Don't continue the loop (which would call LLM again)
                                    # Instead, directly execute the first step script
                                    first_step_name = self._current_skill_steps[0]
                                    logger.info(f"[Agent] Executing first step: {first_step_name}")
                                    
                                    # Create a mock tool call for execute_step_script
                                    from .llm.response import MockFunction, MockToolCall
                                    
                                    # Extract skill name from forced_tool_call arguments
                                    skill_args = json.loads(forced_tool_call.function.arguments) if isinstance(forced_tool_call.function.arguments, str) else forced_tool_call.function.arguments
                                    skill_name = skill_args.get("skill_name", "static-troubleshooting")
                                    
                                    # Build parameters for the step script
                                    # execute_step_script expects: script_name, mode, params
                                    step_params = {
                                        "step_name": first_step_name,
                                        "step_number": 1,
                                        "skill_name": skill_name
                                    }
                                    
                                    # Arguments must match execute_step_script signature: (script_name, mode, params)
                                    step_args_json = json.dumps({
                                        "script_name": "step1_check_route.py",
                                        "mode": "build",
                                        "params": step_params
                                    })
                                    
                                    step_tool_call = MockToolCall(
                                        id=f"call_step_1_{int(time.time())}",
                                        function=MockFunction(name="execute_step_script", arguments=step_args_json),
                                        type="function"
                                    )

                                    # Execute the step script
                                    step_result = self._execute_tool_call(step_tool_call)
                                    logger.info(f"[Agent] Step execution result (first 200 chars): {step_result[:200]}")
                                    
                                    # Send step notification and command to frontend
                                    if on_token:
                                        # Send stepName
                                        step_marker = f"[STEP_START]{first_step_name}[STEP_END]"
                                        logger.info(f"[SkillSteps] Sending proactive step notification for step 1: {first_step_name}")
                                        on_token(step_marker)
                                        
                                        # Send stepCommand if present in result
                                        try:
                                            import json as _json
                                            if isinstance(step_result, str):
                                                parsed = _json.loads(step_result)
                                                if parsed.get("answerType") == "stepCommand":
                                                    logger.info("[Agent] Sending stepCommand to frontend")
                                                    on_token(step_result)
                                        except Exception as e:
                                            logger.warning(f"[Agent] Failed to parse step result: {e}")
                                    
                                    # Now pause and wait for frontend response
                                    logger.info("[Agent] Pausing execution, waiting for frontend to send step analysis result")
                                    
                                    # Add messages to history
                                    msg_dump = message.model_dump()
                                    if msg_dump.get("content") is None:
                                        msg_dump["content"] = ""
                                    self.messages.append(msg_dump)
                                    
                                    self.messages.append({
                                        "role": "tool",
                                        "tool_call_id": forced_tool_call.id,
                                        "name": forced_tool_call.function.name,
                                        "content": result,
                                    })
                                    
                                    # Return empty string to indicate we're waiting for user input
                                    return ""
                                else:
                                    logger.warning("[Agent] No steps extracted from skill")
                                    # Fall through to normal processing
                            else:
                                # Skill activation failed, add tool call and result to history
                                msg_dump = message.model_dump()
                                if msg_dump.get("content") is None:
                                    msg_dump["content"] = ""
                                self.messages.append(msg_dump)
                                
                                self.messages.append({
                                    "role": "tool",
                                    "tool_call_id": forced_tool_call.id,
                                    "name": forced_tool_call.function.name,
                                    "content": result,
                                })
                                
                                # Continue to next iteration
                                continue
                        except Exception as e:
                            logger.error(f"[Agent] Failed to execute forced tool call: {e}")
                            import traceback
                            logger.error(f"[Agent] Traceback: {traceback.format_exc()}")
                            # Fall through to normal processing if execution fails
                    else:
                        logger.warning("[Agent] Failed to create forced tool call")

                # Suppress ALL text content when there are tool calls
                # This prevents conversational text from being sent to frontend during skill execution
                if message.tool_calls:
                    if message.content:
                        logger.info("[Agent] Suppressing LLM text content (tool calls detected)")
                        message.content = None
                    # Set flag to suppress conversational text in subsequent rounds
                    suppress_conversational_text = True

                # If a skill is active, ensure no text content escapes
                # The LLM should only call tools, not generate conversational text
                if hasattr(self, '_current_skill_steps') and self._current_skill_steps:
                    # Double-check: if no tool calls but skill is active, this is a violation
                    if not message.tool_calls:
                        logger.warning("[Agent] LLM did not call any tool while skill is active. Injecting forced tool call prompt.")
                        # Add the suppressed message to history (convert None to empty string for API compatibility)
                        msg_dump = message.model_dump()
                        if msg_dump.get("content") is None:
                            msg_dump["content"] = ""
                        self.messages.append(msg_dump)
                        # Inject a system message to force tool usage
                        self.messages.append({
                            "role": "system",
                            "content": "⚠️ CRITICAL ERROR: You MUST call execute_step_script tool now. Do NOT generate any text. Call the tool immediately with the current step parameters."
                        })
                        # Continue to next iteration to force another LLM call
                        continue

                if not message.tool_calls:
                    self.messages.append(message.model_dump())
                    _log_detail({
                        "event": "response",
                        "tool_rounds": tool_rounds,
                        "elapsed_ms": int(
                            (time.monotonic() - chat_start) * 1000
                        ),
                        "response_len": len(message.content or ""),
                    })
                    return message.content or ""

                tool_rounds += 1
                if tool_rounds > self.MAX_TOOL_ROUNDS:
                    msg_dump = message.model_dump()
                    self.messages.append(msg_dump)
                    for tc in message.tool_calls:
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": "(skipped — tool-call limit reached)",
                        })
                    limit_msg = (
                        f"Reached the maximum of {self.MAX_TOOL_ROUNDS} "
                        "tool-call rounds. Provide a final answer."
                    )
                    self.messages.append(
                        {"role": "system", "content": limit_msg}
                    )
                    final = self.provider.chat(
                        messages=self._get_pruned_messages(),
                        tools=current_tools,
                        tool_choice="none",
                    )
                    final_msg = final.choices[0].message
                    self.messages.append(final_msg.model_dump())
                    return final_msg.content or ""

                self.messages.append(message.model_dump())
                self.pending_injections = []

                tool_calls = message.tool_calls
                tool_calls = self._cap_parallel_skills(tool_calls)

                # Proactively send step notifications before executing tools
                if on_token and hasattr(self, '_current_skill_steps') and self._current_skill_steps:
                    # Only count actual step execution (execute_step_script), not skill activation (use_skill)
                    is_step_execution = any(
                        tc.function.name == 'execute_step_script' 
                        for tc in tool_calls
                    )
                    
                    if is_step_execution:
                        # Use independent counter for skill steps
                        current_step_idx = min(skill_step_counter, len(self._current_skill_steps) - 1)
                        if current_step_idx >= 0:
                            step_name = self._current_skill_steps[current_step_idx]
                            step_marker = f"[STEP_START]{step_name}[STEP_END]"
                            logger.info("[SkillSteps] Sending proactive step notification for step %d: %s", current_step_idx + 1, step_name)
                            on_token(step_marker)
                        
                        # Increment the skill step counter after sending notification
                        skill_step_counter += 1

                if on_token:
                    names = ", ".join(tc.function.name for tc in tool_calls)
                    arguments = ", ".join(tc.function.arguments for tc in tool_calls)
                    # Removed fixed messages to allow skill-defined step markers to be sent
                    pass

                results: dict[str, str] = {}
                with ThreadPoolExecutor(
                    max_workers=min(len(tool_calls), 16),
                ) as pool:
                    futures = {
                        pool.submit(self._execute_tool_call, tc): tc
                        for tc in tool_calls
                    }
                    for future in as_completed(
                        futures, timeout=self.TOOL_TIMEOUT
                    ):
                        tc = futures[future]
                        try:
                            results[tc.id] = future.result()
                        except Exception as exc:
                            results[tc.id] = f"Error: {exc}"

                # Send tool execution results to frontend if on_token is available
                has_step_command = False
                if on_token:
                    for tc in tool_calls:
                        if tc.function.name == 'execute_step_script' and tc.id in results:
                            try:
                                import json as _json
                                result_data = results[tc.id]
                                # Try to parse as JSON to extract stepCommand
                                if isinstance(result_data, str):
                                    parsed = _json.loads(result_data)
                                    if parsed.get("answerType") == "stepCommand":
                                        # Send stepCommand to frontend
                                        logger.info("[Agent] Sending stepCommand to frontend")
                                        on_token(result_data)
                                        has_step_command = True
                            except Exception as e:
                                logger.warning(f"[Agent] Failed to send stepCommand: {e}")

                # If we sent a stepCommand, pause execution and wait for frontend to send analysis result
                if has_step_command:
                    logger.info("[Agent] Pausing execution, waiting for frontend to send step analysis result")
                    # Add tool results to message history
                    for tc in tool_calls:
                        if tc.id not in results:
                            results[tc.id] = (
                                f"Error: tool '{tc.function.name}' timed out "
                                f"after {self.TOOL_TIMEOUT}s"
                            )
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": results[tc.id],
                        })
                    
                    # Inject pending injections
                    for injection in self.pending_injections:
                        self.messages.append(
                            {"role": "system", "content": injection}
                        )
                    self.pending_injections = []
                    
                    # Return empty string to indicate pause
                    # The conversation will continue when frontend sends analysis result
                    return ""

                for tc in tool_calls:
                    if tc.id not in results:
                        results[tc.id] = (
                            f"Error: tool '{tc.function.name}' timed out "
                            f"after {self.TOOL_TIMEOUT}s"
                        )
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": results[tc.id],
                    })

                for injection in self.pending_injections:
                    self.messages.append(
                        {"role": "system", "content": injection}
                    )
                self.pending_injections = []

            except FuturesTimeout:
                logger.warning("Tool execution timed out in stream round %d", tool_rounds)
                for tc in tool_calls:
                    if tc.id not in results:
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": f"Error: timed out after {self.TOOL_TIMEOUT}s",
                        })
                continue
            except Exception as exc:
                import traceback
                error_details = traceback.format_exc()
                logger.error(f"Critical error in Agent.chat_stream(): {exc}")
                logger.error(f"Traceback:\n{error_details}")
                return f"Error: {exc}"

        # This should never be reached
        return ""
