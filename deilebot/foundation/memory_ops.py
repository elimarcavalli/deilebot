"""memory_ops — shared core for the privacy / forget commands.

`/forget_me` (PrivacyCog, self-service) and `/forget` (AdminCog, owner)
both wipe a user's footprint. Doing it RIGHT means clearing THREE stores,
not one:

  1. conversation_store ``message`` rows — the transcript ``/historico``
     reads and the per-channel window the intent classifier consumes.
  2. the agent's IN-MEMORY ``AgentSession`` (``agent._sessions[sid]``) —
     the live working memory of the running bot process.
  3. the agent's PERSISTED session row (``persisted_session`` in
     ``deile_sessions.sqlite``) — what survives a bot restart.

Skipping (2) and (3) was the original bug: the old ``/forget_me`` called
two methods that never existed (``get_user_by_provider`` /
``delete_messages_for``), reported "0 mensagens, 0 sessões", and the agent
kept reciting the whole conversation because its ``AgentSession`` — the
thing it actually remembers from — was never touched.

The orchestrator below clears all three and reports a precise, honest
breakdown. Both the cogs and the ``/v1/test/simulate`` harness call THIS
function, so test and production exercise the exact same path.

deilebot owns no schema in ``deile_sessions.sqlite``; it reaches into the
``persisted_session`` table the same way ``/memoria`` already reads it —
a short-lived ``aiosqlite`` connection. WAL + ``busy_timeout`` makes that
safe alongside the agent's own ``SessionStore`` connection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List

import aiosqlite

_logger = logging.getLogger("deilebot.memory_ops")


def session_id_for(bot_user_id: str) -> str:
    """Canonical agent session id for a bot_user.

    MUST stay in sync with ``agent_bridge._session_id_for`` (which builds
    ``bot_session_<bot_user_id>``) and with ``/memoria`` in history_cog.
    """
    return f"bot_session_{bot_user_id}"


@dataclass
class ForgetResult:
    """Outcome of a :func:`forget_user` run — a precise, honest breakdown."""

    messages_deleted: int = 0
    private_channels: int = 0
    session_in_memory_evicted: bool = False
    session_persisted_deleted: bool = False
    errors: List[str] = field(default_factory=list)

    @property
    def session_cleared(self) -> bool:
        """True if any agent working-memory copy (RAM or disk) was removed."""
        return self.session_in_memory_evicted or self.session_persisted_deleted


def _sessions_db_path() -> Path:
    """Resolve the agent's persisted-session DB — same source the agent uses."""
    try:
        from deilebot.foundation.settings import get_bot_settings

        return Path(get_bot_settings().foundation.sessions_sqlite_path)
    except Exception:  # noqa: BLE001 — fall back to the documented K8s path
        _logger.warning("memory_ops: settings lookup failed; using default path")
        return Path("/home/deile/data/deile_sessions.sqlite")


async def _delete_persisted_session(session_id: str) -> bool:
    """Delete one row from the agent's ``persisted_session`` table.

    Returns True if a row was removed. A missing DB file (agent never ran
    with persisted sessions) is not an error — there is simply nothing to
    forget, so it returns False.
    """
    path = _sessions_db_path()
    if not path.exists():
        return False
    async with aiosqlite.connect(path) as db:
        # Coexist with the agent's own SessionStore connection (WAL mode).
        await db.execute("PRAGMA busy_timeout=5000;")
        cursor = await db.execute(
            "DELETE FROM persisted_session WHERE session_id = ?", (session_id,)
        )
        removed = cursor.rowcount or 0
        await cursor.close()
        await db.commit()
        return removed > 0


async def forget_user(
    *,
    store: Any,
    bridge: Any,
    bot_user_id: str,
) -> ForgetResult:
    """Wipe every trace of a user's conversation memory. See module docstring.

    Args:
        store:        the ``ConversationStore`` (``pipeline.store``).
        bridge:       the ``AgentBridge`` (``pipeline.bridge``) — used to
                      reach the in-process agent for session eviction.
        bot_user_id:  the canonical ULID of the user to forget.

    Never raises: each step is isolated; failures land in ``result.errors``
    so the caller can report a partial wipe honestly instead of crashing.
    """
    result = ForgetResult()

    # 1. conversation_store transcript + per-channel window.
    try:
        counts = await store.delete_user_history(bot_user_id)
        result.messages_deleted = int(counts.get("messages", 0))
        result.private_channels = int(counts.get("private_channels", 0))
    except Exception as exc:  # noqa: BLE001
        _logger.exception("forget_user: conversation_store wipe failed")
        result.errors.append(f"transcript: {type(exc).__name__}: {exc}")

    sid = session_id_for(bot_user_id)

    # The oneshot_subprocess bridge keeps no session — nothing in RAM and
    # nothing persisted under this sid. Skip cleanly.
    agent_provider = getattr(bridge, "_agent_provider", None)
    if agent_provider is None:
        return result

    try:
        agent = await agent_provider()
    except Exception as exc:  # noqa: BLE001
        _logger.exception("forget_user: agent_provider failed")
        result.errors.append(f"agent: {type(exc).__name__}: {exc}")
        agent = None

    # 2. Evict the LIVE in-memory session FIRST. Doing this before the row
    #    delete means no concurrent ``flush_persisted_sessions`` can
    #    re-write the row in the gap between the two steps.
    if agent is not None:
        try:
            if hasattr(agent, "delete_session"):
                result.session_in_memory_evicted = bool(
                    agent.delete_session(sid)
                )
        except Exception as exc:  # noqa: BLE001
            _logger.exception("forget_user: in-memory session evict failed")
            result.errors.append(f"session_ram: {type(exc).__name__}: {exc}")

    # 3. Delete the persisted row from deile_sessions.sqlite.
    try:
        result.session_persisted_deleted = await _delete_persisted_session(sid)
    except Exception as exc:  # noqa: BLE001
        _logger.exception("forget_user: persisted session delete failed")
        result.errors.append(f"session_db: {type(exc).__name__}: {exc}")

    return result
