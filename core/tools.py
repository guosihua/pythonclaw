"""
Built-in tool implementations and OpenAI-compatible schemas.

Structure
---------
  PRIMITIVE_TOOLS   — run_command / read_file / write_file / list_files (always available)
  SKILL_TOOLS       — use_skill / list_skill_resources (always available)
  META_SKILL_TOOLS  — create_skill (always available — "god mode" skill creation)
  MEMORY_TOOLS      — remember / recall (always available)
  WEB_SEARCH_TOOL   — web_search (only when Tavily API key is configured)
  KNOWLEDGE_TOOL    — consult_knowledge_base (only when a RAG index is loaded)
  CRON_TOOLS        — cron_add / cron_remove / cron_list (only when CronScheduler is injected)

Agent._build_tools() assembles the right subset per session.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)


# ── Virtual-environment detection ─────────────────────────────────────────────

_venv_dir: str | None = None


def _detect_venv() -> str | None:
    """Find the project's virtual environment directory.

    Priority:
      1. Already running inside a venv (sys.prefix != sys.base_prefix)
      2. .venv/ in CWD
      3. venv/ in CWD
    """
    if sys.prefix != sys.base_prefix:
        return sys.prefix

    for name in (".venv", "venv"):
        candidate = os.path.join(os.getcwd(), name)
        python = os.path.join(candidate, "bin", "python")
        if os.path.isfile(python):
            return candidate

    return None


def _venv_python() -> str:
    """Return the Python executable inside the detected venv, or sys.executable."""
    venv = _venv_dir or _detect_venv()
    if venv:
        candidate = os.path.join(venv, "bin", "python")
        if os.path.isfile(candidate):
            return candidate
    return sys.executable


def _venv_env() -> dict[str, str]:
    """Build an env dict that activates the project venv for subprocesses."""
    env = os.environ.copy()
    venv = _venv_dir or _detect_venv()
    if venv:
        venv_bin = os.path.join(venv, "bin")
        env["VIRTUAL_ENV"] = venv
        env["PATH"] = f"{venv_bin}{os.pathsep}{env.get('PATH', '')}"
        env.pop("PYTHONHOME", None)
    else:
        python_dir = os.path.dirname(sys.executable)
        env["PATH"] = f"{python_dir}{os.pathsep}{env.get('PATH', '')}"
    return env


def configure_venv(venv_dir: str | None = None) -> str | None:
    """Explicitly set or auto-detect the venv. Called by Agent.__init__."""
    global _venv_dir
    if venv_dir:
        _venv_dir = os.path.realpath(venv_dir)
    else:
        _venv_dir = _detect_venv()
    if _venv_dir:
        logger.info("[tools] Using venv: %s", _venv_dir)
    return _venv_dir


# ── Sandbox (path restriction) ───────────────────────────────────────────────

_sandbox_roots: list[str] = []


def set_sandbox(roots: list[str]) -> None:
    """Configure the allowed root directories for file-write operations.

    Called by Agent.__init__ to restrict write_file / create_skill to the
    project's working tree.  An empty list disables sandboxing (not recommended).
    """
    _sandbox_roots.clear()
    for r in roots:
        _sandbox_roots.append(os.path.realpath(r))


def _resolve_in_sandbox(path: str) -> str:
    """Resolve *path* to an absolute real path and verify it lives inside the sandbox.

    Returns the resolved path on success.
    Raises ``PermissionError`` if the path escapes every sandbox root.
    """
    resolved = os.path.realpath(os.path.abspath(path))

    if not _sandbox_roots:
        return resolved

    for root in _sandbox_roots:
        if resolved == root or resolved.startswith(root + os.sep):
            return resolved

    raise PermissionError(
        f"Path '{path}' (resolved to '{resolved}') is outside the allowed directories: "
        + ", ".join(_sandbox_roots)
    )


def _sanitize_filename(name: str) -> str:
    """Strip path separators and '..' segments from a filename."""
    name = name.replace("..", "").replace("/", "").replace("\\", "")
    if not name:
        raise ValueError("Empty or invalid filename after sanitization.")
    return name


# ── Primitive tool implementations ────────────────────────────────────────────

def _files_dir() -> str:
    """Return the shared files directory, creating it if needed."""
    from .. import config as _cfg
    return str(_cfg.files_dir())


def run_command(command: str) -> str:
    """Execute a shell command and return combined stdout/stderr.

    The command inherits the project's virtual environment so that
    ``python``, ``pip``, and any installed CLI tools resolve correctly.
    The working directory is set to ``~/.pythonclaw/context/files/`` so
    that any files created or downloaded by the command land there.
    """
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True,
            timeout=60, env=_venv_env(), cwd=_files_dir(),
        )
        out = result.stdout.decode("utf-8", errors="replace")
        err = result.stderr.decode("utf-8", errors="replace")
        return out if result.returncode == 0 else f"Error (exit {result.returncode}):\n{err}"
    except Exception as exc:
        return f"Execution error: {exc}"


def read_file(path: str) -> str:
    """Read and return the contents of a file."""
    try:
        if not os.path.exists(path):
            return f"Error: '{path}' not found."
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as exc:
        return f"Read error: {exc}"


def write_file(path: str, content: str) -> str:
    """Write content to a file, creating parent directories as needed.

    Writes are restricted to sandbox directories (configured via set_sandbox).
    """
    try:
        resolved = _resolve_in_sandbox(path)
        parent = os.path.dirname(resolved)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Written {len(content)} chars to {path}"
    except PermissionError as exc:
        return f"Blocked: {exc}"
    except Exception as exc:
        return f"Write error: {exc}"


def list_files(path: str = ".") -> str:
    """List files in a directory, one per line."""
    try:
        return "\n".join(sorted(os.listdir(path)))
    except Exception as exc:
        return f"List error: {exc}"


_MAX_SEND_FILE_BYTES = 100 * 1024 * 1024  # 100 MB

# Channel-provided callback: send_file_fn(path, caption) → None
_file_sender: callable | None = None


def set_file_sender(fn: callable | None) -> None:
    """Register a callback for sending files to the current channel."""
    global _file_sender
    _file_sender = fn


def send_file(path: str, caption: str = "") -> str:
    """Send a file to the user via the active channel (Telegram/Discord/WhatsApp/Web)."""
    resolved = os.path.realpath(os.path.abspath(path))
    if not os.path.isfile(resolved):
        return f"Error: file not found: {path}"

    size = os.path.getsize(resolved)
    if size > _MAX_SEND_FILE_BYTES:
        size_mb = size / (1024 * 1024)
        return f"Error: file too large ({size_mb:.1f} MB). Maximum allowed is 100 MB."

    if _file_sender is None:
        return (
            f"File ready at: {resolved} ({size / 1024:.1f} KB). "
            "No active channel to send through — user can download it directly."
        )

    try:
        _file_sender(resolved, caption)
        name = os.path.basename(resolved)
        return f"File '{name}' ({size / 1024:.1f} KB) sent successfully."
    except Exception as exc:
        return f"Error sending file: {exc}"


def execute_step_script(script_name: str, mode: str, params: dict | str) -> str:
    """Execute a Python script for a troubleshooting step.
    
    This function runs the specified Python script in either 'build', 'analyze', 
    or 'build_and_execute' mode.
    
    Parameters
    ----------
    script_name : str
        Name of the script file (e.g., 'step_executor.py')
    mode : str
        Execution mode: 'build', 'analyze', or 'build_and_execute'
    params : dict | str
        Parameters to pass to the script (can be dict or JSON string)
        
    Returns
    -------
    str
        JSON-formatted result from the script
    """
    import json
    import subprocess
    from pathlib import Path
    
    try:
        # Ensure params is a dict (LLM may generate it as a JSON string)
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except json.JSONDecodeError:
                return json.dumps({"error": f"Invalid JSON in params: {params}"})
        
        # Determine script path - look in skill directories
        # Scripts are located in templates/skills/{category}/{skill_name}/
        skill_base = Path(__file__).parent.parent / "templates" / "skills"
        
        # Search for the script in all skill directories
        script_path = None
        for category_dir in skill_base.iterdir():
            if category_dir.is_dir():
                for skill_dir in category_dir.iterdir():
                    if skill_dir.is_dir():
                        candidate = skill_dir / script_name
                        if candidate.exists():
                            script_path = candidate
                            break
                if script_path:
                    break
        
        if not script_path:
            return json.dumps({"error": f"Script '{script_name}' not found in any skill directory"})
        
        # Build command based on mode
        if mode == "build":
            cmd = [sys.executable, str(script_path), "build", json.dumps(params)]
        elif mode == "analyze":
            # For analyze mode, params should contain response_data
            response_data = params.get("response_data", "")
            cmd = [sys.executable, str(script_path), "analyze", response_data]
        elif mode == "build_and_execute":
            # New unified mode: build commands and execute immediately
            cmd = [sys.executable, str(script_path), "build_and_execute", json.dumps(params)]
        else:
            return json.dumps({"error": f"Invalid mode: {mode}. Must be 'build', 'analyze', or 'build_and_execute'"})
        
        # Execute the script
        logger.info("[execute_step_script] Running: %s", " ".join(cmd))
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,  # Increased timeout for build_and_execute mode (may need to call API)
            env=_venv_env()
        )
        
        # Log stderr (debug output from step_executor)
        if result.stderr:
            logger.info("[execute_step_script] Script stderr:\n%s", result.stderr)
        
        if result.returncode != 0:
            error_msg = result.stderr.strip() if result.stderr else "Unknown error"
            logger.error("[execute_step_script] Script failed: %s", error_msg)
            return json.dumps({"error": f"Script execution failed: {error_msg}"})
        
        output = result.stdout.strip()
        logger.info("[execute_step_script] Script output length: %d chars", len(output))
        logger.info("[execute_step_script] Script output:\n%s", output)
        
        # Validate that output is valid JSON
        try:
            json.loads(output)
            return output
        except json.JSONDecodeError:
            return json.dumps({"error": f"Script returned invalid JSON: {output[:200]}"})
    
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "Script execution timed out (60s limit)"})
    except Exception as exc:
        logger.exception("[execute_step_script] Unexpected error")
        return json.dumps({"error": f"Unexpected error: {str(exc)}"})


def execute_topology_script(params: dict | str | None = None) -> str:
    """Fetch the network topology for the active skill and return a JSON bundle
    of frontend-bound messages.

    This runs ``topology_executor.py`` in the static-troubleshooting skill
    directory, which:
      1. Loads file/devices.json
      2. Calls the third-party topology API
      3. Builds three SSE-ready messages: opening conversation,
         topology data, closing conversation.

    Parameters
    ----------
    params : dict | str | None
        Optional dict containing ``session_id``, ``context_id``,
        ``question_no``. Strings are auto-parsed as JSON.

    Returns
    -------
    str
        JSON string with structure::

            {
              "answerType": "topologyBundle",
              "status": "success" | "failed",
              "sessionId": "...",
              "contextId": "...",
              "questionNo": "...",
              "currentStep": 0,
              "messages": [ ...three SSE-ready dicts... ]
            }
    """
    import json
    import subprocess
    from pathlib import Path

    try:
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except json.JSONDecodeError:
                return json.dumps({"error": f"Invalid JSON in params: {params}"})
        if params is None:
            params = {}

        skill_base = Path(__file__).parent.parent / "templates" / "skills"
        script_path = None
        for category_dir in skill_base.iterdir():
            if not category_dir.is_dir():
                continue
            for skill_dir in category_dir.iterdir():
                if skill_dir.is_dir():
                    candidate = skill_dir / "topology_executor.py"
                    if candidate.exists():
                        script_path = candidate
                        break
            if script_path:
                break

        if not script_path:
            return json.dumps({"error": "topology_executor.py not found in any skill directory"})

        cmd = [sys.executable, str(script_path), "fetch", json.dumps(params)]
        # logger.info("[execute_topology_script] Running: %s", " ".join(cmd))
        # 拓扑获取耗时较长（第三方接口 read timeout 已设为 120s），
        # 子进程额外预留 30s 用于设备列表加载与日志输出，整体 150s
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=150,
            env=_venv_env(),
        )

        if result.stderr:
            # logger.info("[execute_topology_script] Script stderr:\n%s", result.stderr)
            pass

        if result.returncode != 0:
            error_msg = result.stderr.strip() if result.stderr else "Unknown error"
            logger.error("[execute_topology_script] Script failed: %s", error_msg)
            return json.dumps({"error": f"Topology script execution failed: {error_msg}"})

        output = result.stdout.strip()
        # logger.info("[execute_topology_script] Script output length: %d chars", len(output))
        # logger.info("[execute_topology_script] Script output:\n%s", output)

        try:
            json.loads(output)
            return output
        except json.JSONDecodeError:
            return json.dumps({"error": f"Topology script returned invalid JSON: {output[:200]}"})

    except subprocess.TimeoutExpired:
        return json.dumps({"error": "Topology script execution timed out (150s limit)"})
    except Exception as exc:
        logger.exception("[execute_topology_script] Unexpected error")
        return json.dumps({"error": f"Unexpected error: {str(exc)}"})



# ============================================================================
# Tool Schemas and Registry
# ============================================================================

def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    """Helper to build an OpenAI-compatible function schema."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


PRIMITIVE_TOOLS: list[dict] = [
    _fn(
        "run_command",
        "Execute a shell command on the host machine.",
        {
            "command": {"type": "string", "description": "Shell command to execute."},
        },
        ["command"],
    ),
    _fn(
        "read_file",
        "Read the content of a file.",
        {
            "path": {"type": "string", "description": "Path to the file."},
        },
        ["path"],
    ),
    _fn(
        "write_file",
        "Write content to a file (creates or overwrites).",
        {
            "path": {"type": "string", "description": "Path to the file."},
            "content": {"type": "string", "description": "Content to write."},
        },
        ["path", "content"],
    ),
    _fn(
        "list_files",
        "List files in a directory.",
        {
            "path": {"type": "string", "description": "Directory path."},
        },
        ["path"],
    ),
]

SKILL_TOOLS: list[dict] = [
    _fn(
        "use_skill",
        "Activate a skill by loading its instructions into context.",
        {
            "skill_name": {"type": "string", "description": "Name of the skill to activate."},
        },
        ["skill_name"],
    ),
    _fn(
        "list_skill_resources",
        "List bundled resources for a skill.",
        {
            "skill_name": {"type": "string", "description": "Name of the skill."},
        },
        ["skill_name"],
    ),
]

META_SKILL_TOOLS: list[dict] = [
    _fn(
        "create_skill",
        "Create a new skill dynamically. Requires skill_name, description, and instructions.",
        {
            "skill_name": {"type": "string", "description": "Unique name for the new skill."},
            "description": {"type": "string", "description": "Short description of what the skill does."},
            "instructions": {"type": "string", "description": "Full SKILL.md content including frontmatter."},
        },
        ["skill_name", "description", "instructions"],
    ),
]

MEMORY_TOOLS: list[dict] = [
    _fn(
        "remember",
        "Store information in long-term memory.",
        {
            "content": {"type": "string", "description": "Information to remember."},
            "key": {"type": "string", "description": "Optional key for retrieval."},
        },
        ["content"],
    ),
    _fn(
        "recall",
        "Retrieve information from long-term memory.",
        {
            "query": {"type": "string", "description": "Search query."},
        },
        ["query"],
    ),
    _fn(
        "memory_get",
        "Get content of a specific memory file.",
        {
            "path": {"type": "string", "description": "Path to memory file (e.g., MEMORY.md)."},
        },
        ["path"],
    ),
    _fn(
        "memory_list_files",
        "List all memory files.",
        {},
        [],
    ),
    _fn(
        "forget",
        "Remove a memory entry by key.",
        {
            "key": {"type": "string", "description": "Key of the memory to forget."},
        },
        ["key"],
    ),
    _fn(
        "update_index",
        "Update the knowledge index with new content.",
        {
            "content": {"type": "string", "description": "Content to add to the index."},
        },
        ["content"],
    ),
]

CRON_TOOLS: list[dict] = [
    _fn(
        "cron_add",
        "Schedule a recurring task.",
        {
            "job_id": {"type": "string", "description": "Unique ID for the job."},
            "cron": {"type": "string", "description": "Cron expression (e.g., '0 9 * * *')."},
            "prompt": {"type": "string", "description": "Task description or prompt."},
            "deliver_to_chat_id": {"type": "string", "description": "Optional chat ID for delivery."},
        },
        ["job_id", "cron", "prompt"],
    ),
    _fn(
        "cron_remove",
        "Remove a scheduled task.",
        {
            "job_id": {"type": "string", "description": "ID of the job to remove."},
        },
        ["job_id"],
    ),
    _fn(
        "cron_list",
        "List all scheduled tasks.",
        {},
        [],
    ),
]

WEB_SEARCH_TOOL: list[dict] = [
    _fn(
        "web_search",
        "Search the web using Tavily API.",
        {
            "query": {"type": "string", "description": "Search query."},
        },
        ["query"],
    ),
]

KNOWLEDGE_TOOL: list[dict] = [
    _fn(
        "consult_knowledge_base",
        "Consult the RAG knowledge base for relevant information.",
        {
            "query": {"type": "string", "description": "Query to search in the knowledge base."},
        },
        ["query"],
    ),
]

EXECUTE_STEP_TOOL: list[dict] = [
    _fn(
        "execute_step_script",
        "Execute a troubleshooting step script. This is the PRIMARY tool for skill-based troubleshooting workflows.",
        {
            "script_name": {
                "type": "string",
                "description": "Name of the script file (e.g., 'step_executor.py'). ALWAYS use 'step_executor.py' for static-troubleshooting skill."
            },
            "mode": {
                "type": "string",
                "description": "Execution mode. Use 'build_and_execute' for complete workflow (build command + execute + analyze)."
            },
            "params": {
                "type": "object",
                "description": "Parameters for the script execution.",
                "properties": {
                    "commands": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of command templates with {{variable}} placeholders. OPTIONAL if analysis_type is provided (will auto-generate commands). Example: ['display ip routing-table {{destination_network}}']"
                    },
                    "analysis_type": {
                        "type": "string",
                        "description": "Type of analysis to perform (e.g., 'check_route', 'check_nexthop', 'check_mask', 'check_interface', 'check_bfd', 'check_priority'). Used to auto-generate commands if not provided."
                    },
                    "step_type": {
                        "type": "string",
                        "description": "Alias for analysis_type. Can be used as fallback."
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Unique session ID for device command execution. Required for build_and_execute mode."
                    },
                    "device_info": {
                        "type": "object",
                        "description": "Device connection information including ip, port, protocol, username, password. OPTIONAL - if not provided, will be looked up from devices.json by IP."
                    },
                    "destination_network": {
                        "type": "string",
                        "description": "Target network for route checking (e.g., '192.168.1.0/24'). Used in command template rendering."
                    },
                    "nexthop_ip": {
                        "type": "string",
                        "description": "Next-hop IP address. Used in command template rendering for ping/tracert commands."
                    },
                    "context_id": {
                        "type": "string",
                        "description": "Context ID for linking with frontend UI."
                    },
                    "question_no": {
                        "type": "string",
                        "description": "Question number for tracking multiple troubleshooting sessions."
                    }
                },
                "required": ["analysis_type"]
            }
        },
        ["script_name", "mode", "params"],
    ),
]


EXECUTE_TOPOLOGY_TOOL: list[dict] = [
    _fn(
        "execute_topology_script",
        "Fetch network topology for the active skill. Returns a JSON bundle of frontend-bound messages including topology data. Should be called once at the very beginning of the troubleshooting flow before executing the first step.",
        {
            "params": {
                "type": "object",
                "description": "Optional parameters for the topology script.",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Unique session ID."
                    },
                    "context_id": {
                        "type": "string",
                        "description": "Context ID for linking with frontend UI."
                    },
                    "question_no": {
                        "type": "string",
                        "description": "Question number for tracking multiple troubleshooting sessions."
                    }
                },
            },
        },
        [],
    ),
]


# Unified tool registry — Agent._build_tools() picks subsets at runtime.
AVAILABLE_TOOLS: dict[str, callable] = {
    "run_command": lambda **kwargs: None,  # Placeholder; actual impl injected by Agent
    "read_file": lambda **kwargs: None,
    "write_file": lambda **kwargs: None,
    "list_files": lambda **kwargs: None,
    "use_skill": lambda **kwargs: None,
    "list_skill_resources": lambda **kwargs: None,
    "create_skill": lambda **kwargs: None,
    "remember": lambda **kwargs: None,
    "recall": lambda **kwargs: None,
    "memory_get": lambda **kwargs: None,
    "memory_list_files": lambda **kwargs: None,
    "forget": lambda **kwargs: None,
    "update_index": lambda **kwargs: None,
    "cron_add": lambda **kwargs: None,
    "cron_remove": lambda **kwargs: None,
    "cron_list": lambda **kwargs: None,
    "web_search": lambda **kwargs: None,
    "consult_knowledge_base": lambda **kwargs: None,
    "execute_step_script": execute_step_script,
    "execute_topology_script": execute_topology_script,
}
