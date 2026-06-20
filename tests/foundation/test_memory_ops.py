"""Tests for memory_ops — session_id_for helper and forget_user fan-out.

AC-T1: format of session_id_for
AC-T5: forget_user wipes all sessions (1 legacy + 2 channel-scoped)
AC-T6: legacy session is not re-used after fix
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any, Dict

import aiosqlite
import pytest

from deilebot.foundation.envelope import Channel, ChannelScope
from deilebot.foundation.memory_ops import (
    ForgetResult,
    _session_user_prefix,
    forget_user,
    session_id_for,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dm(provider: str = "discord", channel_id: str = "dm-123") -> Channel:
    return Channel(provider=provider, provider_channel_id=channel_id, scope=ChannelScope.DM)


def _group(provider: str = "discord", channel_id: str = "grp-456") -> Channel:
    return Channel(provider=provider, provider_channel_id=channel_id, scope=ChannelScope.GROUP)


# ---------------------------------------------------------------------------
# AC-T1 — exact format of session_id_for
# ---------------------------------------------------------------------------


class TestSessionIdFor:
    def test_format_dm(self):
        ch = _dm(provider="discord", channel_id="123")
        sid = session_id_for("USERABC", ch)
        assert sid == "bot_session_USERABC__discord_DM_123"

    def test_format_group(self):
        ch = _group(provider="discord", channel_id="456")
        sid = session_id_for("USERABC", ch)
        assert sid == "bot_session_USERABC__discord_GROUP_456"

    def test_double_underscore_separator(self):
        ch = _dm()
        sid = session_id_for("UID", ch)
        assert "__" in sid
        parts = sid.split("__", 1)
        assert parts[0] == "bot_session_UID"

    def test_contains_channel_id(self):
        ch = Channel(provider="telegram", provider_channel_id="99999", scope=ChannelScope.DM)
        sid = session_id_for("U", ch)
        assert "99999" in sid
        assert "telegram" in sid


# ---------------------------------------------------------------------------
# AC-T5 — forget_user wipes ALL sessions (1 legacy + 2 channel-scoped)
# ---------------------------------------------------------------------------


class FakeSession:
    def __init__(self):
        self.context_data: dict = {}


class FakeAgent:
    def __init__(self):
        self._sessions: Dict[str, Any] = {}

    def create_session(self, sid: str) -> None:
        self._sessions[sid] = FakeSession()

    def get_session(self, sid: str):
        return self._sessions.get(sid)

    def delete_session(self, sid: str) -> bool:
        return self._sessions.pop(sid, None) is not None


class FakeBridgeWithAgent:
    def __init__(self, agent: FakeAgent):
        self._fake_agent = agent

    @property
    def _agent_provider(self):
        async def _provider():
            return self._fake_agent
        return _provider


class FakeStore:
    async def delete_user_history(self, bot_user_id: str) -> dict:
        return {"messages": 0, "private_channels": 0}


async def _seed_db(db_path: Path, session_ids: list[str]) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS persisted_session "
            "(session_id TEXT PRIMARY KEY, working_directory TEXT, "
            "context_data_json TEXT, created_at TEXT, last_used_at TEXT)"
        )
        for sid in session_ids:
            await db.execute(
                "INSERT OR REPLACE INTO persisted_session VALUES (?,?,?,?,?)",
                (sid, "/", "{}", "2026-01-01", "2026-01-02"),
            )
        await db.commit()


async def _count_rows(db_path: Path, pattern: str) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM persisted_session WHERE session_id = ? OR session_id LIKE ?",
            (pattern, pattern + "__%"),
        )
        row = await cur.fetchone()
        return row[0]


@pytest.mark.asyncio
async def test_forget_wipes_all_channel_sessions(tmp_path, monkeypatch):
    """1 legacy + 2 channel sessions → all wiped; counts are exact (AC-T5)."""
    uid = "01TESTUID123456789012345"
    prefix = _session_user_prefix(uid)

    legacy_sid = prefix
    dm_sid = f"{prefix}__discord_DM_111"
    grp_sid = f"{prefix}__discord_GROUP_222"

    db_path = tmp_path / "deile_sessions.sqlite"
    await _seed_db(db_path, [legacy_sid, dm_sid, grp_sid])

    agent = FakeAgent()
    for sid in [legacy_sid, dm_sid, grp_sid]:
        agent.create_session(sid)

    assert len(agent._sessions) == 3

    monkeypatch.setattr(
        "deilebot.foundation.memory_ops._sessions_db_path",
        lambda: db_path,
    )

    result = await forget_user(
        store=FakeStore(),
        bridge=FakeBridgeWithAgent(agent),
        bot_user_id=uid,
    )

    assert result.session_in_memory_evicted == 3, f"RAM evicted={result.session_in_memory_evicted}"
    assert result.session_persisted_deleted == 3, f"DB deleted={result.session_persisted_deleted}"
    assert len(agent._sessions) == 0
    assert await _count_rows(db_path, prefix) == 0
    assert not result.errors


# ---------------------------------------------------------------------------
# AC-T6 — legacy session is NOT re-used after fix
# ---------------------------------------------------------------------------


def test_legacy_session_not_reused():
    """A session with the old id (no __) is never returned by session_id_for."""
    uid = "LEGACYUID"
    legacy_sid = f"bot_session_{uid}"

    ch_dm = _dm()
    ch_grp = _group()

    new_dm = session_id_for(uid, ch_dm)
    new_grp = session_id_for(uid, ch_grp)

    assert new_dm != legacy_sid, "DM session must not match legacy id"
    assert new_grp != legacy_sid, "GROUP session must not match legacy id"
    assert "__" in new_dm
    assert "__" in new_grp


# ---------------------------------------------------------------------------
# Extra: session_cleared property
# ---------------------------------------------------------------------------


def test_forget_result_session_cleared_with_counts():
    r = ForgetResult(session_in_memory_evicted=2, session_persisted_deleted=1)
    assert r.session_cleared is True


def test_forget_result_session_cleared_false():
    r = ForgetResult()
    assert r.session_cleared is False
