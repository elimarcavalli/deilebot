"""Tests for HistoryCog /memoria — per-channel session lookup (AC-T7)."""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

from deilebot.foundation.memory_ops import _session_user_prefix, session_id_for
from deilebot.foundation.envelope import Channel, ChannelScope


# ---------------------------------------------------------------------------
# Helpers — simulate the query the /memoria command runs (AC-T7)
# ---------------------------------------------------------------------------


def _seed_sessions(db_path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS persisted_session "
        "(session_id TEXT PRIMARY KEY, working_directory TEXT, "
        "context_data_json TEXT, created_at TEXT, last_used_at TEXT)"
    )
    for row in rows:
        conn.execute(
            "INSERT OR REPLACE INTO persisted_session VALUES (?,?,?,?,?)", row
        )
    conn.commit()
    conn.close()


def _query_sessions(db_path: Path, uid: str) -> list[str]:
    """Mirror of the /memoria query — returns session_ids found for a user."""
    prefix = _session_user_prefix(uid)
    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        "SELECT session_id FROM persisted_session "
        "WHERE session_id = ? OR session_id LIKE ? ORDER BY last_used_at DESC",
        (prefix, prefix + "__%"),
    )
    results = [r[0] for r in cur.fetchall()]
    conn.close()
    return results


# ---------------------------------------------------------------------------
# AC-T7 — /memoria per channel: lookup correct by channel
# ---------------------------------------------------------------------------


class TestMemoriaPerChannel:
    def test_memoria_query_returns_per_channel_sessions(self, tmp_path):
        """The /memoria DB query finds all channel-scoped sessions for a user."""
        uid = "01TESTMEM1234567890ABC"
        ch_dm = Channel(provider="discord", provider_channel_id="dm-11", scope=ChannelScope.DM)
        ch_grp = Channel(provider="discord", provider_channel_id="grp-22", scope=ChannelScope.GROUP)

        sid_dm = session_id_for(uid, ch_dm)
        sid_grp = session_id_for(uid, ch_grp)
        unrelated = "bot_session_ANOTHERUSERID"

        db_path = tmp_path / "deile_sessions.sqlite"
        _seed_sessions(db_path, [
            (sid_dm,  "/", "{}", "2026-01-01", "2026-01-02"),
            (sid_grp, "/", "{}", "2026-01-01", "2026-01-03"),
            (unrelated, "/", "{}", "2026-01-01", "2026-01-04"),
        ])

        found = _query_sessions(db_path, uid)

        assert sid_dm in found
        assert sid_grp in found
        assert unrelated not in found

    def test_memoria_query_does_not_confuse_dm_with_group(self, tmp_path):
        """Filtering by channel_id in the session key isolates the correct session."""
        uid = "01TESTMEM1234567890ABC"
        ch_dm = Channel(provider="discord", provider_channel_id="dm-11", scope=ChannelScope.DM)
        ch_grp = Channel(provider="discord", provider_channel_id="grp-22", scope=ChannelScope.GROUP)

        sid_dm = session_id_for(uid, ch_dm)
        sid_grp = session_id_for(uid, ch_grp)

        db_path = tmp_path / "deile_sessions.sqlite"
        _seed_sessions(db_path, [
            (sid_dm,  "/", '{"note": "dm data"}', "2026-01-01", "2026-01-02"),
            (sid_grp, "/", '{"note": "group data"}', "2026-01-01", "2026-01-03"),
        ])

        all_sessions = _query_sessions(db_path, uid)

        # Filter as /memoria does with channel_id="dm-11"
        dm_sessions = [s for s in all_sessions if "dm-11" in s]
        grp_sessions = [s for s in all_sessions if "grp-22" in s]

        assert dm_sessions == [sid_dm]
        assert grp_sessions == [sid_grp]
        # The DM session is not included when filtering for group channel
        assert sid_dm not in grp_sessions
        assert sid_grp not in dm_sessions

    def test_memoria_query_finds_legacy_session(self, tmp_path):
        """The query also covers the legacy (no-channel) session id for backward compat."""
        uid = "01TESTMEM1234567890ABC"
        legacy_sid = _session_user_prefix(uid)  # exact legacy id, no '__'

        db_path = tmp_path / "deile_sessions.sqlite"
        _seed_sessions(db_path, [
            (legacy_sid, "/", "{}", "2026-01-01", "2026-01-01"),
        ])

        found = _query_sessions(db_path, uid)
        assert legacy_sid in found


# ---------------------------------------------------------------------------
# Structural: /memoria command accepts channel_id parameter
# ---------------------------------------------------------------------------


class TestMemoriaCommandShape:
    def test_memoria_accepts_channel_id_param(self):
        """Verify that the /memoria command signature now includes channel_id."""
        import inspect
        from deilebot.providers.discord.cogs.history_cog import HistoryCog

        # The hybrid_command decorator wraps the function; look at the callback.
        cmd = HistoryCog.memoria
        callback = getattr(cmd, "callback", None) or getattr(cmd, "__func__", None) or cmd
        sig = inspect.signature(callback)
        assert "channel_id" in sig.parameters, (
            "/memoria must accept channel_id for per-channel lookup (AC-T7)"
        )

    def test_memoria_uses_session_user_prefix(self):
        """Verify /memoria source uses the canonical prefix helper, not raw string."""
        from deilebot.providers.discord.cogs import history_cog
        src = inspect.getsource(history_cog)
        assert "_session_user_prefix" in src, (
            "/memoria must use _session_user_prefix, not raw 'bot_session_' string"
        )
        # The raw hardcoded string should only appear in memory_ops, not history_cog
        assert 'f"bot_session_{' not in src, (
            "history_cog must not build bot_session_ string directly"
        )
