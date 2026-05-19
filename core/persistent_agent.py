"""
PersistentAgent — an Agent subclass that automatically saves its message
history to a SessionStore after every chat() or compact() call.

On construction it restores the previous conversation from the store so that
sessions survive server restarts.

Restoration strategy
--------------------
  messages[0]   — always rebuilt fresh by Agent.__init__ (soul + persona + skills)
  messages[1:]  — restored from the Markdown session store

This means soul/persona/skill changes take effect on the next restart while
the full conversation history (including compaction summaries and skill
injection messages) is preserved.

Timestamps
----------
Each message carries a ``_ts`` field (ISO 8601) that records when it was
created.  This enables time-based truncation in the SessionStore.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .agent import Agent

if TYPE_CHECKING:
    from .session_store import SessionStore

logger = logging.getLogger(__name__)


class PersistentAgent(Agent):
    """Agent that auto-saves to and restores from a Markdown SessionStore.
    
    db_meta={"user_id": 39819, "account": "39819", "client": "知了小程序"}
    """
    def __init__(
        self,
        *args,
        store: "SessionStore",
        session_id: str,
        db_meta: dict[str, Any] | None = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("session_id", session_id)
        super().__init__(*args, **kwargs)
        self._store = store
        self._session_id = session_id
        self._db_meta = db_meta or {}
        self._restore()

    # ── Restore ──────────────────────────────────────────────────────────────

    def _restore(self) -> None:
        """Load saved messages and merge with the freshly built system prompt."""
        saved = self._store.load(self._session_id)
        if not saved:
            return

        initial_system = self.messages[0]   # freshly built system prompt

        # Sanitize restored messages to remove broken tool-call sequences
        # that may have been persisted from a previous crash or error.
        saved = self._sanitize_tool_pairs(saved)

        self.messages = [initial_system] + saved

        # Re-infer which skills were loaded so _use_skill doesn't double-inject
        logger.info("[PersistentAgent] Scanning %d saved messages for skill activations", len(saved))
        for msg in saved:
            if msg.get("role") == "system":
                content = msg.get("content", "")
                m = re.search(r"(?:Skill Enabled|SKILL ACTIVATED):\s*(.+)", content)
                if m:
                    skill_name = m.group(1).strip().rstrip("]")
                    logger.info("[PersistentAgent] Found skill activation: '%s'", skill_name)
                    self.loaded_skill_names.add(skill_name)
                    
                    # Extract steps from restored skills
                    try:
                        from ..core.skill_loader import SkillRegistry
                        registry = SkillRegistry()
                        skill = registry.load_skill(skill_name)
                        if skill:
                            step_pattern = r'\[STEP_START\](.*?)\[STEP_END\]'
                            steps = re.findall(step_pattern, skill.instructions)
                            if steps and not hasattr(self, '_current_skill_steps'):
                                self._current_skill_steps = steps
                                logger.info(
                                    "[PersistentAgent] Restored %d steps for skill '%s'",
                                    len(steps), skill_name
                                )
                    except Exception as exc:
                        logger.warning("[PersistentAgent] Failed to restore steps for '%s': %s", skill_name, exc)

        # Inject a fresh memory snapshot so the LLM sees up-to-date context
        # near the end of the history (not just buried in the system prompt).
        self._inject_memory_refresh()

        logger.info(
            "[PersistentAgent] Restored session '%s': %d messages, %d skills",
            self._session_id, len(saved), len(self.loaded_skill_names),
        )

    def _inject_memory_refresh(self) -> None:
        """Append a fresh memory snapshot as a system message.

        Called after session restore so the LLM sees up-to-date long-term
        memory near the latest conversation context, not just the stale
        snapshot in the original system prompt.
        """
        try:
            boot_mem = self.memory.boot_context(max_chars=2000)
        except Exception:
            return
        if not boot_mem:
            return
        self.messages.append({
            "role": "system",
            "content": (
                "[Memory Refresh — session restored]\n"
                "The following is your latest long-term memory. "
                "Use this context to personalize responses.\n\n"
                f"{boot_mem}"
            ),
        })

    # ── Timestamp injection ──────────────────────────────────────────────────

    @staticmethod
    def _ensure_ts(msg: dict) -> dict:
        """Add a ``_ts`` field to a message if it doesn't have one."""
        if "_ts" not in msg:
            msg["_ts"] = datetime.now().isoformat(timespec="seconds")
        return msg

    # ── Auto-save ────────────────────────────────────────────────────────────

    def _save(self) -> None:
        # Ensure every message has a timestamp before saving
        for msg in self.messages[1:]:
            self._ensure_ts(msg)
        self._store.save(self._session_id, self.messages)

    def save_to_db(
        self,
        user_input: str,
        response: str,
        **extra: Any,
    ) -> None:
        """Save conversation record to MySQL database.

        Reads DB connection parameters from the **database** section of
        ``pythonclaw.json``::

            {
              "database": {
                "host": "127.0.0.1",
                "port": 3306,
                "user": "root",
                "password": "",
                "name": "pythonclaw",
                "table": "conversation_log"
              }
            }

        Parameters
        ----------
        user_input : str
            The user's utterance.
        response : str
            The model's final text response.
        **extra :
            Additional fields to store (e.g. user_id, account, client).
            These override any default values from ``self._db_meta``.
        """
        import pymysql

        from .. import config as _cfg

        db_host = _cfg.get_str("database", "host", default="")
        db_port = _cfg.get_int("database", "port", default=3306)
        db_user = _cfg.get_str("database", "user", default="root")
        db_password = _cfg.get_str("database", "password", default="")
        db_name = _cfg.get_str("database", "name", default="")
        db_table = _cfg.get_str("database", "table", default="conversation_log")

        if not db_host or not db_name:
            logger.warning(
                "[PersistentAgent] database.host or database.name not configured, skipping DB save"
            )
            return

        now = datetime.now()
        start_time = int(now.timestamp() * 1000)
        question_no = f"{self._session_id}{start_time}"

        record: dict[str, Any] = {
            "session_id": self._session_id,
            "question_no": question_no,
            "utterance": user_input,
            "model_output": response,
            "content": response,
            "answer_type": "auto",
            "status": "auto",
            "correct": 1,
            "start_time": start_time,
            "end_time": start_time,
            "create_time": now,
            "update_time": now,
        }

        # Merge stored db_meta (lower priority)
        for k, v in self._db_meta.items():
            record.setdefault(k, v)

        # Merge extra kwargs (highest priority)
        for k, v in extra.items():
            record[k] = v

        columns = [f"`{k}`" for k in record]
        placeholders = ["%s"] * len(record)
        values = list(record.values())

        sql = (
            f"INSERT INTO `{db_table}` "
            f"({', '.join(columns)}) VALUES ({', '.join(placeholders)})"
        )

        conn = None
        try:
            conn = pymysql.connect(
                host=db_host,
                port=db_port,
                user=db_user,
                password=db_password,
                database=db_name,
                charset="utf8mb4",
            )
            with conn.cursor() as cursor:
                cursor.execute(sql, values)
            conn.commit()
            logger.info(
                "[PersistentAgent] Saved conversation to DB, session=%s, question_no=%s",
                self._session_id,
                question_no,
            )
        except Exception as exc:
            logger.error("[PersistentAgent] Failed to save to DB: %s", exc)
        finally:
            if conn:
                conn.close()

    def chat(self, user_input: str) -> str:
        response = super().chat(user_input)
        self._save()
        return response

    def chat_stream(self, user_input: str, on_token: callable = None) -> str:
        response = super().chat_stream(user_input, on_token)
        # self._store.save(self._session_id, self.messages)
        # self.save_to_db(user_input, response)
        # self.save_to_db(user_input, response, user_id=39819, account="39819")
        return response

    def compact(self, instruction: str | None = None) -> str:
        result = super().compact(instruction)
        self._save()
        return result
