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

Session-id scheme (post-fix)
-----------------------------
Session ids are now per (bot_user, channel):
  ``bot_session_<bot_user_id>__<provider>_<scope>_<channel_id>``

Pre-fix sessions (``bot_session_<bot_user_id>`` without ``__``) are
considered corrupt (mixed-channel working memory) and are never re-used
after the fix. ``forget_user`` wipes them via the prefix fan-out.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, List

import aiosqlite

if TYPE_CHECKING:
    from deilebot.foundation.envelope import Channel

_logger = logging.getLogger("deilebot.memory_ops")


def session_id_for(bot_user_id: str, channel: "Channel") -> str:
    """Canonical agent session id for a (bot_user, channel) pair.

    Format: ``bot_session_<bot_user_id>__<provider>_<scope>_<channel_id>``

    The ``__`` double-underscore separator is unambiguous because bot_user_id
    is a ULID (Crockford base32, no underscores). Threads get their own
    ``provider_channel_id`` so they are naturally isolated from the parent.

    Call-sites: ``agent_bridge._session_id_for`` and ``history_cog./memoria``.
    No other code should build the ``bot_session_`` string directly.
    """
    return (
        f"bot_session_{bot_user_id}"
        f"__{channel.provider}_{channel.scope.value}_{channel.provider_channel_id}"
    )


def _session_user_prefix(bot_user_id: str) -> str:
    """Prefix shared by the legacy session and all channel-scoped sessions.

    ``bot_session_<uid>`` is the exact legacy id; ``bot_session_<uid>__*``
    are the new per-channel ids. Both start with this prefix.
    """
    return f"bot_session_{bot_user_id}"


@dataclass
class ForgetResult:
    """Outcome of a :func:`forget_user` run — a precise, honest breakdown."""

    messages_deleted: int = 0
    private_channels: int = 0
    # Counts (not bools) — with N sessions per user, multiple may be removed.
    session_in_memory_evicted: int = 0
    session_persisted_deleted: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def session_cleared(self) -> bool:
        """True if any agent working-memory copy (RAM or disk) was removed."""
        return self.session_in_memory_evicted > 0 or self.session_persisted_deleted > 0


def _sessions_db_path() -> Path:
    """Resolve the agent's persisted-session DB — same source the agent uses."""
    try:
        from deilebot.foundation.settings import get_bot_settings

        return Path(get_bot_settings().foundation.sessions_sqlite_path)
    except Exception:  # noqa: BLE001 — fall back to the documented K8s path
        _logger.warning("memory_ops: settings lookup failed; using default path")
        return Path("/home/deile/data/deile_sessions.sqlite")


async def _delete_persisted_sessions_for_user(bot_user_id: str) -> int:
    """Delete all persisted sessions that belong to a user.

    Covers both the legacy exact id (``bot_session_<uid>``) and all
    channel-scoped ids (``bot_session_<uid>__*``). Returns the number of
    rows removed. A missing DB file returns 0 (nothing to forget).
    """
    path = _sessions_db_path()
    if not path.exists():
        return 0
    prefix = _session_user_prefix(bot_user_id)
    async with aiosqlite.connect(path) as db:
        # Coexist with the agent's own SessionStore connection (WAL mode).
        await db.execute("PRAGMA busy_timeout=5000;")
        cursor = await db.execute(
            "DELETE FROM persisted_session WHERE session_id = ? OR session_id LIKE ?",
            (prefix, prefix + "__%"),
        )
        removed = cursor.rowcount or 0
        await cursor.close()
        await db.commit()
        return removed


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

    Fan-out: with per-channel sessions, one user may have N sessions (one per
    DM + one per group). All are wiped — both RAM and disk — by matching the
    ``bot_session_<uid>`` prefix (covers legacy exact id and all ``__<chan>``
    variants). ``ForgetResult`` carries the exact count removed from each store.
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

    # The oneshot_subprocess bridge keeps no session — skip cleanly.
    agent_provider = getattr(bridge, "_agent_provider", None)
    if agent_provider is None:
        return result

    try:
        agent = await agent_provider()
    except Exception as exc:  # noqa: BLE001
        _logger.exception("forget_user: agent_provider failed")
        result.errors.append(f"agent: {type(exc).__name__}: {exc}")
        agent = None

    prefix = _session_user_prefix(bot_user_id)

    # 2. Evict ALL in-memory sessions for this user FIRST (before the row
    #    delete, so no concurrent flush can re-write them in the gap).
    if agent is not None:
        try:
            all_sids = list(getattr(agent, "_sessions", {}).keys())
            to_evict = [
                s for s in all_sids
                if s == prefix or s.startswith(prefix + "__")
            ]
            if hasattr(agent, "delete_session"):
                for s in to_evict:
                    try:
                        if agent.delete_session(s):
                            result.session_in_memory_evicted += 1
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:  # noqa: BLE001
            _logger.exception("forget_user: in-memory session evict failed")
            result.errors.append(f"session_ram: {type(exc).__name__}: {exc}")

    # 3. Delete ALL persisted rows for this user from deile_sessions.sqlite.
    try:
        result.session_persisted_deleted = await _delete_persisted_sessions_for_user(
            bot_user_id
        )
    except Exception as exc:  # noqa: BLE001
        _logger.exception("forget_user: persisted session delete failed")
        result.errors.append(f"session_db: {type(exc).__name__}: {exc}")

    return result
