"""
FastAPI application for the PythonClaw Web Dashboard.

Provides REST endpoints for config/skills/status inspection, a config
save endpoint for editing settings from the browser, and a WebSocket
endpoint for real-time chat with the agent.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import config
from ..core.agent import Agent
from ..core.llm.base import LLMProvider
from ..core.persistent_agent import PersistentAgent
from ..core.session_store import SessionStore
from ..core.skill_loader import SkillRegistry

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

_agents: dict[str, Agent] = {}
_provider: LLMProvider | None = None
_store: SessionStore | None = None
_start_time: float = 0.0
_build_provider_fn = None
_active_bots: list = []
_chat_lock: asyncio.Lock | None = None
_fastapi_app: FastAPI | None = None

# WEB_SESSION_ID = "web:dashboard"


def _get_chat_lock() -> asyncio.Lock:
    """Lazily create the web chat lock (must be done inside the event loop)."""
    global _chat_lock
    if _chat_lock is None:
        _chat_lock = asyncio.Lock()
    return _chat_lock


def create_app(provider: LLMProvider | None, *, build_provider_fn=None) -> FastAPI:
    """Build and return the FastAPI app.

    Parameters
    ----------
    provider          : LLM provider (may be None if not yet configured)
    build_provider_fn : callable that rebuilds the provider from config
                        (used after config save to hot-reload the provider)
    """
    global _provider, _store, _start_time, _build_provider_fn, _fastapi_app
    _provider = provider
    _store = SessionStore()
    _start_time = time.time()
    _build_provider_fn = build_provider_fn

    app = FastAPI(title="PythonClaw Dashboard", docs_url=None, redoc_url=None)
    _fastapi_app = app

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.add_api_route("/", _serve_index, methods=["GET"], response_class=HTMLResponse)
    app.add_api_route("/api/config", _api_config_get, methods=["GET"])
    app.add_api_route("/api/config", _api_config_save, methods=["POST"])
    # app.add_api_route("/api/skills", _api_skills, methods=["GET"])
    # app.add_api_route("/api/status", _api_status, methods=["GET"])
    # app.add_api_route("/api/memories", _api_memories, methods=["GET"])
    app.add_api_route("/api/identity", _api_identity, methods=["GET"])
    app.add_api_route("/api/identity/soul", _api_save_soul, methods=["POST"])
    app.add_api_route("/api/identity/persona", _api_save_persona, methods=["POST"])
    app.add_api_route("/api/identity/tools", _api_get_tools_notes, methods=["GET"])
    app.add_api_route("/api/identity/tools", _api_save_tools_notes, methods=["POST"])
    # app.add_api_route("/api/memory/index", _api_get_index, methods=["GET"])
    # app.add_api_route("/api/memory/index", _api_save_index, methods=["POST"])
    app.add_api_route("/api/transcribe", _api_transcribe, methods=["POST"])
    app.add_api_route("/api/marketplace/search", _api_marketplace_search, methods=["POST"])
    app.add_api_route("/api/marketplace/browse", _api_marketplace_browse, methods=["GET"])
    # app.add_api_route("/api/marketplace/install", _api_marketplace_install, methods=["POST"])
    app.add_api_route("/api/marketplace/stats", _api_marketplace_stats, methods=["GET"])
    # Legacy aliases
    app.add_api_route("/api/skillhub/search", _api_marketplace_search, methods=["POST"])
    app.add_api_route("/api/skillhub/browse", _api_marketplace_browse, methods=["GET"])
    # app.add_api_route("/api/skillhub/install", _api_marketplace_install, methods=["POST"])
    app.add_api_route("/api/channels", _api_channels_status, methods=["GET"])
    app.add_api_route("/api/channels/restart", _api_channels_restart, methods=["POST"])
    # Step analysis endpoint for skill troubleshooting
    app.add_api_route("/api/step/analyze", _api_step_analyze, methods=["POST"])
    # app.add_api_route("/api/files/clear", _api_clear_files, methods=["POST"])
    # app.add_api_route("/api/files", _api_list_files, methods=["GET"])
    app.add_api_websocket_route("/ws/chat", _ws_chat)
    app.add_api_route("/chatbot/upload", _api_chatbot_upload, methods=["POST"])
    app.add_api_route("/dm/cancas/chat", _sse_chat, methods=["POST"])
    # app.add_api_route("/chatbot/dm/claw/stream", _sse_chat, methods=["POST"])

    return app


def _get_agent(session_id: str) -> Agent | None:
    """Lazy-init a per-session web agent with persistent sessions."""
    global _agents
    if session_id in _agents:
        return _agents[session_id]
    if _provider is None:
        return None
    try:
        verbose = config.get("agent", "verbose", default=False)
        agent = PersistentAgent(
            provider=_provider,
            verbose=bool(verbose),
            store=_store,
            session_id=session_id,
        )
        _agents[session_id] = agent
        logger.info("[Web] Agent created for session '%s'", session_id)
    except Exception as exc:
        logger.warning("[Web] Agent init failed for session '%s': %s", session_id, exc)
        return None
    return agent


def _reset_agent(session_id: str | None = None) -> None:
    """Discard agent(s) so the next call rebuilds them.

    If session_id is None, all agents are discarded (e.g. on config change).
    """
    global _agents
    if session_id is None:
        _agents.clear()
        logger.info("[Web] All agents reset")
    else:
        _agents.pop(session_id, None)
        logger.info("[Web] Agent reset for session '%s'", session_id)


# ── HTML ──────────────────────────────────────────────────────────────────────

async def _serve_index():
    index_path = STATIC_DIR / "index.html"
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


# ── REST API ──────────────────────────────────────────────────────────────────

def _mask_secrets(obj: Any, _parent_key: str = "") -> Any:
    """Recursively mask values whose key contains 'apikey' or 'token'."""
    if isinstance(obj, dict):
        return {k: _mask_secrets(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_secrets(v) for v in obj]
    if isinstance(obj, str) and obj:
        key_lower = _parent_key.lower()
        if any(s in key_lower for s in ("apikey", "token", "secret", "password")):
            if len(obj) > 8:
                return obj[:4] + "*" * (len(obj) - 8) + obj[-4:]
            return "****"
    return obj


def _secret_keys_present(obj: Any, _parent_key: str = "") -> dict[str, str]:
    """Walk config and return a flat map of dotted-key → value for secret fields."""
    result: dict[str, str] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            full = f"{_parent_key}.{k}" if _parent_key else k
            if isinstance(v, (dict, list)):
                result.update(_secret_keys_present(v, full))
            elif isinstance(v, str) and v:
                if any(s in k.lower() for s in ("apikey", "token", "secret", "password")):
                    result[full] = v
    return result


_MASKED_PLACEHOLDER = "••••••••"


async def _api_config_get():
    raw = config.as_dict()
    masked = _mask_secrets(copy.deepcopy(raw))
    cfg_path = config.config_path()

    # Build a list of which secret fields have a value set (without revealing them)
    secrets_set = {k: True for k in _secret_keys_present(raw)}

    return {
        "config": masked,
        "configPath": str(cfg_path) if cfg_path else None,
        "providerReady": _provider is not None,
        "secretsSet": secrets_set,
    }


def _deep_set(d: dict, keys: list[str], value: Any) -> None:
    """Set a value in a nested dict using a list of keys."""
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def _deep_get_raw(d: dict, keys: list[str]) -> Any:
    """Get a value from a nested dict using a list of keys."""
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


async def _api_config_save(request: Request):
    """Save new configuration to pythonclaw.json and hot-reload the provider.

    Secret fields that arrive as the masked placeholder or empty string
    are preserved from the existing config (not overwritten).
    """
    global _provider

    try:
        body = await request.json()
        new_config = body.get("config")
        if not isinstance(new_config, dict):
            return JSONResponse({"ok": False, "error": "Invalid config object."}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    # Merge: for any secret field that is still the placeholder or empty,
    # keep the original value from the current config.
    existing = config.as_dict()
    existing_secrets = _secret_keys_present(existing)
    for dotted_key, original_value in existing_secrets.items():
        keys = dotted_key.split(".")
        incoming = _deep_get_raw(new_config, keys)
        if incoming is None or incoming == "" or incoming == _MASKED_PLACEHOLDER or "****" in str(incoming):
            _deep_set(new_config, keys, original_value)

    cfg_path = config.config_path()
    if cfg_path is None:
        cfg_path = config.PYTHONCLAW_HOME / "pythonclaw.json"

    try:
        json_text = json.dumps(new_config, indent=2, ensure_ascii=False)
        cfg_path.write_text(json_text + "\n", encoding="utf-8")
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Write failed: {exc}"}, status_code=500)

    config.load(str(cfg_path), force=True)
    logger.info("[Web] Config saved to %s", cfg_path)

    _reset_agent()
    if _build_provider_fn:
        try:
            _provider = _build_provider_fn()
            logger.info("[Web] Provider rebuilt successfully.")
        except Exception as exc:
            logger.warning("[Web] Provider rebuild failed: %s", exc)
            _provider = None

    channels_started = await _maybe_start_channels()

    return {
        "ok": True,
        "configPath": str(cfg_path),
        "providerReady": _provider is not None,
        "channelsStarted": channels_started,
    }


# async def _api_skills():
#     agent = _get_agent()
#     if agent is None:
#         try:
#             pkg_templates = os.path.join(
#                 os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
#                 "templates", "skills",
#             )
#             skills_dirs = [pkg_templates, os.path.join(str(config.PYTHONCLAW_HOME), "context", "skills")]
#             skills_dirs = [d for d in skills_dirs if os.path.isdir(d)]
#             registry = SkillRegistry(skills_dirs=skills_dirs)
#             skills_meta = registry.discover()
#         except Exception:
#             return {"total": 0, "categories": {}}
#     else:
#         registry = agent._registry
#         skills_meta = registry.discover()

#     categories: dict[str, list] = {}
#     for sm in skills_meta:
#         cat = sm.category or "uncategorised"
#         categories.setdefault(cat, []).append({
#             "name": sm.name,
#             "description": sm.description,
#             "category": cat,
#             "path": sm.path,
#             "emoji": sm.emoji,
#         })

#     cat_meta = {}
#     for cat_key, cat_obj in registry.categories.items():
#         cat_meta[cat_key] = {
#             "name": cat_obj.name,
#             "description": cat_obj.description,
#             "emoji": cat_obj.emoji,
#         }

#     return {"total": len(skills_meta), "categories": categories, "categoryMeta": cat_meta}


# async def _api_status():
#     uptime = int(time.time() - _start_time)
#     provider_name = config.get_str("llm", "provider", env="LLM_PROVIDER", default="deepseek")

#     agent = _get_agent()
#     if agent is None:
#         return {
#             "provider": "Not configured",
#             "providerName": provider_name,
#             "providerReady": False,
#             "skillsLoaded": 0,
#             "skillsTotal": 0,
#             "memoryCount": 0,
#             "historyLength": 0,
#             "compactionCount": 0,
#             "uptimeSeconds": uptime,
#             "webSearchEnabled": False,
#         }

#     session_file = _store._path(WEB_SESSION_ID) if _store else None
#     return {
#         "provider": type(agent.provider).__name__,
#         "providerName": provider_name,
#         "providerReady": True,
#         "skillsLoaded": len(agent.loaded_skill_names),
#         "skillsTotal": len(agent._registry.discover()),
#         "memoryCount": len(agent.memory.list_all()),
#         "historyLength": len(agent.messages),
#         "compactionCount": agent.compaction_count,
#         "uptimeSeconds": uptime,
#         "webSearchEnabled": agent._web_search_enabled,
#         "sessionFile": session_file,
#         "sessionPersistent": True,
#     }


# async def _api_memories():
#     agent = _get_agent()
#     if agent is None:
#         return {"total": 0, "memories": []}
#     memories = agent.memory.list_all()
#     return {"total": len(memories), "memories": memories}


async def _api_identity():
    """Return soul, persona content, and the full tool list."""
    from ..core.tools import (
        CRON_TOOLS,
        KNOWLEDGE_TOOL,
        MEMORY_TOOLS,
        META_SKILL_TOOLS,
        PRIMITIVE_TOOLS,
        SKILL_TOOLS,
        WEB_SEARCH_TOOL,
    )

    def _read_md(directory: str) -> str | None:
        p = Path(directory)
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()
        if p.is_dir():
            for f in sorted(p.iterdir()):
                if f.suffix in (".md", ".txt") and f.is_file():
                    return f.read_text(encoding="utf-8").strip()
        return None

    home = config.PYTHONCLAW_HOME
    soul = _read_md(str(home / "context" / "soul"))
    persona = _read_md(str(home / "context" / "persona"))
    tools_notes = _read_md(str(home / "context" / "tools"))
    index_file = home / "context" / "memory" / "INDEX.md"
    index_content = None
    if index_file.is_file():
        try:
            index_content = index_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass

    def _tool_info(schema: dict) -> dict:
        fn = schema.get("function", {})
        return {"name": fn.get("name", ""), "description": fn.get("description", "")}

    tools = []
    tool_groups = [
        ("Primitive", PRIMITIVE_TOOLS),
        ("Skills", SKILL_TOOLS),
        ("Meta", META_SKILL_TOOLS),
        ("Memory", MEMORY_TOOLS),
        ("Cron", CRON_TOOLS),
    ]
    for group, schemas in tool_groups:
        for s in schemas:
            info = _tool_info(s)
            info["group"] = group
            tools.append(info)

    tools.append({**_tool_info(WEB_SEARCH_TOOL), "group": "Search"})
    tools.append({**_tool_info(KNOWLEDGE_TOOL), "group": "Knowledge"})

    return {
        "soul": soul,
        "persona": persona,
        "toolsNotes": tools_notes,
        "indexContent": index_content,
        "soulConfigured": soul is not None,
        "personaConfigured": persona is not None,
        "toolsNotesConfigured": tools_notes is not None,
        "indexConfigured": index_content is not None,
        "tools": tools,
    }


async def _api_save_soul(request: Request):
    """Save soul content to context/soul/SOUL.md and reload agent identity."""
    try:
        body = await request.json()
        content = body.get("content", "").strip()
        if not content:
            return JSONResponse({"ok": False, "error": "Content cannot be empty."}, status_code=400)

        soul_dir = config.PYTHONCLAW_HOME / "context" / "soul"
        soul_dir.mkdir(parents=True, exist_ok=True)
        soul_file = soul_dir / "SOUL.md"
        soul_file.write_text(content + "\n", encoding="utf-8")
        logger.info("[Web] Soul saved to %s", soul_file)

        _reload_agent_identity()
        return {"ok": True, "path": str(soul_file)}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def _api_save_persona(request: Request):
    """Save persona content to context/persona/persona.md and reload agent identity."""
    try:
        body = await request.json()
        content = body.get("content", "").strip()
        if not content:
            return JSONResponse({"ok": False, "error": "Content cannot be empty."}, status_code=400)

        persona_dir = config.PYTHONCLAW_HOME / "context" / "persona"
        persona_dir.mkdir(parents=True, exist_ok=True)
        persona_file = persona_dir / "persona.md"
        persona_file.write_text(content + "\n", encoding="utf-8")
        logger.info("[Web] Persona saved to %s", persona_file)

        _reload_agent_identity()
        return {"ok": True, "path": str(persona_file)}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def _api_get_tools_notes():
    """Return the current TOOLS.md content."""
    tools_dir = config.PYTHONCLAW_HOME / "context" / "tools"
    content = None
    if tools_dir.is_dir():
        for f in sorted(tools_dir.iterdir()):
            if f.suffix in (".md", ".txt") and f.is_file():
                content = f.read_text(encoding="utf-8").strip()
                break
    elif tools_dir.is_file():
        content = tools_dir.read_text(encoding="utf-8").strip()
    return {"ok": True, "content": content}


async def _api_save_tools_notes(request: Request):
    """Save TOOLS.md content and reload agent identity."""
    try:
        body = await request.json()
        content = body.get("content", "").strip()

        tools_dir = config.PYTHONCLAW_HOME / "context" / "tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        tools_file = tools_dir / "TOOLS.md"
        tools_file.write_text(content + "\n", encoding="utf-8")
        logger.info("[Web] TOOLS.md saved to %s", tools_file)

        _reload_agent_identity()
        return {"ok": True, "path": str(tools_file)}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def _api_get_index():
    """Return the INDEX.md curated system info content."""
    index_path = config.PYTHONCLAW_HOME / "context" / "memory" / "INDEX.md"
    content = ""
    if index_path.is_file():
        try:
            content = index_path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return {"content": content, "path": str(index_path)}


# async def _api_save_index(request: Request):
#     """Save INDEX.md content and refresh agent memory."""
#     try:
#         body = await request.json()
#         content = body.get("content", "").strip()
#         index_dir = config.PYTHONCLAW_HOME / "context" / "memory"
#         index_dir.mkdir(parents=True, exist_ok=True)
#         index_file = index_dir / "INDEX.md"
#         index_file.write_text(content + "\n", encoding="utf-8")
#         logger.info("[Web] INDEX.md saved to %s", index_file)

#         agent = _get_agent()
#         if agent is not None:
#             agent.memory.storage._load()
#             agent._init_system_prompt()
#         # Refresh all live agents with the new index
#         for a in _agents.values():
#             if a is not agent:
#                 a.memory.storage._load()
#                 a._init_system_prompt()

#         return {"ok": True, "path": str(index_file)}
#     except Exception as exc:
#         return JSONResponse(
#             {"ok": False, "error": str(exc)}, status_code=500
#         )


async def _api_transcribe(request: Request):
    """Proxy audio to Deepgram STT and return transcript."""
    from ..core.stt import no_key_message, transcribe_bytes_async

    content_type = request.headers.get("content-type", "audio/webm")
    body = await request.body()
    if not body:
        return JSONResponse({"ok": False, "error": "No audio data received."}, status_code=400)

    try:
        transcript = await transcribe_bytes_async(body, content_type)
    except Exception as exc:
        logger.warning("[Web] Deepgram error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    if transcript is None:
        return JSONResponse({"ok": False, "error": no_key_message()}, status_code=400)

    return {"ok": True, "transcript": transcript}


async def _api_marketplace_search(request: Request):
    """Search ClawHub marketplace."""
    from ..core import skillhub

    try:
        body = await request.json()
        query = body.get("query", "").strip()
        if not query:
            return JSONResponse({"ok": False, "error": "Query is required."}, status_code=400)
        limit = int(body.get("limit", 10))
        results = await skillhub.search_async(query, limit=limit)
        return {"ok": True, "results": results}
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def _api_marketplace_browse(request: Request):
    """Browse ClawHub catalog."""
    from ..core import skillhub

    try:
        limit = int(request.query_params.get("limit", 20))
        sort = request.query_params.get("sort", "score")
        results = await skillhub.browse_async(limit=limit, sort=sort)
        return {"ok": True, "results": results}
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# async def _api_marketplace_install(request: Request):
#     """Install a skill from ClawHub and hot-reload into the running agent."""
#     from ..core import skillhub

#     try:
#         body = await request.json()
#         skill_id = body.get("skill_id", "").strip()
#         if not skill_id:
#             return JSONResponse({"ok": False, "error": "skill_id is required."}, status_code=400)

#         path = await skillhub.install_skill_async(skill_id)

#         agent = _get_agent()
#         skill_count = 0
#         installed_name = ""
#         if agent is not None:
#             agent._refresh_skill_registry()
#             skill_count = len(agent._registry.discover())
#             for sm in agent._registry.discover():
#                 if sm.path == path:
#                     installed_name = sm.name
#                     break
#         # Refresh all live agents' skill registries
#         for a in _agents.values():
#             if a is not agent:
#                 a._refresh_skill_registry()

#         if not installed_name:
#             import re as _re
#             md_path = os.path.join(path, "SKILL.md")
#             try:
#                 md_text = open(md_path, encoding="utf-8").read()
#                 m = _re.search(r"^name:\s*(.+)$", md_text, _re.MULTILINE)
#                 installed_name = m.group(1).strip() if m else skill_id
#             except OSError:
#                 installed_name = skill_id

#         return {
#             "ok": True,
#             "path": path,
#             "skill_name": installed_name,
#             "skill_count": skill_count,
#             "message": f"Skill '{installed_name}' installed and ready to use.",
#         }
#     except RuntimeError as exc:
#         return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
#     except Exception as exc:
#         return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def _api_marketplace_stats(request: Request):
    """Get ClawHub marketplace statistics."""
    from ..core import skillhub

    try:
        result = await skillhub.verify_api_async()
        return result
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def _maybe_start_channels() -> list[str]:
    """Start channels whose tokens are now configured but not yet running."""
    global _active_bots
    if _provider is None:
        return []

    wanted = []
    tg_token = config.get_str("channels", "telegram", "token", default="")
    if tg_token:
        wanted.append("telegram")
    dc_token = config.get_str("channels", "discord", "token", default="")
    if dc_token:
        wanted.append("discord")
    wa_phone = config.get_str("channels", "whatsapp", "phoneNumberId", default="")
    wa_token = config.get_str("channels", "whatsapp", "token", default="")
    if wa_phone and wa_token:
        wanted.append("whatsapp")

    if not wanted:
        return []

    running_types = set()
    for bot in _active_bots:
        cls_name = type(bot).__name__.lower()
        if "telegram" in cls_name:
            running_types.add("telegram")
        elif "discord" in cls_name:
            running_types.add("discord")
        elif "whatsapp" in cls_name:
            running_types.add("whatsapp")

    to_start = [ch for ch in wanted if ch not in running_types]
    if not to_start:
        return list(running_types)

    try:
        from ..server import start_channels
        new_bots = await start_channels(_provider, to_start, fastapi_app=_fastapi_app)
        _active_bots.extend(new_bots)
        return [ch for ch in wanted if ch in running_types or ch in to_start]
    except Exception as exc:
        logger.warning("[Web] Channel start failed: %s", exc)
        return list(running_types)


async def _api_channels_status():
    """Return status of messaging channels."""
    channels = []
    for bot in _active_bots:
        cls_name = type(bot).__name__
        if "Telegram" in cls_name:
            ch_type = "telegram"
        elif "Discord" in cls_name:
            ch_type = "discord"
        elif "WhatsApp" in cls_name:
            ch_type = "whatsapp"
        else:
            ch_type = cls_name
        channels.append({"type": ch_type, "running": True})

    running_types = {c["type"] for c in channels}

    tg_token = config.get_str("channels", "telegram", "token", default="")
    dc_token = config.get_str("channels", "discord", "token", default="")
    wa_phone = config.get_str("channels", "whatsapp", "phoneNumberId", default="")
    wa_token = config.get_str("channels", "whatsapp", "token", default="")

    if tg_token and "telegram" not in running_types:
        channels.append({"type": "telegram", "running": False, "tokenSet": True})
    if dc_token and "discord" not in running_types:
        channels.append({"type": "discord", "running": False, "tokenSet": True})
    if wa_phone and wa_token and "whatsapp" not in running_types:
        channels.append({"type": "whatsapp", "running": False, "tokenSet": True})

    return {"channels": channels}


async def _api_channels_restart(request: Request):
    """Stop and restart all configured channels."""
    global _active_bots

    for bot in _active_bots:
        if hasattr(bot, "stop_async"):
            try:
                await bot.stop_async()
            except Exception:
                pass
    _active_bots = []

    started = await _maybe_start_channels()
    return {"ok": True, "channels": started}


def _reload_agent_identity() -> None:
    """Reload soul/persona/tools from disk for all live agents."""
    global _agents
    if not _agents:
        return
    from ..core.agent import _load_text_dir_or_file
    home = config.PYTHONCLAW_HOME
    for agent in _agents.values():
        agent.soul_instruction = _load_text_dir_or_file(
            str(home / "context" / "soul"), label="Soul"
        )
        agent.persona_instruction = _load_text_dir_or_file(
            str(home / "context" / "persona"), label="Persona"
        )
        agent.tools_notes = _load_text_dir_or_file(
            str(home / "context" / "tools"), label="Tools"
        )
        agent._needs_onboarding = False
        agent._init_system_prompt()


# ── Files management ──────────────────────────────────────────────────────────

async def _api_clear_files(request: Request):
    """Delete all downloaded/generated files."""
    count = config.clear_files()
    return JSONResponse({"ok": True, "cleared": count})


async def _api_list_files(request: Request):
    """List files in the shared files directory."""
    d = config.files_dir()
    files = []
    for entry in sorted(d.iterdir()):
        if entry.is_file():
            files.append({
                "name": entry.name,
                "size": entry.stat().st_size,
                "modified": entry.stat().st_mtime,
            })
    return JSONResponse({"files": files, "dir": str(d)})


# ── Web file sender ───────────────────────────────────────────────────────────

def _register_web_file_sender(loop: asyncio.AbstractEventLoop, ws: WebSocket) -> None:
    """Register a sync callback so the Agent can push file-download links to the web UI."""
    from ..core.tools import set_file_sender

    def _sender(path: str, caption: str = "") -> None:
        import base64 as _b64

        name = os.path.basename(path)
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            data = _b64.b64encode(fh.read()).decode()

        async def _push():
            try:
                await ws.send_json({
                    "type": "file",
                    "filename": name,
                    "size": size,
                    "caption": caption,
                    "data": data,
                })
            except Exception as exc:
                logger.warning("[Web] send_file via WS failed: %s", exc)

        future = asyncio.run_coroutine_threadsafe(_push(), loop)
        future.result(timeout=60)

    set_file_sender(_sender)


# ── WebSocket Chat ────────────────────────────────────────────────────────────

async def _ws_chat(websocket: WebSocket):
    await websocket.accept()
    logger.info("[Web] WebSocket client connected")

    try:
        while True:
            data = await websocket.receive_text()
            try:
                payload = json.loads(data)
                message = payload.get("message", "").strip()
                image_data = payload.get("image")
                session_id = payload.get("sessionId", "").strip()
            except (json.JSONDecodeError, AttributeError):
                message = data.strip()
                image_data = None
                session_id = None

            if not message and not image_data:
                continue

            agent = _get_agent(session_id)
            if agent is None:
                await websocket.send_json({
                    "type": "error",
                    "content": "LLM provider is not configured yet. Go to the Config tab and set your API key, then save.",
                })
                continue

            if message.startswith("/compact"):
                hint = message[len("/compact"):].strip() or None
                result = agent.compact(instruction=hint)
                await websocket.send_json({"type": "response", "content": result})
                continue

            if message == "/status":
                status = await _api_status()
                await websocket.send_json({"type": "response", "content": json.dumps(status, indent=2)})
                continue

            if message == "/clear":
                if _store:
                    _store.delete(session_id)
                if agent is not None:
                    agent.clear_history()
                await websocket.send_json({"type": "response", "content": "Chat history cleared. Agent is still active with all skills and memory intact."})
                continue

            lock = _get_chat_lock()
            if lock.locked():
                await websocket.send_json({"type": "thinking", "content": "Processing previous message\u2026"})
            else:
                await websocket.send_json({"type": "thinking", "content": ""})

            loop = asyncio.get_event_loop()

            _register_web_file_sender(loop, websocket)

            try:
                token_queue: asyncio.Queue[str | None] = asyncio.Queue()

                def _on_token(text: str) -> None:
                    loop.call_soon_threadsafe(token_queue.put_nowait, text)

                async def _stream_tokens() -> None:
                    while True:
                        tok = await token_queue.get()
                        if tok is None:
                            break
                        try:
                            await websocket.send_json(
                                {"type": "stream", "content": tok}
                            )
                        except Exception:
                            break

                # Build multimodal input if image is attached
                chat_input: str | list = message or ""
                if image_data:
                    chat_input = [
                        {"type": "text", "text": message or "What is in this image?"},
                        {"type": "image_url", "image_url": {"url": image_data}},
                    ]

                async with lock:
                    stream_task = asyncio.create_task(_stream_tokens())
                    try:
                        response = await loop.run_in_executor(
                            None, agent.chat_stream, chat_input, _on_token
                        )
                    finally:
                        loop.call_soon_threadsafe(
                            token_queue.put_nowait, None
                        )
                        await stream_task
                await websocket.send_json(
                    {"type": "response", "content": response}
                )
            except Exception as exc:
                logger.exception("[Web] Chat error")
                await websocket.send_json({"type": "error", "content": str(exc)})

    except WebSocketDisconnect:
        logger.info("[Web] WebSocket client disconnected")
    except Exception:
        logger.exception("[Web] WebSocket error")


# ── SSE Response Builders ───────────────────────────────────────────────────

def build_response(sessionId: str, content: str, end: str, answerType: str = 'AI_LIST', contextId: str = None, currentStep: int = None, questionNo: str = None) -> dict:
    """Build SSE response message.
    
    Parameters
    ----------
    sessionId : Session identifier
    content : Message content
    end : Whether this is the last message (True/False)
    answerType : Type of answer (AI_LIST, STEP_NOTIFICATION, STEP_COMMAND)
    contextId : Optional context/conversation ID
    currentStep : Current step number (for step notifications)
    questionNo : Optional question number
    """
    conversation_id = contextId or str(int(time.time() * 1000))
    
    if answerType == 'STEP_NOTIFICATION':
        # Step notification format
        data = f"data: {json.dumps({'answerType': 'stepName', 'contextEnd': str(end).lower(), 'contextId': conversation_id, 'currentStep': currentStep or 1, 'message': content, 'questionNo': questionNo or '', 'sessionId': sessionId}, ensure_ascii=False)}\n\n"
    else:
        # Normal AI response format
        data = f"data: {json.dumps({'answerStatus': 'SUCCESS', 'answerType': answerType, 'conversationId': conversation_id, 'end': end, 'message': content, 'sessionId': sessionId}, ensure_ascii=False)}\n\n"
    
    return data


def process_step_markers(sessionId: str, text: str, contextId: str = None, questionNo: str = None, step_counter: list = None) -> list[str]:
    """Process step markers in text and return SSE messages.
    
    Detects [STEP_START]...[STEP_END] patterns and converts them to 
    stepName messages, while sending remaining text as normal AI_LIST messages.
    Also handles direct JSON stepCommand messages from tool execution.
    
    Parameters
    ----------
    sessionId : Session identifier
    text : Text content that may contain step markers or JSON stepCommand
    contextId : Optional context/conversation ID
    questionNo : Optional question number
    step_counter : Mutable list to track step count across calls [current_step]
    """
    import re
    import json as _json
    
    if step_counter is None:
        step_counter = [0]
    
    messages = []
    
    # Debug: log the input text
    logger.info("[StepMarker] Processing text: %s", repr(text))
    
    # Check if text contains STEP_RESULT markers
    step_result_pattern = r'\[STEP_RESULT\](.*?)\[STEP_RESULT_END\]'
    step_result_match = re.search(step_result_pattern, text, re.DOTALL)
    if step_result_match:
        json_content = step_result_match.group(1).strip()
        logger.info("[StepMarker] Detected STEP_RESULT marker, extracting JSON content")
        text = json_content
    
    # Check if text is a JSON step message from tool execution
    try:
        if text.strip().startswith('{'):
            parsed = _json.loads(text)
            answer_type = parsed.get("answerType")
            
            # Handle stepCommand, stepContent, conversation, and topology messages directly
            if answer_type in ("stepCommand", "stepContent", "conversation", "topology"):
                logger.info("[StepMarker] Detected direct %s JSON", answer_type)
                
                # Build the response by copying the parsed JSON and filling in missing fields
                response_data = {
                    "answerType": answer_type,
                    "contextEnd": parsed.get("contextEnd", "false"),
                    "contextId": parsed.get("contextId", contextId or str(int(time.time() * 1000))),
                    "currentStep": parsed.get("currentStep", step_counter[0] + 1),
                    "message": parsed.get("message", ""),
                    "questionNo": parsed.get("questionNo", questionNo or ""),
                    "sessionId": parsed.get("sessionId", sessionId)
                }
                
                # Update step counter (但 topology / conversation 的 currentStep=0 不应推进 step_counter)
                try:
                    cs_val = int(response_data["currentStep"])
                except (TypeError, ValueError):
                    cs_val = step_counter[0]
                if cs_val > step_counter[0]:
                    step_counter[0] = cs_val
                
                # Serialize the complete structure as SSE message
                sse_message = f"data: {_json.dumps(response_data, ensure_ascii=False)}\n\n"
                messages.append(sse_message)
                
                logger.info("[StepMarker] Sending %s for step %d", answer_type, response_data["currentStep"])
                return messages
    except (_json.JSONDecodeError, Exception) as e:
        logger.info("[StepMarker] Not a valid JSON: %s", str(e))
        pass  # Not a JSON, continue with normal processing
    
    # Pattern to match [STEP_START]content[STEP_END]
    pattern = r'\[STEP_START\](.*?)\[STEP_END\]'
    
    matches = list(re.finditer(pattern, text))
    logger.info("[StepMarker] Found %d step markers", len(matches))
    
    last_end = 0
    for match in matches:
        # Send any text before the step marker as normal message
        if match.start() > last_end:
            normal_text = text[last_end:match.start()]
            if normal_text.strip():
                logger.info("[StepMarker] Sending normal text: %s", repr(normal_text))
                messages.append(build_response(sessionId, normal_text, False, 'AI_LIST', contextId))
        
        # Increment step counter and send step notification
        step_counter[0] += 1
        step_name = match.group(1).strip()
        logger.info("[StepMarker] Sending step %d: %s", step_counter[0], step_name)
        messages.append(build_response(
            sessionId, 
            step_name, 
            False, 
            'STEP_NOTIFICATION',
            contextId,
            step_counter[0],
            questionNo
        ))
        
        last_end = match.end()
    
    # Send any remaining text after the last step marker
    if last_end < len(text):
        remaining_text = text[last_end:]
        if remaining_text.strip():
            logger.info("[StepMarker] Sending remaining text: %s", repr(remaining_text))
            messages.append(build_response(sessionId, remaining_text, False, 'AI_LIST', contextId))
    
    if not messages:
        logger.info("[StepMarker] No markers found, sending as normal text")
        return [build_response(sessionId, text, False, 'AI_LIST', contextId)]
    
    logger.info("[StepMarker] Total messages generated: %d", len(messages))
    return messages


# ────────────────────────── Chatbot Upload ───────────────────────────────────

async def _api_chatbot_upload(
    sessionId: str = Form(...),
    userId: str = Form(...),
    client: str = Form(...),
    file: UploadFile = File(...),
):
    """Upload a file for a chatbot session.

    The file is saved to ``<upload_dir>/<sessionId>/<filename>``,
    where ``<upload_dir>`` is configured via the ``upload.path`` config key
    (defaults to ``<PYTHONCLAW_HOME>/context/file/``).
    """
    import uuid

    upload_base = config.chatbot_upload_dir()
    session_dir = upload_base / sessionId
    session_dir.mkdir(parents=True, exist_ok=True)

    file_id = uuid.uuid4().hex
    ext = Path(file.filename).suffix if file.filename else ""
    dest = session_dir / f"{file_id}_{ext}"

    try:
        content = await file.read()
        dest.write_bytes(content)
    except Exception as exc:
        logger.exception("[Upload] Failed to save file")
        return JSONResponse(
            {"success": False, "message": "error", "data": None},
            status_code=500,
        )
    finally:
        await file.close()

    logger.info(
        "[Upload] sessionId=%s userId=%s client=%s filename=%s size=%d",
        sessionId, userId, client, dest.name, len(content),
    )

    return {
        "success": True,
        "message": "success",
        "data": {
            "fileId": file_id,
        },
    }


# ────────────────────────── SSE Chat Endpoint ─────────────────────────────────
async def _sse_chat(request: Request):
    """Handle chat request using Server-Sent Events (SSE)."""
    import base64 as b64
    import time
    import uuid
    
    try:
        payload = await request.json()
        message = payload.get("question", "").strip() or payload.get("content", "").strip()
        userId = str(payload.get("userId", "")).strip()
        sessionId = payload.get("sessionId", "").strip()
        if not sessionId:
            sessionId = str(uuid.uuid4()).upper()
        # 前端会传 contextId（必填）和可选 fileId，需要透传到 SSE 上下文
        client_context_id = (payload.get("contextId") or "").strip()
        client_file_id = (payload.get("fileId") or "").strip()
        image_data = payload.get("image")
    except Exception:
        return JSONResponse(
            {"type": "error", "content": "Invalid JSON payload"},
            status_code=400,
        )
    
    if not message and not image_data:
        return JSONResponse(
            {"type": "error", "content": "Message or image is required"},
            status_code=400,
        )
    
    agent = _get_agent(sessionId)
    if agent is None:
        return JSONResponse(
            {"type": "error", 
             "content": "LLM provider is not configured yet. Go to the Config tab and set your API key, then save."},
            status_code=503,
        )
    
    # if message.startswith("/compact"):
    #     hint = message[len("/compact"):].strip() or None
    #     result = agent.compact(instruction=hint)
    #     return JSONResponse({"type": "response", "content": result})
    
    # if message == "/status":
    #     status = await _api_status()
    #     return JSONResponse({"type": "response", "content": status})
    
    # if message == "/clear":
    #     if _store:
    #         _store.delete(sessionId)
    #     if agent is not None:
    #         agent.clear_history()
    #     return JSONResponse(
    #         {"type": "response", 
    #          "content": "Chat history cleared. Agent is still active with all skills and memory intact."}
    #     )
    
    async def generate_sse():
        """Generate SSE stream."""
        loop = asyncio.get_event_loop()
        sse_queue: asyncio.Queue[str | None] = asyncio.Queue()
        
        # 优先使用前端传入的 contextId；缺失时才回退到自动生成
        context_id = client_context_id or str(uuid.uuid4())
        # 按前端约定生成 questionNo，格式：<sessionId><时间戳>
        question_no = f"{sessionId}{int(time.time() * 1000)}"
        step_counter = [0]  # Mutable list to track step count
        logger.info(
            "[SSE] Conversation context bound: sessionId=%s, contextId=%s (from_client=%s), questionNo=%s",
            sessionId, context_id, bool(client_context_id), question_no,
        )
        # 把上下文信息注入 agent，供后端自动触发的步骤/拓扑工具使用
        try:
            if agent is not None:
                agent.frontend_context_id = context_id
                agent.frontend_question_no = question_no
                agent.frontend_session_id = sessionId
                agent.frontend_file_id = client_file_id
        except Exception as bind_err:
            logger.warning("[SSE] Failed to bind frontend context to agent: %s", bind_err)

        def _on_token(text: str) -> None:
            # Process step markers and send appropriate messages
            logger.info("[SSE] _on_token called with text length: %d, preview: %s", len(text), repr(text))
            messages = process_step_markers(sessionId, text, context_id, question_no, step_counter)
            logger.info("[SSE] process_step_markers returned %d messages", len(messages))
            for i, msg in enumerate(messages):
                logger.info("[SSE] Sending message %d: %s", i+1, repr(msg))
                loop.call_soon_threadsafe(sse_queue.put_nowait, msg)

        chat_input: str | list = message or ""

        async def _run_chat():
            try:
                return await loop.run_in_executor(
                    None, agent.chat_stream, chat_input, _on_token
                )
            except Exception as exc:
                logger.exception("[Web] run_in_executor error: %s", str(exc))
            finally:
                loop.call_soon_threadsafe(sse_queue.put_nowait, None)

        chat_task = asyncio.create_task(_run_chat())

        try:
            message_count = 0
            while True:
                sse_chunk = await sse_queue.get()
                if sse_chunk is None:
                    logger.info("[SSE] Received None from queue, breaking loop after %d messages", message_count)
                    break
                message_count += 1
                logger.info("[SSE] Yielding message %d to client: %s", message_count, repr(sse_chunk))
                yield sse_chunk
            logger.info("[SSE] Total messages sent: %d", message_count)
        except Exception as exc:
            logger.exception("[Web] SSE chat error: %s", str(exc))
            if not chat_task.done():
                chat_task.cancel()
        finally:
            final_msg = build_response(sessionId, '', True)
            logger.info("[SSE] Sending final empty response: %s", repr(final_msg))
            yield final_msg
    
    return StreamingResponse(
        generate_sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Step Analysis Endpoint ─────────────────────────────────────────────────

async def _api_step_analyze(request: Request):
    """Analyze device response data using skill step scripts.

    This endpoint receives device response data and calls the appropriate
    Python script to analyze it and determine the next troubleshooting step.

    Expected JSON body (Frontend Integration Format):
    {
        "script_name": "step_executor.py",
        "sessionId": "session_xxx",
        "consoleCmd": "{\"command\": \"output\"}",
        "questionNo": "question_xxx",
        "status": true,
        "currentStep": 2,
        "uuid": "device_uuid"
    }

    Or legacy format:
    {
        "script_name": "step_executor.py",
        "response_data": "<raw device response>",
        "session_id": "session_xxx",
        "analysis_type": "check_route"
    }

    Returns analysis result with decision logic for the next step.
    """
    import subprocess
    import sys

    try:
        body = await request.json()

        # Support frontend integration format
        console_cmd = body.get("consoleCmd")
        if console_cmd:
            # Frontend integration format
            script_name = body.get("script_name", "step_executor.py").strip()
            session_id = body.get("sessionId", "")
            current_step = body.get("currentStep", 1)
            question_no = body.get("questionNo", "")
            status = body.get("status", True)

            # Parse consoleCmd - it's a JSON string containing {command: output} pairs
            command_outputs = {}
            if console_cmd:
                try:
                    command_outputs = json.loads(console_cmd)
                except json.JSONDecodeError as e:
                    return JSONResponse(
                        {"ok": False, "error": f"Failed to parse consoleCmd: {e}"},
                        status_code=400
                    )

            # Combine outputs
            combined_output = ""
            for cmd, output in command_outputs.items():
                combined_output += f"\n# Command: {cmd}\n{output}\n"

            # Prepare params for step_executor.py analyze mode
            params = {
                "analysis_type": body.get("analysis_type", "check_route"),
                "response_data": combined_output,
                "session_id": session_id,
                "current_step": current_step,
                "question_no": question_no,
                "status": status,
                "consoleCmd": console_cmd
            }

            # Find step_executor.py in skill directories
            skill_base = Path(__file__).parent.parent / "templates" / "skills"
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
                return JSONResponse(
                    {"ok": False, "error": f"Script '{script_name}' not found"},
                    status_code=404
                )

            # Execute the script in analyze mode with JSON params
            cmd = [sys.executable, str(script_path), "analyze", json.dumps(params)]
            logger.info("[StepAnalyze] Running: %s", " ".join(cmd))

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode != 0:
                error_msg = result.stderr.strip() if result.stderr else "Unknown error"
                logger.error("[StepAnalyze] Script failed: %s", error_msg)
                return JSONResponse(
                    {"ok": False, "error": f"Analysis failed: {error_msg}"},
                    status_code=500
                )

            output = result.stdout.strip()
            logger.info("[StepAnalyze] Analysis result: %s", output[:200])

            # Parse and return the result
            try:
                analysis_result = json.loads(output)
                return JSONResponse(analysis_result)
            except json.JSONDecodeError:
                return JSONResponse({"ok": True, "result": output})

        # Legacy format support
        script_name = body.get("script_name", "").strip()
        response_data = body.get("response_data", "")
        session_id = body.get("session_id", "")

        if not script_name:
            return JSONResponse(
                {"ok": False, "error": "script_name is required"},
                status_code=400
            )

        if not response_data:
            return JSONResponse(
                {"ok": False, "error": "response_data is required"},
                status_code=400
            )

        # Find the script in skill directories
        skill_base = Path(__file__).parent.parent / "templates" / "skills"
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
            return JSONResponse(
                {"ok": False, "error": f"Script '{script_name}' not found"},
                status_code=404
            )

        # Execute the script in analyze mode
        cmd = [sys.executable, str(script_path), "analyze", response_data]
        logger.info("[StepAnalyze] Running: %s", " ".join(cmd))

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode != 0:
            error_msg = result.stderr.strip() if result.stderr else "Unknown error"
            logger.error("[StepAnalyze] Script failed: %s", error_msg)
            return JSONResponse(
                {"ok": False, "error": f"Analysis failed: {error_msg}"},
                status_code=500
            )

        output = result.stdout.strip()
        logger.info("[StepAnalyze] Analysis result: %s", output[:200])

        # Parse and return the result
        try:
            analysis_result = json.loads(output)
            return {
                "ok": True,
                "result": analysis_result,
                "session_id": session_id
            }
        except json.JSONDecodeError:
            return JSONResponse(
                {"ok": False, "error": f"Invalid JSON from script: {output[:200]}"},
                status_code=500
            )
    
    except subprocess.TimeoutExpired:
        return JSONResponse(
            {"ok": False, "error": "Analysis timed out (30s limit)"},
            status_code=500
        )
    except Exception as exc:
        logger.exception("[StepAnalyze] Unexpected error")
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=500
        )
