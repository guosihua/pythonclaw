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
        self._session_step_indices: dict[str, int] = {}  # Track step progress per session
        self._current_skill_steps: list[str] = []  # Store current skill's steps
        self._current_skill_step_numbers: list[int] = []  # Store actual step numbers from SKILL.md
        self._current_skill_step_commands: dict[int, list[str]] = {}  # Store commands for each step from SKILL.md (key: step_number)
        self._session_device_info: dict[str, dict] = {}  # Cache device_info per session for reuse across steps
        self._session_destination_network: dict[str, str] = {}  # Cache destination_network per session for reuse across steps
        self._session_topology_fetched: set[str] = set()  # Track which sessions already fetched topology (step 0)
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
        3. We need to force skill activation or continue pending steps
        
        Returns MockToolCall object or None if no appropriate skill found.
        """
        from .llm.response import MockFunction, MockToolCall
        
        try:
            # Check if a troubleshooting skill is already active with pending steps
            if hasattr(self, '_current_skill_steps') and self._current_skill_steps:
                logger.info(f"[Agent] Skill already active with {len(self._current_skill_steps)} pending steps")
                logger.info(f"[Agent] Pending steps: {self._current_skill_steps}")
                
                # Skill is already active, we need to continue execution
                # Check if user provided supplementary information
                last_user_msg = ""
                for msg in reversed(self.messages):
                    if msg.get("role") == "user":
                        last_user_msg = msg.get("content", "")
                        break
                
                # Extract device info and destination from user message
                import re
                ip_pattern = r'\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?\b'
                has_ip = bool(re.search(ip_pattern, last_user_msg))
                
                # If user provided IP info, create step execution instead of skill activation
                if has_ip:
                    logger.info(f"[Agent] User provided supplementary info, creating step execution")
                    # Create execute_step_script call to continue with the steps
                    skill_name = 'static-troubleshooting'  # Default to static-troubleshooting
                    analysis_type = 'check_route'  # Default analysis type
                    
                    # Extract destination network from user message
                    destination_network = "0.0.0.0/0"
                    dest_keyword_match = re.search(r'(目的网段|目标网段|目的IP)\s*[：:]?\s*([0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}(?:/[0-9]{1,2})?)', last_user_msg)
                    if dest_keyword_match:
                        destination_network = dest_keyword_match.group(2)
                        logger.info(f"[Agent] Extracted destination_network: {destination_network}")
                    else:
                        ip_matches = re.findall(ip_pattern, last_user_msg)
                        if ip_matches:
                            destination_network = ip_matches[-1]
                            logger.info(f"[Agent] Using last IP as destination: {destination_network}")
                    
                    # Extract device info
                    device_info = self._extract_device_info_from_context()
                    logger.info(f"[Agent] Extracted device info: {device_info}")
                    
                    # Get current step using session_id to track progress
                    session_id_for_step = self.session_id or "default"
                    current_step = self._session_step_indices.get(session_id_for_step, 0)
                    first_step_name = self._current_skill_steps[current_step] if current_step < len(self._current_skill_steps) else self._current_skill_steps[0]
                    logger.info(f"[Agent] Session: {session_id_for_step}, Current step: {current_step}, step name: {first_step_name}")
                    
                    # Get actual step number from SKILL.md
                    actual_step_num = self._current_skill_step_numbers[current_step] if current_step < len(self._current_skill_step_numbers) else current_step + 1
                    # Get commands from SKILL.md for this step
                    skill_commands = self._current_skill_step_commands.get(actual_step_num, [])
                    
                    # Create execute_step_script arguments
                    step_params = {
                        "step_name": first_step_name,
                        "step_number": actual_step_num,
                        "skill_name": skill_name,
                        "analysis_type": analysis_type,
                        "commands": skill_commands,
                        "destination_network": destination_network,
                        "device_info": device_info,
                        "context_id": "",
                        "question_no": "",
                        "session_id": session_id_for_step
                    }
                    
                    step_args_json = json.dumps({
                        "script_name": "step_executor.py",
                        "mode": "build_and_execute",
                        "params": step_params
                    })
                    
                    logger.info(f"[Agent] Creating execute_step_script tool call for pending step")
                    return MockToolCall(
                        id=f"call_step_pending_{int(time.time())}",
                        function=MockFunction(name="execute_step_script", arguments=step_args_json),
                        type="function"
                    )
            
            # Get the last user message to determine intent
            last_user_msg = ""
            for msg in reversed(self.messages):
                if msg.get("role") == "user":
                    last_user_msg = msg.get("content", "")
                    break
            
            # Detect troubleshooting intent
            troubleshooting_keywords = ['排查', '诊断', 'troubleshoot', 'diagnose', 'static route', '静态路由']
            has_troubleshooting_intent = any(kw in last_user_msg.lower() for kw in troubleshooting_keywords)
            
            # Also detect IP address patterns which might indicate routing troubleshooting
            import re
            ip_pattern = r'\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?\b'
            has_ip_address = bool(re.search(ip_pattern, last_user_msg))
            
            # If either troubleshooting keywords or IP address found, consider it troubleshooting intent
            has_troubleshooting_intent = has_troubleshooting_intent or has_ip_address
            
            logger.info(f"[Agent] Intent detection - last_user_msg: {last_user_msg[:100]}")
            logger.info(f"[Agent] Intent detection - has_troubleshooting_keywords: {any(kw in last_user_msg.lower() for kw in troubleshooting_keywords)}")
            logger.info(f"[Agent] Intent detection - has_ip_address: {has_ip_address}")
            logger.info(f"[Agent] Intent detection - has_troubleshooting_intent: {has_troubleshooting_intent}")
            
            if has_troubleshooting_intent:
                skills = self._registry.discover()
                logger.info(f"[Agent] Found {len(skills)} skills in registry")
                
                # Extract skill names from SkillMetadata objects
                skill_names = [s.name for s in skills]
                logger.info(f"[Agent] Available skill names: {skill_names}")
                
                # Prefer static-troubleshooting for static route issues
                if 'static-troubleshooting' in skill_names:
                    skill_name = 'static-troubleshooting'
                    logger.info(f"[Agent] Selected skill: {skill_name}")
                elif 'CT-AP-not-online-zhuwang' in skill_names:
                    skill_name = 'CT-AP-not-online-zhuwang'
                    logger.info(f"[Agent] Selected skill: {skill_name}")
                else:
                    # Find any troubleshooting skill by checking name field
                    troubleshooting_skills = [s.name for s in skills if 'troubleshoot' in s.name.lower()]
                    logger.info(f"[Agent] Troubleshooting skills found: {troubleshooting_skills}")
                    if troubleshooting_skills:
                        skill_name = troubleshooting_skills[0]
                        logger.info(f"[Agent] Selected troubleshooting skill: {skill_name}")
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

    def _fetch_and_emit_topology(
        self,
        session_id: str,
        context_id: str,
        question_no: str,
        on_token,
    ) -> bool:
        """Step 0: 获取网络拓扑图并按需求顺序通过 on_token 推送三条消息到前端。

        前端约定的发送顺序：
            1. {"answerType": "conversation", "message": "网络拓扑图获取中...<br/>"}
            2. {"answerType": "topology", "message": {"nodes": [...], "edges": [...]}}
            3. {"answerType": "conversation", "message": "获取成功"}

        采用 session-level 去重，同一会话只会执行一次。
        """
        if not on_token:
            # logger.info("[Topology] on_token not available, skip topology emission")
            return False

        cache_key = session_id or "default"
        if cache_key in self._session_topology_fetched:
            # logger.info(f"[Topology] Session {cache_key} already fetched topology, skip")
            return False

        try:
            from .tools import execute_topology_script

            topology_params = {
                "session_id": session_id or "",
                "context_id": context_id or "",
                "question_no": question_no or "",
            }
            # logger.info(f"[Topology] Fetching topology with params: {topology_params}")
            raw = execute_topology_script(topology_params)
            # logger.info(f"[Topology] Raw topology bundle (length={len(raw) if raw else 0})")
            # logger.info(f"[Topology] Raw topology bundle content:\n{raw}")

            bundle = json.loads(raw) if isinstance(raw, str) else (raw or {})
            messages = bundle.get("messages") or []

            if not messages:
                # logger.warning("[Topology] No frontend messages in bundle, skipping")
                # 仍然标记完成，避免反复尝试
                self._session_topology_fetched.add(cache_key)
                return False

            for idx, msg in enumerate(messages):
                try:
                    serialized = json.dumps(msg, ensure_ascii=False)
                    # logger.info(
                    #     f"[Topology] Emitting message {idx + 1}/{len(messages)} (answerType={msg.get('answerType')})"
                    # )
                    on_token(serialized)
                except Exception as emit_err:
                    # 保留错误日志，便于排查发送失败
                    logger.warning(f"[Topology] Failed to emit message {idx + 1}: {emit_err}")

            self._session_topology_fetched.add(cache_key)
            return True
        except Exception as e:
            # 保留异常日志，便于排查拓扑获取失败
            logger.exception(f"[Topology] Failed to fetch/emit topology: {e}")
            # 标记完成避免阻塞，让后续步骤继续执行
            self._session_topology_fetched.add(cache_key)
            return False

    def _override_currentstep(self, raw, forced_step):
        """把 stepCommand/stepContent 等消息的 currentStep 字段强制改成
        SKILL.md 解析得到的步骤号 forced_step，避免脚本侧硬编码 step 值
        与 SKILL.md 不一致导致前端展示错位。

        - raw 可以是 dict 或 JSON 字符串
        - forced_step 为 None 时不做改写，直接返回字符串形式
        - 返回值始终为 JSON 字符串，便于直接 on_token 发送
        """
        try:
            if isinstance(raw, str):
                obj = json.loads(raw)
            elif isinstance(raw, dict):
                obj = raw
            else:
                return raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        except Exception:
            return raw if isinstance(raw, str) else str(raw)

        if forced_step is not None and isinstance(obj, dict):
            try:
                obj["currentStep"] = int(forced_step)
            except Exception:
                obj["currentStep"] = forced_step
        return json.dumps(obj, ensure_ascii=False)

    def _emit_step_bundle_if_present(self, raw_result, on_token, forced_step=None) -> tuple[bool, str]:
        """检测 step_executor 返回的结果中是否带 stepBundle（含 stepCommand+stepContent
        的复合包）。如果带：
            - 把 stepBundle 中除最后一条以外的所有消息（通常是 stepCommand）
              立即通过 on_token 推送给前端；
            - 返回 (True, last_message_json) —— last_message_json 是 bundle
              中最后一条消息（通常是 stepContent），由上层负责发送 step_marker
              并 on_token 推送。
        如果没带 stepBundle：返回 (False, original_result_str)。

        这样既保证了 stepCommand 在 stepContent 之前到达前端（顺序固定），
        又复用了原有的 stepContent 推进逻辑（answerType/nextStep 等）。
        """
        if raw_result is None:
            return False, ""

        result_str = raw_result if isinstance(raw_result, str) else str(raw_result)

        try:
            parsed = json.loads(result_str) if isinstance(result_str, str) else None
        except Exception:
            return False, result_str

        if not isinstance(parsed, dict):
            return False, result_str

        bundle_messages = parsed.get("stepBundle")
        if not isinstance(bundle_messages, list) or not bundle_messages:
            return False, result_str

        # 先推送除最后一条之外的所有 bundle 消息（stepCommand 等）
        # 注意：这里不 sleep——stepCommand 和 stepContent 应当连续到达前端，
        # 由上层调用方在 stepContent 推送之后统一 sleep 给打字机留时间。
        if on_token:
            for idx, msg in enumerate(bundle_messages[:-1]):
                try:
                    # 强制把 currentStep 改成 SKILL.md 的步骤号
                    serialized = self._override_currentstep(msg, forced_step)
                    logger.info(
                        f"[StepBundle] Emitting bundle message {idx + 1}/{len(bundle_messages)} "
                        f"(answerType={msg.get('answerType') if isinstance(msg, dict) else 'unknown'}, "
                        f"forced_step={forced_step})"
                    )
                    on_token(serialized)
                except Exception as emit_err:
                    logger.warning(f"[StepBundle] Failed to emit bundle message {idx + 1}: {emit_err}")

        # 最后一条交给调用方，仍按原 stepContent 流程发送（含 [STEP_START] 标记）
        last_msg = bundle_messages[-1]
        last_str = self._override_currentstep(last_msg, forced_step)
        return True, last_str

    def _extract_step_commands_from_skill(self, instructions: str, step_name_to_number: dict[str, int]) -> dict[int, list[str]]:
        """
        Parse SKILL.md instructions and extract `commands` field from each step's
        tool params JSON block. Returns a dict mapping step_number -> list of command templates.
        
        Each step in SKILL.md is structured as:
            ### **第N步：step name**  (N 可以是阿拉伯数字 1/2/3 或中文数字 一/二/三)
            [STEP_START]step name[STEP_END]
            ...
            ```json
            {
              ...
              "params": {
                "commands": [
                  "cmd template 1",
                  "cmd template 2"
                ],
                ...
              }
            }
            ```
        """
        import re
        result: dict[int, list[str]] = {}
        
        # 中文数字到阿拉伯数字映射
        cn_num_map = {
            '一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
            '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
            '十一': 11, '十二': 12, '十三': 13, '十四': 14, '十五': 15,
        }
        
        def parse_step_num(arabic: str, chinese: str, english: str) -> int | None:
            if arabic:
                try:
                    return int(arabic)
                except (TypeError, ValueError):
                    return None
            if chinese:
                return cn_num_map.get(chinese)
            if english:
                try:
                    return int(english)
                except (TypeError, ValueError):
                    return None
            return None
        
        # Split instructions into step sections using step number headers
        # Match "### **第N步" or "### **第中文步" or "### Step N" headers
        section_pattern = r'###\s*\*?\*?(?:第(\d+)步|第([一二三四五六七八九十]{1,3})步|Step\s+(\d+))'
        section_matches = list(re.finditer(section_pattern, instructions))
        
        for i, match in enumerate(section_matches):
            step_num = parse_step_num(match.group(1), match.group(2), match.group(3))
            if step_num is None:
                continue
            
            # Determine section range (from this header to next header or EOF)
            section_start = match.end()
            section_end = section_matches[i + 1].start() if i + 1 < len(section_matches) else len(instructions)
            section_text = instructions[section_start:section_end]
            
            # Find the first ```json ... ``` block in this section
            json_block_match = re.search(r'```json\s*(\{.*?\})\s*```', section_text, re.DOTALL)
            if not json_block_match:
                continue
            
            json_text = json_block_match.group(1)
            
            # Try to parse JSON
            try:
                parsed = json.loads(json_text)
            except json.JSONDecodeError:
                # SKILL.md may contain placeholders like <设备IP> which are invalid JSON
                # Use regex to extract commands list directly
                commands_match = re.search(r'"commands"\s*:\s*\[(.*?)\]', json_text, re.DOTALL)
                if commands_match:
                    cmds_text = commands_match.group(1)
                    # Extract each quoted string
                    cmds = re.findall(r'"([^"]+)"', cmds_text)
                    if cmds:
                        result[step_num] = cmds
                continue
            
            # Extract commands from parsed JSON
            params = parsed.get("params", {}) if isinstance(parsed, dict) else {}
            commands = params.get("commands")
            if isinstance(commands, list) and commands:
                result[step_num] = [str(c) for c in commands]
        
        return result

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
        
        # Extract step numbers from skill instructions to match SKILL.md definitions
        # Pattern to match "第X步：" or "第中文步：" or "Step X:" format
        step_number_pattern = r'第(\d+)步[：:]|第([一二三四五六七八九十]{1,3})步[：:]|Step\s+(\d+)[：:]'
        step_number_matches = list(re.finditer(step_number_pattern, skill.instructions))
        
        # 中文数字到阿拉伯数字映射
        _cn_num_map = {
            '一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
            '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
            '十一': 11, '十二': 12, '十三': 13, '十四': 14, '十五': 15,
        }
        
        def _to_step_num(arabic: str, chinese: str, english: str):
            if arabic:
                try:
                    return int(arabic)
                except (TypeError, ValueError):
                    return None
            if chinese:
                return _cn_num_map.get(chinese)
            if english:
                try:
                    return int(english)
                except (TypeError, ValueError):
                    return None
            return None
        
        if steps and step_number_matches:
            # Map step names to their actual step numbers from SKILL.md
            step_name_to_number = {}
            for match in step_number_matches:
                step_num = _to_step_num(match.group(1), match.group(2), match.group(3))
                if step_num is None:
                    continue
                # Find the step name after this marker
                after_marker = skill.instructions[match.end():]
                # Look for the next STEP_START marker
                next_step_match = re.search(r'\[STEP_START\](.*?)\[STEP_END\]', after_marker)
                if next_step_match:
                    step_name = next_step_match.group(1).strip()
                    step_name_to_number[step_name] = step_num
            
            # Store steps with their actual numbers
            self._current_skill_steps = steps
            self._current_skill_step_numbers = [step_name_to_number.get(step, idx + 1) for idx, step in enumerate(steps)]
            logger.info("[SkillSteps] Extracted %d steps from skill '%s': %s", len(steps), skill_name, steps[:3])
            logger.info("[SkillSteps] Step numbers: %s", self._current_skill_step_numbers[:3])
            
            # Extract commands for each step from SKILL.md tool params JSON blocks
            self._current_skill_step_commands = self._extract_step_commands_from_skill(skill.instructions, step_name_to_number)
            logger.info("[SkillSteps] Extracted commands per step: %s", self._current_skill_step_commands)
        elif steps:
            # Fallback: use sequential numbering if no step numbers found
            self._current_skill_steps = steps
            self._current_skill_step_numbers = list(range(1, len(steps) + 1))
            logger.info("[SkillSteps] Extracted %d steps from skill '%s': %s", len(steps), skill_name, steps[:3])
            # Extract commands with sequential numbering
            self._current_skill_step_commands = self._extract_step_commands_from_skill(
                skill.instructions, {s: i+1 for i, s in enumerate(steps)}
            )
        else:
            self._current_skill_steps = []
            self._current_skill_step_numbers = []
            self._current_skill_step_commands = {}
        
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
        has_troubleshooting_keywords = any(kw in user_input_lower for kw in troubleshooting_keywords)
        
        # Also detect IP address patterns which might indicate routing troubleshooting
        import re
        ip_pattern = r'\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?\b'
        has_ip_address = bool(re.search(ip_pattern, user_input)) if isinstance(user_input, str) else False
        
        # If either troubleshooting keywords or IP address found, consider it troubleshooting intent
        if has_troubleshooting_keywords or has_ip_address:
            logger.info(f"[Agent] Detected troubleshooting intent, enabling text suppression")
            logger.info(f"[Agent]   has_troubleshooting_keywords: {has_troubleshooting_keywords}")
            logger.info(f"[Agent]   has_ip_address: {has_ip_address}")
            suppress_conversational_text = True
        
        # Check if we have an active skill with pending steps and user provided IP info
        # If so, skip LLM and directly execute the next step
        has_active_skill = hasattr(self, '_current_skill_steps') and self._current_skill_steps
        if has_active_skill and has_ip_address:
            logger.info(f"[Agent] Found active skill with steps and user provided IP info, skipping LLM, directly executing steps")
            
            # Import required classes
            from .llm.response import MockFunction, MockToolCall
            
            # Create forced tool call for step execution
            forced_tool_call = self._create_forced_tool_call()
            if forced_tool_call:
                logger.info(f"[Agent] Force-created tool call for step execution: {forced_tool_call.function.name}")
                
                # Execute the tool call directly
                try:
                    step_result = self._execute_tool_call(forced_tool_call)
                    
                    # Log and process the result
                    result_str = str(step_result) if step_result else "None"
                    logger.info(f"[Agent] Step execution result: {result_str}")
                    
                    # Parse the result
                    answer_type = None
                    parsed_result = None
                    try:
                        import json as _json
                        if isinstance(step_result, str):
                            parsed_result = _json.loads(step_result)
                            answer_type = parsed_result.get("answerType")
                            logger.info(f"[Agent] Parsed step result - answer_type: {answer_type}")
                    except Exception as e:
                        logger.error(f"[Agent] Failed to parse step result: {e}")
                    
                    # Update step index
                    session_id_for_step = self.session_id or "default"
                    self._session_step_indices[session_id_for_step] = 1
                    
                    # Cache device_info and destination_network from forced_tool_call args for reuse
                    try:
                        forced_args = json.loads(forced_tool_call.function.arguments) if isinstance(forced_tool_call.function.arguments, str) else forced_tool_call.function.arguments
                        forced_params = forced_args.get("params", {}) if forced_args else {}
                        forced_device_info = forced_params.get("device_info", {})
                        forced_dest = forced_params.get("destination_network", "")
                        if forced_device_info:
                            self._session_device_info[session_id_for_step] = forced_device_info
                            logger.info(f"[Agent] Cached device_info for session {session_id_for_step}: {forced_device_info}")
                        if forced_dest and forced_dest != "0.0.0.0/0":
                            self._session_destination_network[session_id_for_step] = forced_dest
                            logger.info(f"[Agent] Cached destination_network for session {session_id_for_step}: {forced_dest}")
                    except Exception as _e:
                        logger.warning(f"[Agent] Failed to cache device_info/destination_network: {_e}")
                    
                    # Send result to frontend and continue with next steps
                    if answer_type in ("stepContent", "stepAnalysis", "stepCommand"):
                        # Get current step info
                        current_step_idx = self._session_step_indices.get(session_id_for_step, 0)
                        if current_step_idx > 0 and current_step_idx <= len(self._current_skill_steps):
                            current_step_name = self._current_skill_steps[current_step_idx - 1]
                        else:
                            current_step_name = self._current_skill_steps[0] if self._current_skill_steps else ""

                        # 统一从 SKILL.md 解析的 _current_skill_step_numbers 取真实步骤号
                        # 该值是 stepName / stepCommand / stepContent 三条消息的唯一 currentStep 源
                        _idx_for_num = current_step_idx - 1 if current_step_idx > 0 else 0
                        if 0 <= _idx_for_num < len(self._current_skill_step_numbers):
                            _skill_step_num = self._current_skill_step_numbers[_idx_for_num]
                        else:
                            _skill_step_num = self._current_skill_step_numbers[0] if self._current_skill_step_numbers else 1

                        # 先把当前步骤名 [STEP_START]...[STEP_END] 推送给前端，
                        # 让前端先切换到新 step（清空旧 step 残留的展示），然后
                        # 再推送本步骤的 stepCommand / stepContent，保证它们都
                        # 挂载到正确的 step 下。
                        if on_token:
                            step_marker = f"[STEP_START:{_skill_step_num}]{current_step_name}[STEP_END]"
                            logger.info(f"[SkillSteps] Sending step notification: step={_skill_step_num} name={current_step_name}")
                            on_token(step_marker)

                        # 如果 step_executor 返回了 stepBundle（含 stepCommand 回显
                        # + stepContent 分析），先把 stepCommand 等附加消息推送给
                        # 前端，再用最后一条 stepContent 字符串作为后续 result_str
                        # forced_step 强制 stepCommand/stepContent 的 currentStep 与 SKILL.md 一致
                        bundle_handled, result_str = self._emit_step_bundle_if_present(step_result, on_token, forced_step=_skill_step_num)
                        if bundle_handled:
                            logger.info("[Agent] stepBundle detected, stepCommand pushed before stepContent")

                        # Send stepContent result to frontend (step_marker 已在上方发送)
                        if on_token:
                            # 兜底：再次确保 currentStep 与 SKILL.md 一致
                            result_str = self._override_currentstep(result_str, _skill_step_num)
                            logger.info("[Agent] Sending stepContent to frontend")
                            on_token(result_str)
                            # stepContent（总结信息）推送之后 sleep 3 秒，留出
                            # 前端打字机动画时间
                            try:
                                import time as _time
                                _time.sleep(3)
                            except Exception:
                                pass
                        
                        # Continue executing next steps based on nextStep field
                        from .llm.response import MockFunction, MockToolCall
                        while True:
                            # Parse next_step from result
                            next_step_name = None
                            next_step_number = None
                            
                            try:
                                current_parsed = json.loads(step_result) if isinstance(step_result, str) else step_result
                                if current_parsed:
                                    # Check both old format (analysis.next_step) and new format (nextStep)
                                    analysis_next_step = None
                                    if "analysis" in current_parsed:
                                        analysis_next_step = current_parsed["analysis"].get("next_step")
                                    if not analysis_next_step:
                                        analysis_next_step = current_parsed.get("nextStep")
                                    
                                    if analysis_next_step:
                                        match = re.search(r'step(\d+)', analysis_next_step)
                                        if match:
                                            next_step_number = int(match.group(1))
                                            try:
                                                next_step_idx = self._current_skill_step_numbers.index(next_step_number)
                                                if 0 <= next_step_idx < len(self._current_skill_steps):
                                                    next_step_name = self._current_skill_steps[next_step_idx]
                                                    logger.info(f"[Agent] Jumping to step {next_step_number}: {next_step_name}")
                                            except ValueError:
                                                logger.warning(f"[Agent] Step number {next_step_number} not found in _current_skill_step_numbers")
                            except Exception as e:
                                logger.warning(f"[Agent] Failed to parse next_step: {e}")
                            
                            # If no next_step from analysis, use sequential execution
                            if not next_step_name:
                                current_step_idx = self._session_step_indices.get(session_id_for_step, 0)
                                if current_step_idx >= len(self._current_skill_steps):
                                    logger.info(f"[Agent] All steps completed")
                                    self._current_skill_steps = []
                                    self._current_skill_step_numbers = []
                                    self._session_step_indices.pop(session_id_for_step, None)
                                    return "所有故障排查步骤执行完成。"
                                next_step_name = self._current_skill_steps[current_step_idx]
                                next_step_number = self._current_skill_step_numbers[current_step_idx] if current_step_idx < len(self._current_skill_step_numbers) else current_step_idx + 1
                                logger.info(f"[Agent] Executing next step sequentially: step {next_step_number}: {next_step_name}")
                            
                            # Check if this is the final step
                            max_step_number = max(self._current_skill_step_numbers) if self._current_skill_step_numbers else 7
                            if next_step_number == max_step_number or "流程结束" in next_step_name or "总结" in next_step_name:
                                logger.info(f"[Agent] Reached final step {next_step_number}: {next_step_name}")
                                self._current_skill_steps = []
                                self._current_skill_step_numbers = []
                                self._session_step_indices.pop(session_id_for_step, None)
                                summary_text = (
                                    "静态路由故障排查完成。<br/><br/>"
                                    "诊断结果：已完成所有故障排查步骤。<br/><br/>"
                                    "建议：根据各步骤分析结果进行相应配置调整。<br/>"
                                )
                                completion_result = {
                                    "answerType": "conversation",
                                    "contextEnd": "false",
                                    "contextId": "",
                                    "currentStep": next_step_number,
                                    "message": summary_text,
                                    "questionNo": "",
                                    "sessionId": self.session_id or "default"
                                }
                                completion_json = json.dumps(completion_result, ensure_ascii=False)
                                # 推送一条 conversation 总结消息给前端
                                if on_token:
                                    logger.info("[Agent] Emitting final conversation summary to frontend")
                                    on_token(completion_json)
                                return completion_json
                            
                            # Build next step params
                            step_to_analysis_type = {
                                1: "check_route",
                                2: "check_nexthop",
                                3: "check_mask",
                                5: "check_interface",
                                6: "check_bfd",
                                7: "check_priority"
                            }
                            
                            if next_step_number not in step_to_analysis_type:
                                error_msg = f"[Agent] ERROR: Step number {next_step_number} not found in mapping!"
                                logger.error(error_msg)
                                raise ValueError(error_msg)
                            
                            next_analysis_type = step_to_analysis_type[next_step_number]
                            
                            # Get skill_args from forced_tool_call
                            skill_args = json.loads(forced_tool_call.function.arguments) if isinstance(forced_tool_call.function.arguments, str) else {}
                            
                            # Extract device_info and destination_network from step_executor result
                            # Priority: cached session values > parsed_result > skill_args > defaults
                            device_info = self._session_device_info.get(session_id_for_step, {})
                            destination_network = self._session_destination_network.get(session_id_for_step, "")
                            try:
                                if parsed_result and parsed_result.get("device_info"):
                                    new_device_info = parsed_result["device_info"]
                                    if new_device_info:
                                        device_info = new_device_info
                                if parsed_result and parsed_result.get("destination_network"):
                                    new_dest = parsed_result["destination_network"]
                                    if new_dest:
                                        destination_network = new_dest
                            except:
                                pass
                            # Fall back to skill_args if still empty
                            if not device_info:
                                device_info = skill_args.get("device_info", {}) or {}
                            if not destination_network:
                                destination_network = skill_args.get("destination_network", "") or "0.0.0.0/0"
                            
                            # Cache for reuse in subsequent steps
                            if device_info:
                                self._session_device_info[session_id_for_step] = device_info
                            if destination_network and destination_network != "0.0.0.0/0":
                                self._session_destination_network[session_id_for_step] = destination_network
                            
                            # Get commands from SKILL.md for this step
                            skill_commands = self._current_skill_step_commands.get(next_step_number, [])
                            
                            next_step_params = {
                                "step_name": next_step_name,
                                "step_number": next_step_number,
                                "skill_name": skill_args.get("skill_name", "static-troubleshooting"),
                                "analysis_type": next_analysis_type,
                                "commands": skill_commands,
                                "destination_network": destination_network,
                                "device_info": device_info,
                                "context_id": getattr(self, "frontend_context_id", None) or skill_args.get("context_id", ""),
                                "question_no": getattr(self, "frontend_question_no", None) or skill_args.get("question_no", ""),
                                "session_id": getattr(self, "frontend_session_id", None) or self.session_id or "default"
                            }
                            
                            # Create next step tool call
                            next_step_args_json = json.dumps({
                                "script_name": "step_executor.py",
                                "mode": "build_and_execute",
                                "params": next_step_params
                            })
                            
                            next_step_tool_call = MockToolCall(
                                id=f"call_step_{next_step_number}_{int(time.time())}",
                                function=MockFunction(name="execute_step_script", arguments=next_step_args_json),
                                type="function"
                            )
                            
                            # Execute next step
                            logger.info(f"[Agent] Executing next step: {next_step_name}")
                            next_step_result = self._execute_tool_call(next_step_tool_call)
                            
                            # Parse next step result
                            next_answer_type = None
                            try:
                                next_parsed = json.loads(next_step_result) if isinstance(next_step_result, str) else next_step_result
                                next_answer_type = next_parsed.get("answerType") if next_parsed else None
                            except:
                                next_answer_type = None
                            
                            # If next step requires user input (conversation), pause and don't send step name
                            if next_answer_type == "conversation":
                                logger.info(f"[Agent] Next step returned conversation (info request), pausing loop")
                                if on_token:
                                    on_token(str(next_step_result))
                                return str(next_step_result)

                            # 先发送 [STEP_START]...[STEP_END] 切换前端到新 step
                            # next_step_number 来自 _current_skill_step_numbers（SKILL.md）
                            if on_token:
                                step_marker = f"[STEP_START:{next_step_number}]{next_step_name}[STEP_END]"
                                on_token(step_marker)

                            # 若返回 stepBundle，先推送 stepCommand 等附加消息
                            # forced_step 强制 stepCommand/stepContent 的 currentStep 与 SKILL.md 一致
                            bundle_handled, next_step_result = self._emit_step_bundle_if_present(next_step_result, on_token, forced_step=next_step_number)
                            if bundle_handled:
                                logger.info("[Agent] stepBundle detected on next step, stepCommand pushed before stepContent")

                            # Send next step stepContent to frontend (step_marker 已在上方发送)
                            if on_token:
                                # 兜底：再次确保 currentStep 与 SKILL.md 一致
                                next_step_result = self._override_currentstep(next_step_result, next_step_number)
                                on_token(str(next_step_result))
                                try:
                                    import time as _time
                                    _time.sleep(3)
                                except Exception:
                                    pass
                            
                            # Update step index and continue
                            self._session_step_indices[session_id_for_step] = self._session_step_indices.get(session_id_for_step, 0) + 1
                            step_result = next_step_result
                            parsed_result = next_parsed
                    
                    elif answer_type == "conversation":
                        # Info request, send to frontend
                        if on_token:
                            logger.info("[Agent] Sending conversation message to frontend")
                            on_token(result_str)
                    
                    return
                
                except Exception as e:
                    logger.error(f"[Agent] Failed to execute step: {e}")
                    import traceback
                    logger.error(f"[Agent] Step execution traceback: {traceback.format_exc()}")
                    # Return error message instead of continuing to LLM
                    error_result = {
                        "answerType": "conversation",
                        "contextEnd": "false",
                        "contextId": "",
                        "currentStep": 0,
                        "message": f"步骤执行失败: {str(e)}",
                        "questionNo": "",
                        "sessionId": self.session_id or "default"
                    }
                    error_str = json.dumps(error_result, ensure_ascii=False)
                    if on_token:
                        on_token(error_str)
                    return error_str
        
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
                    logger.info("[Agent] Content preview: %s", repr(message.content or ""))
                    
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
                            
                            # Log forced tool execution result with proper handling
                            try:
                                if result is None:
                                    logger.info("[Agent] Forced tool execution result: None")
                                else:
                                    result_str = str(result)
                                    logger.info(f"[Agent] Forced tool execution result type: {type(result).__name__}")
                                    logger.info(f"[Agent] Forced tool execution result length: {len(result_str)} chars")
                                    logger.info(f"[Agent] Forced tool execution result:\n{result_str}")
                            except Exception as e:
                                logger.warning(f"[Agent] Failed to log forced tool result: {e}")
                            
                            # Check if skill was successfully activated
                            result_lower = str(result).lower() if result else ""
                            skill_activated = "activated" in result_lower or "already active" in result_lower
                            
                            # Also check if result is a step execution result (stepContent, stepAnalysis, etc.)
                            parsed_result = None
                            step_result_type = None
                            try:
                                import json as _json
                                if isinstance(result, str):
                                    parsed_result = _json.loads(result)
                                    step_result_type = parsed_result.get("answerType") if parsed_result else None
                            except:
                                pass
                            
                            logger.info(f"[Agent] Skill activation check: skill_activated={skill_activated}, step_result_type={step_result_type}")
                            
                            # Check if this is a step execution result (not skill activation)
                            is_step_result = step_result_type in ("stepContent", "stepAnalysis", "stepCommand", "stepInfoRequest", "stepError")
                            
                            if skill_activated or is_step_result:
                                # Skill is now active or we have step execution result, extract steps and start/execute steps
                                if skill_activated:
                                    logger.info("[Agent] Skill activation successful, starting step execution...")
                                
                                # Extract steps from the activated skill
                                has_steps = hasattr(self, '_current_skill_steps') and self._current_skill_steps
                                logger.info(f"[Agent] Has steps attribute: {has_steps}")
                                
                                if has_steps:
                                    logger.info(f"[Agent] Found {len(self._current_skill_steps)} steps to execute")
                                    logger.info(f"[Agent] Steps list: {self._current_skill_steps}")
                                    
                                    first_step_name = self._current_skill_steps[0]
                                    
                                    # ── Step 0: 获取网络拓扑图（根据技能配置决定是否执行）──────────────────────
                                    # 在执行第一步排查脚本之前，先检查技能配置是否需要获取拓扑图
                                    need_fetch_topology = False
                                    try:
                                        # 从 forced_tool_call 参数中获取技能名称
                                        skill_args_for_topo = json.loads(forced_tool_call.function.arguments) if isinstance(forced_tool_call.function.arguments, str) else (forced_tool_call.function.arguments or {})
                                        skill_name_for_topo = skill_args_for_topo.get("skill_name", "")
                                        
                                        # 获取技能的 metadata 配置
                                        if skill_name_for_topo:
                                            skill_meta = self._registry._get_skill_metadata(skill_name_for_topo)
                                            if skill_meta and hasattr(skill_meta, 'fetch_topology'):
                                                need_fetch_topology = skill_meta.fetch_topology
                                                logger.info(f"[Agent] Skill '{skill_name_for_topo}' fetch_topology config: {need_fetch_topology}")
                                    except Exception as e:
                                        logger.warning(f"[Agent] Failed to check fetch_topology config: {e}")
                                        # 默认不获取拓扑图
                                        need_fetch_topology = False
                                    
                                    if need_fetch_topology:
                                        # 在执行第一步排查脚本之前，先调用第三方拓扑接口
                                        # 并按约定顺序向前端发送三条 SSE 消息
                                        # 优先使用 web 层注入的前端上下文（contextId/questionNo/sessionId），
                                        # 这样发给前端的 SSE 消息能与前端原始请求匹配上
                                        topo_session_id = getattr(self, "frontend_session_id", None) or self.session_id or "default"
                                        topo_context_id = getattr(self, "frontend_context_id", None) or skill_args_for_topo.get("context_id", "")
                                        topo_question_no = getattr(self, "frontend_question_no", None) or skill_args_for_topo.get("question_no", "")
                                        self._fetch_and_emit_topology(
                                            session_id=topo_session_id,
                                            context_id=topo_context_id,
                                            question_no=topo_question_no,
                                            on_token=on_token,
                                        )
                                    
                                    # If we already have a step result from forced tool call (is_step_result),
                                    # skip re-executing the first step to avoid duplicate execution
                                    if is_step_result:
                                        logger.info("[Agent] Already have step result from forced tool call, skipping re-execution")
                                        step_result = result  # Use the already executed result
                                    else:
                                        # IMPORTANT: Don't continue the loop (which would call LLM again)
                                        # Instead, directly execute the first step script
                                        logger.info(f"[Agent] Executing first step: {first_step_name}")
                                        
                                        # Create a mock tool call for execute_step_script
                                        from .llm.response import MockFunction, MockToolCall
                                        
                                        # Extract skill name from forced_tool_call arguments
                                        skill_args = json.loads(forced_tool_call.function.arguments) if isinstance(forced_tool_call.function.arguments, str) else forced_tool_call.function.arguments
                                        skill_name = skill_args.get("skill_name", "static-troubleshooting")
                                        logger.info(f"[Agent] Skill name: {skill_name}")
                                        
                                        # Build parameters for the step script
                                        # execute_step_script expects: script_name, mode, params
                                        # Map step name to analysis_type
                                        step_to_analysis_type = {
                                            "检查全局路由表中是否存在该静态路由": "check_route",
                                            "检查下一跳地址可达性": "check_nexthop",
                                            "检查路由掩码与最长匹配原则": "check_mask",
                                            "检查出接口物理与协议状态": "check_interface",
                                            "检查BFD或NQA配置与状态": "check_bfd",
                                            "检查本静态路由的优先级": "check_priority"
                                        }
                                        analysis_type = step_to_analysis_type.get(first_step_name, "check_route")
                                        logger.info(f"[Agent] Analysis type: {analysis_type}")
                                        
                                        # Extract destination network from context or use default
                                        destination_network = "0.0.0.0/0"  # Default
                                        for msg in self.messages[-10:]:
                                            if msg.get("role") == "user":
                                                content = msg.get("content", "")
                                                # Try to extract CIDR notation or plain IP address
                                                import re
                                                
                                                # First, try to find IP after "目的网段" or "目标网段" keywords
                                                dest_keyword_match = re.search(r'(目的网段|目标网段|目的IP)\s*[：:]?\s*([0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}(?:/[0-9]{1,2})?)', content)
                                                if dest_keyword_match:
                                                    destination_network = dest_keyword_match.group(2)
                                                    logger.info(f"[Agent] Extracted destination_network after keyword: {destination_network}")
                                                    break
                                                
                                                # If no keyword found, find the last IP address in the message (usually destination)
                                                ip_matches = re.findall(r'([0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}(?:/[0-9]{1,2})?)', content)
                                                if ip_matches:
                                                    # Use the last IP as destination (first is often device IP)
                                                    destination_network = ip_matches[-1]
                                                    logger.info(f"[Agent] Extracted last IP as destination_network: {destination_network}")
                                                    break
                                       
                                        # Keep destination network as-is without adding mask
                                        logger.info(f"[Agent] Destination network: {destination_network}")
                                        
                                        # Extract device info from context
                                        device_info = self._extract_device_info_from_context()
                                        logger.info(f"[Agent] Extracted device info: {device_info}")
                                        
                                        # Get actual step number from SKILL.md
                                        actual_step_number = self._current_skill_step_numbers[0] if self._current_skill_step_numbers else 1
                                        logger.info(f"[Agent] Actual step number from SKILL.md: {actual_step_number}")
                                        
                                        # Get commands from SKILL.md for this step
                                        skill_commands = self._current_skill_step_commands.get(actual_step_number, [])
                                        logger.info(f"[Agent] Commands from SKILL.md for step {actual_step_number}: {skill_commands}")
                                        
                                        step_params = {
                                            "step_name": first_step_name,
                                            "step_number": actual_step_number,
                                            "skill_name": skill_name,
                                            "analysis_type": analysis_type,
                                            "commands": skill_commands,
                                            "destination_network": destination_network,
                                            "device_info": device_info,
                                            # 优先使用 web 层注入的前端上下文，避免后端自造的 contextId/questionNo
                                            "context_id": getattr(self, "frontend_context_id", None) or skill_args.get("context_id", ""),
                                            "question_no": getattr(self, "frontend_question_no", None) or skill_args.get("question_no", ""),
                                            "session_id": getattr(self, "frontend_session_id", None) or self.session_id or "default"
                                        }
                                        logger.info(f"[Agent] Step params built successfully")
                                        
                                        # Arguments must match execute_step_script signature: (script_name, mode, params)
                                        step_args_json = json.dumps({
                                            "script_name": "step_executor.py",
                                            "mode": "build_and_execute",
                                            "params": step_params
                                        })
                                        logger.info(f"[Agent] Step arguments JSON prepared")
                                        
                                        step_tool_call = MockToolCall(
                                            id=f"call_step_1_{int(time.time())}",
                                            function=MockFunction(name="execute_step_script", arguments=step_args_json),
                                            type="function"
                                        )

                                        # Execute the step script
                                        logger.info(f"[Agent] Executing step script: execute_step_script with params: {step_params}")
                                        step_result = self._execute_tool_call(step_tool_call)
                                        logger.info(f"[Agent] Step script execution completed")
                                        
                                        # Cache device_info and destination_network for reuse in subsequent steps
                                        session_id_for_cache = self.session_id or "default"
                                        if device_info:
                                            self._session_device_info[session_id_for_cache] = device_info
                                            logger.info(f"[Agent] Cached device_info for session {session_id_for_cache}: {device_info}")
                                        if destination_network and destination_network != "0.0.0.0/0":
                                            self._session_destination_network[session_id_for_cache] = destination_network
                                            logger.info(f"[Agent] Cached destination_network for session {session_id_for_cache}: {destination_network}")
                                    
                                    # Log step result with proper type handling
                                    try:
                                        if step_result is None:
                                            logger.info("[Agent] Step execution result: None")
                                        else:
                                            result_str = str(step_result)
                                            logger.info(f"[Agent] Step execution result type: {type(step_result).__name__}")
                                            logger.info(f"[Agent] Step execution result length: {len(result_str)} chars")
                                            logger.info(f"[Agent] Step execution result:\n{result_str}")
                                    except Exception as e:
                                        logger.warning(f"[Agent] Failed to log step result: {e}")
                                    
                                    # Parse step result to determine response
                                    answer_type = None
                                    user_message = ""
                                    parsed = None
                                    try:
                                        import json as _json
                                        if isinstance(step_result, str):
                                            parsed = _json.loads(step_result)
                                            answer_type = parsed.get("answerType")
                                            user_message = parsed.get("message", parsed.get("user_message", ""))
                                            logger.info(f"[Agent] Parsed step result - answer_type: {answer_type}, user_message: {user_message if user_message else 'None'}")
                                    except Exception as e:
                                        logger.warning(f"[Agent] Failed to parse step result: {e}")
                                    
                                    # Send step notification and command to frontend
                                    # Send step notification and analysis result to frontend
                                    logger.info(f"[Agent] Checking if on_token is available: {on_token is not None}")
                                    if on_token:
                                        logger.info(f"[Agent] on_token is available, sending stepContent")

                                        # 对正式排查步骤（stepContent / stepAnalysis），先发送
                                        # [STEP_START]...[STEP_END] 切换前端到新 step，再推送
                                        # stepCommand（来自 bundle），最后推送 stepContent。
                                        # 统一从 SKILL.md 解析的 _current_skill_step_numbers 取首步号
                                        _skill_first_step_num = self._current_skill_step_numbers[0] if self._current_skill_step_numbers else 1
                                        if answer_type in ("stepContent", "stepAnalysis"):
                                            step_marker = f"[STEP_START:{_skill_first_step_num}]{first_step_name}[STEP_END]"
                                            logger.info(f"[SkillSteps] Sending proactive step notification for step {_skill_first_step_num}: {first_step_name}")
                                            on_token(step_marker)

                                        # 若 step_executor 返回 stepBundle，先把 stepCommand 等
                                        # 附加消息推送给前端，再把最后一条 stepContent 当作 step_result
                                        # forced_step 强制 stepCommand/stepContent 的 currentStep 与 SKILL.md 一致
                                        bundle_handled, step_result = self._emit_step_bundle_if_present(step_result, on_token, forced_step=_skill_first_step_num)
                                        if bundle_handled:
                                            logger.info("[Agent] stepBundle detected, stepCommand pushed before stepContent")

                                        # Send the appropriate message based on answer_type
                                        if answer_type == "stepContent":
                                            # step_marker 已在上方发送
                                            # 兜底：对排查步骤的 stepContent 再次确保 currentStep 与 SKILL.md 一致
                                            step_result = self._override_currentstep(step_result, _skill_first_step_num)
                                            logger.info("[Agent] Sending stepContent to frontend")
                                            on_token(step_result)
                                            try:
                                                import time as _time
                                                _time.sleep(3)
                                            except Exception:
                                                pass
                                        elif answer_type == "stepAnalysis":
                                            # step_marker 已在上方发送
                                            step_result = self._override_currentstep(step_result, _skill_first_step_num)
                                            logger.info("[Agent] Sending stepAnalysis to frontend")
                                            on_token(step_result)
                                        elif answer_type == "stepCommand":
                                            # Fallback: if still getting stepCommand, log warning
                                            step_result = self._override_currentstep(step_result, _skill_first_step_num)
                                            logger.warning("[Agent] Received stepCommand instead of stepContent - script may not be executing correctly")
                                            on_token(step_result)
                                        elif answer_type == "conversation":
                                            # For conversation type (info request), don't send step name
                                            # Just send the conversation message directly
                                            logger.info("[Agent] Sending conversation message to frontend - need additional information")
                                            on_token(step_result)
                                        elif answer_type == "stepError":
                                            error_msg = user_message or (parsed.get('message', 'Unknown error') if parsed else 'Unknown error')
                                            logger.warning(f"[Agent] Step execution error: {error_msg}")
                                            on_token(step_result)
                                        elif answer_type is not None:
                                            logger.warning(f"[Agent] Unknown answerType: {answer_type}, sending as-is")
                                            on_token(step_result)
                                        else:
                                            logger.warning("[Agent] No answerType found in step result")

                                    # IMPORTANT: Do NOT return here - we need to continue to next step for stepContent/stepAnalysis
                                    # Update step index after successful execution (not for stepInfoRequest)
                                    if answer_type != "stepInfoRequest":
                                        session_id_for_step = self.session_id or "default"
                                        current_idx = self._session_step_indices.get(session_id_for_step, 0)
                                        self._session_step_indices[session_id_for_step] = current_idx + 1
                                        logger.info(f"[Agent] Session {session_id_for_step}: Updated step index to: {self._session_step_indices[session_id_for_step]}")
                                    
                                    # For stepAnalysis, stepContent, stepCommand, continue to next step automatically
                                    # Only pause for stepInfoRequest (need user input) or stepError
                                    logger.info(f"[Agent] Checking answer_type: {answer_type}, in allowed types: {answer_type in ('stepAnalysis', 'stepContent', 'stepCommand')}")
                                    if answer_type in ("stepAnalysis", "stepContent", "stepCommand"):
                                        logger.info(f"[Agent] answer_type={answer_type}, continuing to next step...")
                                        
                                        # Add messages to history
                                        msg_dump = message.model_dump()
                                        if msg_dump.get("content") is None:
                                            msg_dump["content"] = ""
                                        self.messages.append(msg_dump)
                                        
                                        self.messages.append({
                                            "role": "tool",
                                            "tool_call_id": forced_tool_call.id,
                                            "name": forced_tool_call.function.name,
                                            "content": step_result,
                                        })
                                        
                                        # Directly execute next step without calling LLM
                                        # Continue the step execution loop until all steps done or need user input
                                        from .llm.response import MockFunction, MockToolCall
                                        # Ensure skill_args is defined for the loop (may not be set in is_step_result branch)
                                        try:
                                            skill_args
                                        except NameError:
                                            try:
                                                skill_args = json.loads(forced_tool_call.function.arguments) if isinstance(forced_tool_call.function.arguments, str) else (forced_tool_call.function.arguments or {})
                                            except Exception:
                                                skill_args = {}
                                        try:
                                            skill_name
                                        except NameError:
                                            skill_name = skill_args.get("skill_name", "static-troubleshooting")
                                        while True:
                                            session_id_for_step = self.session_id or "default"
                                            
                                            # Get next step from analysis result if available
                                            next_step_name = None
                                            next_step_number = None
                                            current_step_idx = self._session_step_indices.get(session_id_for_step, 0)
                                            
                                            # Parse current result to get next_step from analysis or nextStep field
                                            try:
                                                current_parsed = json.loads(step_result) if isinstance(step_result, str) else step_result
                                                if current_parsed:
                                                    # Check both old format (analysis.next_step) and new format (nextStep)
                                                    analysis_next_step = None
                                                    if "analysis" in current_parsed:
                                                        analysis_next_step = current_parsed["analysis"].get("next_step")
                                                    # Fall back to new format
                                                    if not analysis_next_step:
                                                        analysis_next_step = current_parsed.get("nextStep")
                                                    
                                                    if analysis_next_step:
                                                        # Convert next_step like "step2" to step index
                                                        match = re.search(r'step(\d+)', analysis_next_step)
                                                        if match:
                                                            next_step_number = int(match.group(1))
                                                            # Find the step name by looking for the step number in _current_skill_step_numbers
                                                            try:
                                                                next_step_idx = self._current_skill_step_numbers.index(next_step_number)
                                                                if 0 <= next_step_idx < len(self._current_skill_steps):
                                                                    next_step_name = self._current_skill_steps[next_step_idx]
                                                                    logger.info(f"[Agent] Jumping to step {next_step_number}: {next_step_name} (from {'analysis.next_step' if 'analysis' in current_parsed else 'nextStep'})")
                                                            except ValueError:
                                                                logger.warning(f"[Agent] Step number {next_step_number} not found in _current_skill_step_numbers")
                                            except Exception as e:
                                                logger.warning(f"[Agent] Failed to parse next_step from analysis: {e}")
                                            
                                            # If no next_step from analysis, use sequential execution
                                            if not next_step_name:
                                                current_step_idx = self._session_step_indices.get(session_id_for_step, 0)
                                                if current_step_idx >= len(self._current_skill_steps):
                                                    logger.info(f"[Agent] All steps completed, exiting step execution loop")
                                                    # Skill execution complete, clear skill state
                                                    # 在清空 _current_skill_step_numbers 之前先取出最大步骤号（来自 SKILL.md）
                                                    _final_step_num = max(self._current_skill_step_numbers) if self._current_skill_step_numbers else 7
                                                    self._current_skill_steps = []
                                                    self._current_skill_step_numbers = []
                                                    self._session_step_indices.pop(session_id_for_step, None)
                                                    # Return completion message in conversation format
                                                    summary_text = (
                                                        "静态路由故障排查完成。<br/><br/>"
                                                        "诊断结果：已完成所有故障排查步骤。<br/><br/>"
                                                        "建议：根据各步骤分析结果进行相应配置调整。<br/>"
                                                    )
                                                    completion_result = {
                                                        "answerType": "conversation",
                                                        "contextEnd": "false",
                                                        "contextId": skill_args.get("context_id", ""),
                                                        "currentStep": _final_step_num,
                                                        "message": summary_text,
                                                        "questionNo": skill_args.get("question_no", ""),
                                                        "sessionId": self.session_id or "default"
                                                    }
                                                    completion_json = json.dumps(completion_result, ensure_ascii=False)
                                                    if on_token:
                                                        logger.info("[Agent] Emitting final conversation summary to frontend (all steps completed)")
                                                        on_token(completion_json)
                                                    return completion_json
                                                next_step_name = self._current_skill_steps[current_step_idx]
                                                # Get actual step number from SKILL.md
                                                next_step_number = self._current_skill_step_numbers[current_step_idx] if current_step_idx < len(self._current_skill_step_numbers) else current_step_idx + 1
                                                logger.info(f"[Agent] Executing next step sequentially: step {next_step_number}: {next_step_name}")
                                            else:
                                                # Find the index of the step with this number
                                                try:
                                                    next_step_idx = self._current_skill_step_numbers.index(next_step_number)
                                                    current_step_idx = next_step_idx
                                                except ValueError:
                                                    # If not found, use the number directly
                                                    current_step_idx = next_step_number - 1
                                                self._session_step_indices[session_id_for_step] = current_step_idx
                                            
                                            # Check if this is the final step (step 7 - 流程结束与总结)
                                            max_step_number = max(self._current_skill_step_numbers) if self._current_skill_step_numbers else 7
                                            if next_step_number == max_step_number or "流程结束" in next_step_name or "总结" in next_step_name:
                                                logger.info(f"[Agent] Reached final step {next_step_number}: {next_step_name}, ending troubleshooting flow")
                                                # Clear skill state
                                                self._current_skill_steps = []
                                                self._current_skill_step_numbers = []
                                                self._session_step_indices.pop(session_id_for_step, None)
                                                # Return completion message in conversation format
                                                summary_text = (
                                                    "静态路由故障排查完成。<br/><br/>"
                                                    "诊断结果：已完成所有故障排查步骤。<br/><br/>"
                                                    "建议：根据各步骤分析结果进行相应配置调整。<br/>"
                                                )
                                                completion_result = {
                                                    "answerType": "conversation",
                                                    "contextEnd": "false",
                                                    "contextId": skill_args.get("context_id", ""),
                                                    "currentStep": next_step_number,
                                                    "message": summary_text,
                                                    "questionNo": skill_args.get("question_no", ""),
                                                    "sessionId": self.session_id or "default"
                                                }
                                                completion_json = json.dumps(completion_result, ensure_ascii=False)
                                                # 推送一条 conversation 总结消息给前端
                                                if on_token:
                                                    logger.info("[Agent] Emitting final conversation summary to frontend")
                                                    on_token(completion_json)
                                                return completion_json
                                            
                                            logger.info(f"[Agent] Executing next step directly: step {next_step_number}: {next_step_name}")
                                            
                                            # Map step numbers to analysis types - MUST match SKILL.md definition
                                            step_to_analysis_type = {
                                                1: "check_route",      # Step 1: 检查全局路由表中是否存在该静态路由
                                                2: "check_nexthop",    # Step 2: 检查下一跳地址可达性
                                                3: "check_mask",       # Step 3: 检查路由掩码与最长匹配原则
                                                5: "check_interface",  # Step 5: 检查出接口物理与协议状态 (注意：没有Step 4)
                                                6: "check_bfd",        # Step 6: 检查BFD或NQA配置与状态
                                                7: "check_priority"    # Step 7: 检查本静态路由的优先级
                                            }
                                            
                                            # Determine the correct analysis type for this step
                                            # If step number not found in mapping, raise error
                                            if next_step_number not in step_to_analysis_type:
                                                error_msg = f"[Agent] ERROR: Step number {next_step_number} not found in step_to_analysis_type mapping! Valid steps: {list(step_to_analysis_type.keys())}"
                                                logger.error(error_msg)
                                                raise ValueError(error_msg)
                                            
                                            next_analysis_type = step_to_analysis_type[next_step_number]
                                            logger.info(f"[Agent] Step {next_step_number} using analysis_type: {next_analysis_type}")
                                            
                                            # Build next step params (reuse same device_info and destination_network)
                                            # Priority: cached session values > skill_args > defaults
                                            device_info = self._session_device_info.get(session_id_for_step, {})
                                            destination_network = self._session_destination_network.get(session_id_for_step, "")
                                            if not device_info:
                                                device_info = skill_args.get("device_info", {}) or {}
                                            if not destination_network:
                                                destination_network = skill_args.get("destination_network", "") or "0.0.0.0/0"
                                            
                                            # Cache for reuse in subsequent steps
                                            if device_info:
                                                self._session_device_info[session_id_for_step] = device_info
                                            if destination_network and destination_network != "0.0.0.0/0":
                                                self._session_destination_network[session_id_for_step] = destination_network
                                            
                                            # Get commands from SKILL.md for this step
                                            skill_commands = self._current_skill_step_commands.get(next_step_number, [])
                                            logger.info(f"[Agent] Commands from SKILL.md for step {next_step_number}: {skill_commands}")
                                            
                                            next_step_params = {
                                                "step_name": next_step_name,
                                                "step_number": next_step_number,
                                                "skill_name": skill_name,
                                                "analysis_type": next_analysis_type,
                                                "commands": skill_commands,
                                                "destination_network": destination_network,
                                                "device_info": device_info,
                                                "context_id": getattr(self, "frontend_context_id", None) or skill_args.get("context_id", ""),
                                                "question_no": getattr(self, "frontend_question_no", None) or skill_args.get("question_no", ""),
                                                "session_id": getattr(self, "frontend_session_id", None) or self.session_id or "default"
                                            }
                                            
                                            # Create next step tool call
                                            next_step_args_json = json.dumps({
                                                "script_name": "step_executor.py",
                                                "mode": "build_and_execute",
                                                "params": next_step_params
                                            })
                                            
                                            next_step_tool_call = MockToolCall(
                                                id=f"call_step_{next_step_number}_{int(time.time())}",
                                                function=MockFunction(name="execute_step_script", arguments=next_step_args_json),
                                                type="function"
                                            )
                                            
                                            # Execute next step directly
                                            next_step_result = self._execute_tool_call(next_step_tool_call)
                                            
                                            # Parse next step result
                                            try:
                                                next_parsed = json.loads(next_step_result) if isinstance(next_step_result, str) else next_step_result
                                                next_answer_type = next_parsed.get("answerType") if next_parsed else None
                                            except:
                                                next_answer_type = None
                                            
                                            # If next step requires user input (conversation), pause and don't send step name
                                            if next_answer_type == "conversation":
                                                logger.info(f"[Agent] Next step returned conversation (info request), pausing loop")
                                                if on_token:
                                                    on_token(next_step_result)
                                                return next_step_result

                                            # 先发送 [STEP_START]...[STEP_END] 切换前端到新 step
                                            # next_step_number 来自 _current_skill_step_numbers（SKILL.md）
                                            if on_token:
                                                step_marker = f"[STEP_START:{next_step_number}]{next_step_name}[STEP_END]"
                                                on_token(step_marker)

                                            # 若返回 stepBundle，先推送 stepCommand 等附加消息
                                            # forced_step 强制 stepCommand/stepContent 的 currentStep 与 SKILL.md 一致
                                            bundle_handled, next_step_result = self._emit_step_bundle_if_present(next_step_result, on_token, forced_step=next_step_number)
                                            if bundle_handled:
                                                logger.info("[Agent] stepBundle detected on next step, stepCommand pushed before stepContent")

                                            # Send next step stepContent to frontend (step_marker 已在上方发送)
                                            if on_token:
                                                # 兜底：再次确保 currentStep 与 SKILL.md 一致
                                                next_step_result = self._override_currentstep(next_step_result, next_step_number)
                                                on_token(next_step_result)
                                                try:
                                                    import time as _time
                                                    _time.sleep(3)
                                                except Exception:
                                                    pass
                                            
                                            # Update step index
                                            self._session_step_indices[session_id_for_step] = current_step_idx + 1
                                            
                                            # Check if need to pause for user input
                                            if next_answer_type == "stepInfoRequest":
                                                logger.info("[Agent] Next step requires user input, pausing")
                                                # Add tool result to messages
                                                self.messages.append({
                                                    "role": "tool",
                                                    "tool_call_id": next_step_tool_call.id,
                                                    "name": next_step_tool_call.function.name,
                                                    "content": next_step_result,
                                                })
                                                return next_parsed.get("message", "请提供必要的信息。")
                                            elif next_answer_type == "stepError":
                                                logger.info("[Agent] Next step encountered error")
                                                self.messages.append({
                                                    "role": "tool",
                                                    "tool_call_id": next_step_tool_call.id,
                                                    "name": next_step_tool_call.function.name,
                                                    "content": next_step_result,
                                                })
                                                return f"步骤执行错误: {next_parsed.get('message', 'Unknown error')}"
                                            
                                            # stepAnalysis, stepContent or stepCommand - continue to next step
                                            logger.info(f"[Agent] Next step completed with answer_type={next_answer_type}, continuing...")
                                            self.messages.append({
                                                "role": "tool",
                                                "tool_call_id": next_step_tool_call.id,
                                                "name": next_step_tool_call.function.name,
                                                "content": next_step_result,
                                            })
                                            # Update step_result to current step result for next iteration
                                            step_result = next_step_result
                                            # Loop continues to execute next step
                                    else:
                                        # Determine what to return to the user for stepInfoRequest or stepError
                                        return_message = ""
                                        if answer_type == "stepInfoRequest" and user_message:
                                            # Return user-friendly message for direct HTTP responses (e.g., Postman)
                                            return_message = user_message
                                            logger.info(f"[Agent] Returning user message: {return_message[:100]}...")
                                        elif answer_type == "stepError":
                                            return_message = f"步骤执行错误: {user_message}"
                                        elif answer_type is not None:
                                            return_message = f"步骤执行完成，类型: {answer_type}"
                                        
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
                                        
                                        # Return appropriate message to indicate we're waiting for user input
                                        return return_message
                                    
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
                                
                                # Check if this was a step execution (execute_step_script) that completed successfully
                                # If so, return the result directly without going through LLM
                                if forced_tool_call.function.name == "execute_step_script":
                                    logger.info("[Agent] Step execution completed, returning result directly without LLM")
                                    # Return the result message directly
                                    try:
                                        result_parsed = json.loads(result) if isinstance(result, str) else result
                                        if result_parsed and "message" in result_parsed:
                                            return result_parsed.get("message", "步骤执行完成")
                                    except:
                                        pass
                                    return "步骤执行完成"
                                
                                # Continue to next iteration for other tool calls
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
                            # 取真实步骤号嵌入 STEP_START 标记，确保 stepName cs 与 stepCommand/stepContent 一致
                            try:
                                _cs_num = self._current_skill_step_numbers[current_step_idx] if current_step_idx < len(self._current_skill_step_numbers) else current_step_idx + 1
                            except Exception:
                                _cs_num = current_step_idx + 1
                            step_marker = f"[STEP_START:{_cs_num}]{step_name}[STEP_END]"
                            logger.info("[SkillSteps] Sending proactive step notification for step %d: %s", _cs_num, step_name)
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

                # If we sent a stepCommand, do NOT pause - continue execution to wait for frontend to send analysis result
                # But we need to add the tool result to messages so the next LLM call can process it
                if has_step_command:
                    logger.info("[Agent] stepCommand sent to frontend, continuing execution to wait for analysis result")
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

                    # Do NOT return here - continue the loop to wait for frontend to send analysis result
                    # The frontend will send the analysis result as a new user message
                    continue

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
            
    def _extract_device_info_from_context(self) -> dict:
                """Extract device information from conversation context."""
                import re
                
                # Default device info
                device_info = {
                    "ip": "",
                    "port": 23,
                    "protocol": "telnet",
                    "username": "",
                    "password": ""
                }
                
                # Get all user messages from conversation history
                user_messages = []
                for msg in self.messages:
                    if msg.get("role") == "user":
                        content = msg.get("content", "")
                        if content:
                            user_messages.append(content)
                
                # Combine all user messages for analysis
                combined_text = "\n".join(user_messages)
                
                if not combined_text:
                    logger.warning("[DeviceInfo] No user messages found in context")
                    return device_info
                
                # Extract IP address patterns
                ip_patterns = [
                    r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})',
                    r'IP[:\s]+(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})',
                    r'设备[:\s]*IP[:\s]*(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})',
                    r'设备[:\s]*ip[:\s]*(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})',  # Lowercase "ip"
                    r'设备IP[:\s]*(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})',
                ]
                
                for pattern in ip_patterns:
                    match = re.search(pattern, combined_text)
                    if match:
                        device_info["ip"] = match.group(1)
                        logger.info(f"[DeviceInfo] Extracted IP: {device_info['ip']}")
                        break
                
                # Extract username patterns
                username_patterns = [
                    r'(?:用户名|username|user)[:\s]*([a-zA-Z0-9_@.-]+)',
                    r'([a-zA-Z0-9_@.-]+@[a-zA-Z0-9_.-]+)',
                ]
                
                for pattern in username_patterns:
                    match = re.search(pattern, combined_text, re.IGNORECASE)
                    if match:
                        candidate = match.group(1)
                        if candidate.lower() not in ['display', 'ping', 'show', 'check']:
                            device_info["username"] = candidate
                            logger.info(f"[DeviceInfo] Extracted username: {device_info['username']}")
                            break
                
                # Extract password patterns
                password_patterns = [
                    r'(?:密码|password|passwd)[:\s]*([^\s,，;；]+)',
                ]
                
                for pattern in password_patterns:
                    match = re.search(pattern, combined_text, re.IGNORECASE)
                    if match:
                        device_info["password"] = match.group(1)
                        logger.info(f"[DeviceInfo] Extracted password: {'*' * len(device_info['password'])}")
                        break

                # Extract port if specified
                port_match = re.search(r'(?:端口|port)[:\s]*(\d+)', combined_text, re.IGNORECASE)
                if port_match:
                    try:
                        device_info["port"] = int(port_match.group(1))
                        logger.info(f"[DeviceInfo] Extracted port: {device_info['port']}")
                    except ValueError:
                        pass

                # Extract protocol if specified
                protocol_match = re.search(r'(?:协议|protocol)[:\s]*(telnet|ssh|http|https)', combined_text, re.IGNORECASE)
                if protocol_match:
                    device_info["protocol"] = protocol_match.group(1).lower()
                    logger.info(f"[DeviceInfo] Extracted protocol: {device_info['protocol']}")

                # If IP is found, try to load full device info from devices.json
                if device_info["ip"]:
                    try:
                        from pathlib import Path
                        devices_file = Path(__file__).parent.parent / "file" / "devices.json"
                        if devices_file.exists():
                            import json as _json
                            with open(devices_file, 'r', encoding='utf-8') as f:
                                devices = _json.load(f)
                            # Find device by IP
                            for dev in devices:
                                if dev.get("ip") == device_info["ip"]:
                                    # Fill in missing fields from devices.json
                                    if not device_info.get("username") and dev.get("userName"):
                                        device_info["username"] = dev["userName"]
                                        logger.info(f"[DeviceInfo] Loaded username from devices.json: {device_info['username']}")
                                    if not device_info.get("password") and dev.get("password"):
                                        device_info["password"] = dev["password"]
                                        logger.info(f"[DeviceInfo] Loaded password from devices.json")
                                    if not device_info.get("port") or device_info["port"] == 23:
                                        if dev.get("port"):
                                            device_info["port"] = dev["port"]
                                    if not device_info.get("protocol") or device_info["protocol"] == "telnet":
                                        if dev.get("protocol"):
                                            device_info["protocol"] = dev["protocol"]
                                    # Load deviceId as uuid for API sessionId
                                    if not device_info.get("uuid") and dev.get("deviceId"):
                                        device_info["uuid"] = dev["deviceId"]
                                        logger.info(f"[DeviceInfo] Loaded uuid (deviceId) from devices.json: {device_info['uuid']}")
                                    break
                            else:
                                logger.info(f"[DeviceInfo] Device {device_info['ip']} not found in devices.json")
                        else:
                            logger.warning(f"[DeviceInfo] devices.json not found at {devices_file}")
                    except Exception as e:
                        logger.warning(f"[DeviceInfo] Failed to load device info from devices.json: {e}")

                # If IP is still empty, log warning
                if not device_info["ip"]:
                    logger.warning("[DeviceInfo] Could not extract IP address from context")

                return device_info

        # This should never be reached
        # return ""
