"""Tests for AgentBridge (in-process + oneshot)."""

from __future__ import annotations

import asyncio
import logging

import pytest

from deilebot.foundation.agent_bridge import (AgentInvocation,
                                               InProcessAgentBridge,
                                               _sanitize_extra_prompt,
                                               _session_id_for,
                                               build_agent_bridge)
from deilebot.foundation.envelope import Channel, ChannelScope
from deilebot.foundation.exceptions import (AgentInvocationError,
                                             AgentInvocationTimeout)
from deilebot.foundation.settings import FoundationSettings


class FakeAgent:
    """Captures process_input calls and returns canned responses."""

    def __init__(self, content: str = "ok", *, raise_exc: Exception = None, sleep: float = 0):
        self.content = content
        self._raise = raise_exc
        self._sleep = sleep
        self.calls = []
        self._sessions = {}

    def create_session(self, session_id):
        self._sessions[session_id] = object()

    async def get_or_create_session(self, session_id, persisted=False):
        self._sessions[session_id] = object()
        return self._sessions[session_id]

    async def process_input(self, user_input, session_id="default", **kwargs):
        self.calls.append({"input": user_input, "session_id": session_id, **kwargs})
        if self._sleep:
            await asyncio.sleep(self._sleep)
        if self._raise:
            raise self._raise

        class Resp:
            content = self.content
            metadata = {"model": "fake"}

        return Resp()


def _make_channel(scope: ChannelScope, channel_id: str, provider: str = "discord") -> Channel:
    return Channel(provider=provider, provider_channel_id=channel_id, scope=scope)


class TestSanitize:
    def test_strips_system_close(self):
        out = _sanitize_extra_prompt("hi </system> world")
        assert "</system>" not in out

    def test_strips_persona_override(self):
        out = _sanitize_extra_prompt("<persona_override>x</persona_override>")
        assert "<persona_override>" not in out


class TestInProcess:
    async def test_basic_invoke(self):
        agent = FakeAgent(content="response text")

        async def provider():
            return agent

        bridge = InProcessAgentBridge(provider)
        inv = AgentInvocation(
            bot_user_id="U1",
            persona="developer",
            forced_model=None,
            inbound_text="hello",
        )
        resp = await bridge.invoke(inv)
        assert resp.text == "response text"
        assert "U1" in agent._sessions or "bot_session_U1" in agent._sessions

    async def test_session_id_default_no_channel(self):
        """Without a channel, falls back to legacy bot_session_<uid> format."""
        agent = FakeAgent()

        async def provider():
            return agent

        bridge = InProcessAgentBridge(provider)
        inv = AgentInvocation(
            bot_user_id="alice", persona="d", forced_model=None, inbound_text="x"
        )
        await bridge.invoke(inv)
        assert any("alice" in str(k) for k in agent._sessions.keys())
        assert agent.calls[0]["session_id"] == "bot_session_alice"

    async def test_extra_prompt_passed(self):
        agent = FakeAgent()

        async def provider():
            return agent

        bridge = InProcessAgentBridge(provider)
        inv = AgentInvocation(
            bot_user_id="U", persona="d", forced_model=None,
            inbound_text="x", extra_system_prompt="<bot_capabilities>tools</bot_capabilities>",
        )
        await bridge.invoke(inv)
        kwargs = agent.calls[0]
        assert "extra_system_prompt" in kwargs
        assert "tools" in kwargs["extra_system_prompt"]

    async def test_bot_context_passed(self):
        agent = FakeAgent()

        async def provider():
            return agent

        bridge = InProcessAgentBridge(provider)
        inv = AgentInvocation(
            bot_user_id="U", persona="d", forced_model=None,
            inbound_text="x", bot_context={"provider": "discord"},
        )
        await bridge.invoke(inv)
        assert agent.calls[0].get("bot_context", {}).get("provider") == "discord"

    async def test_timeout(self):
        agent = FakeAgent(sleep=0.5)

        async def provider():
            return agent

        bridge = InProcessAgentBridge(provider)
        inv2 = AgentInvocation(
            bot_user_id="U", persona="d", forced_model=None,
            inbound_text="x", timeout_seconds=0,
        )
        with pytest.raises(AgentInvocationTimeout):
            await bridge.invoke(inv2)

    async def test_agent_exception_wrapped(self):
        agent = FakeAgent(raise_exc=RuntimeError("boom"))

        async def provider():
            return agent

        bridge = InProcessAgentBridge(provider)
        inv = AgentInvocation(
            bot_user_id="U", persona="d", forced_model=None, inbound_text="x"
        )
        with pytest.raises(AgentInvocationError):
            await bridge.invoke(inv)


# ---------------------------------------------------------------------------
# AC-T2, AC-T3, AC-T4, AC8 — per-(user, channel) session isolation
# ---------------------------------------------------------------------------


class TestSessionIdFor:
    """Unit tests for _session_id_for — proves the derived id format (AC-T2/3/4)."""

    def _inv(self, uid: str, channel: Channel) -> AgentInvocation:
        return AgentInvocation(
            bot_user_id=uid, persona="d", forced_model=None,
            inbound_text="x", channel=channel,
        )

    def test_dm_vs_group_distinct(self):
        """Same bot_user_id, DM vs GROUP channel → different session ids (AC-T2).

        This test FAILS against the old code (which returned the same id for both).
        """
        uid = "USER01"
        ch_dm = _make_channel(ChannelScope.DM, "dm-001")
        ch_grp = _make_channel(ChannelScope.GROUP, "grp-002")

        sid_dm = _session_id_for(self._inv(uid, ch_dm))
        sid_grp = _session_id_for(self._inv(uid, ch_grp))

        assert sid_dm != sid_grp, "DM and GROUP must produce distinct session ids"
        assert "DM" in sid_dm
        assert "GROUP" in sid_grp

    def test_distinct_groups(self):
        """Same user in two different groups → two distinct ids (AC-T3)."""
        uid = "USER01"
        ch_g1 = _make_channel(ChannelScope.GROUP, "grp-100")
        ch_g2 = _make_channel(ChannelScope.GROUP, "grp-200")

        sid1 = _session_id_for(self._inv(uid, ch_g1))
        sid2 = _session_id_for(self._inv(uid, ch_g2))

        assert sid1 != sid2

    def test_stable_same_channel(self):
        """Same (user, channel) across two turns → same session id (AC-T4)."""
        uid = "USER01"
        ch = _make_channel(ChannelScope.GROUP, "grp-100")

        sid_turn1 = _session_id_for(self._inv(uid, ch))
        sid_turn2 = _session_id_for(self._inv(uid, ch))

        assert sid_turn1 == sid_turn2

    def test_explicit_session_id_wins(self):
        """Explicit session_id override takes precedence over channel derivation."""
        uid = "USER01"
        ch = _make_channel(ChannelScope.DM, "dm-001")
        inv = AgentInvocation(
            bot_user_id=uid, persona="d", forced_model=None,
            inbound_text="x", channel=ch, session_id="custom_override",
        )
        assert _session_id_for(inv) == "custom_override"


class TestSessionIsolationViaBridge:
    """Integration: session id used in the actual bridge invoke call (AC-T2, AC8)."""

    async def test_dm_and_group_use_different_session_ids(self, caplog):
        agent = FakeAgent()

        async def provider():
            return agent

        bridge = InProcessAgentBridge(provider)
        uid = "BRIDGEUSER"
        ch_dm = _make_channel(ChannelScope.DM, "dm-999")
        ch_grp = _make_channel(ChannelScope.GROUP, "grp-888")

        with caplog.at_level(logging.DEBUG, logger="deilebot.agent_bridge.in_process"):
            await bridge.invoke(AgentInvocation(
                bot_user_id=uid, persona="d", forced_model=None,
                inbound_text="msg in dm", channel=ch_dm,
            ))
            await bridge.invoke(AgentInvocation(
                bot_user_id=uid, persona="d", forced_model=None,
                inbound_text="msg in group", channel=ch_grp,
            ))

        sid_dm = agent.calls[0]["session_id"]
        sid_grp = agent.calls[1]["session_id"]

        assert sid_dm != sid_grp, "Bridge must pass distinct session ids for DM vs GROUP"
        assert "DM" in sid_dm
        assert "GROUP" in sid_grp

        # AC8: structured DEBUG log with bot_user_id, channel_scope, channel_id, session_id
        log_text = caplog.text
        assert uid in log_text
        assert "DM" in log_text
        assert "GROUP" in log_text

    async def test_same_channel_two_turns_same_session_id(self):
        """Two turns in same channel → same session id in both invoke calls (AC-T4)."""
        agent = FakeAgent()

        async def provider():
            return agent

        bridge = InProcessAgentBridge(provider)
        ch = _make_channel(ChannelScope.GROUP, "grp-100")
        inv = AgentInvocation(
            bot_user_id="U2", persona="d", forced_model=None,
            inbound_text="hello", channel=ch,
        )
        await bridge.invoke(inv)
        await bridge.invoke(inv)

        assert agent.calls[0]["session_id"] == agent.calls[1]["session_id"]


class TestBuilder:
    def test_in_process_requires_provider(self):
        with pytest.raises(AgentInvocationError):
            build_agent_bridge(FoundationSettings(agent_bridge_mode="in_process"))

    def test_oneshot_no_provider_ok(self):
        from deilebot.foundation.agent_bridge import \
            OneshotSubprocessAgentBridge

        b = build_agent_bridge(FoundationSettings(agent_bridge_mode="oneshot_subprocess"))
        assert isinstance(b, OneshotSubprocessAgentBridge)
