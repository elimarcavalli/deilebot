"""Tests for _build_transcription_service wiring in cli.py.

AC-FIAÇÃO: factory builds TranscriptionService from settings (same code path as
cli.py), and the service is correctly wired so IngressPipeline.handle() puts the
transcript into inbound_text.

AC-DISABLED: enabled=false → factory returns None (no regression).

AC-WARN: enabled=true + engine=openai + key absent → WARN log emitted.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deilebot.foundation.agent_bridge import AgentInvocation, AgentResponse
from deilebot.foundation.envelope import (
    Attachment,
    AttachmentKind,
    BotUser,
    Channel,
    ChannelScope,
    MessageEnvelope,
)
from deilebot.foundation.pipeline import EgressPipeline, IngressPipeline
from deilebot.foundation.settings import (
    BotSettings,
    FoundationSettings,
    TranscriptionSettings,
)
from deilebot.foundation.transcription import TranscriptionService
from deilebot.foundation.transcription_budget import TranscriptionBudgetTracker


# ── fixtures ─────────────────────────────────────────────────────────────────

_FIXED_TS = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_BOT_USER = BotUser(
    bot_user_id="u1",
    provider="discord",
    provider_user_id="111",
    display_name="TestUser",
)
_CHANNEL = Channel(
    provider="discord",
    provider_channel_id="ch1",
    name="test-channel",
    scope=ChannelScope.DM,
)


def _audio_envelope(text: str = "") -> MessageEnvelope:
    return MessageEnvelope(
        message_id="msg1",
        channel=_CHANNEL,
        author=_BOT_USER,
        sent_at=_FIXED_TS,
        text=text,
        attachments=(
            Attachment(
                kind=AttachmentKind.AUDIO,
                url="http://cdn.example.com/audio.ogg",
                mime="audio/ogg",
                filename="voice.ogg",
            ),
        ),
    )


def _settings(*, enabled: bool, engine: str = "openai") -> BotSettings:
    return BotSettings(
        foundation=FoundationSettings(),
        transcription=TranscriptionSettings(enabled=enabled, engine=engine),
    )


def _mock_bot_settings(*, transcription_enabled: bool = True) -> BotSettings:
    return _settings(enabled=transcription_enabled)


def _build_pipeline_with_service(transcription_service=None):
    bridge = MagicMock()
    invocations: list[AgentInvocation] = []

    async def _capture(inv: AgentInvocation) -> AgentResponse:
        invocations.append(inv)
        try:
            from deile.common.markup_ast import MarkupAST
            markup = MarkupAST.from_plain("ok")
        except Exception:
            markup = None
        return AgentResponse(text="ok", markup=markup)

    bridge.invoke = _capture
    bridge.invocations = invocations

    identity = MagicMock()
    identity.resolve = AsyncMock(return_value=_BOT_USER)

    permissions = MagicMock()
    permissions.check = AsyncMock(return_value=MagicMock(allowed=True, reason="ok"))
    permissions.is_owner = AsyncMock(return_value=False)

    rate_limit = MagicMock()
    rate_limit.acquire_inbound = AsyncMock()

    store = MagicMock()
    store.upsert_user = AsyncMock()
    store.upsert_channel = AsyncMock()
    store.record_inbound = AsyncMock()
    store.get_recent_messages = AsyncMock(return_value=[])

    intent = MagicMock()
    intent.decide = AsyncMock(return_value=MagicMock(should_respond=True, reason="test"))

    capability_catalog = MagicMock()
    capability_catalog.snapshot = AsyncMock(return_value=MagicMock())
    capability_catalog.render_for_system_prompt = MagicMock(return_value="")

    persona_selector = MagicMock()
    persona_selector.resolve = AsyncMock(return_value="developer")

    audit = MagicMock()
    audit.log = AsyncMock()

    event_bus = MagicMock()
    event_bus.publish = AsyncMock()

    metrics = MagicMock()
    metrics.inc = MagicMock()
    metrics.observe = MagicMock()

    egress = MagicMock()
    egress.send_response = AsyncMock()

    agent_meta = MagicMock()

    pipeline = IngressPipeline(
        identity=identity,
        permissions=permissions,
        rate_limit=rate_limit,
        store=store,
        intent=intent,
        bridge=bridge,
        capability_catalog=capability_catalog,
        persona_selector=persona_selector,
        audit=audit,
        event_bus=event_bus,
        metrics=metrics,
        egress=egress,
        agent_meta=agent_meta,
        transcription_service=transcription_service,
    )
    return pipeline, bridge


# ── AC-FIAÇÃO ────────────────────────────────────────────────────────────────


async def test_ac_fiacao_factory_wires_service_and_handle_transcribes():
    """AC-FIAÇÃO: _build_transcription_service (same factory as cli.py) creates a
    TranscriptionService when enabled=true; IngressPipeline.handle() with an AUDIO
    attachment puts the transcript into AgentInvocation.inbound_text.
    """
    from deilebot.cli import _build_transcription_service

    settings = _settings(enabled=True, engine="openai")

    # Patch TranscriptionService at its definition site so the factory's local
    # import picks up the mock. TranscriptionBudgetTracker is NOT patched — its
    # __init__ only stores path/lock and does no I/O.
    with patch.dict(os.environ, {"DEILE_BOT_TRANSCRIPTION_API_KEY": "sk-test-key"}), \
         patch("deilebot.foundation.transcription.TranscriptionService") as MockSvc:
        mock_instance = MagicMock(spec=TranscriptionService)
        mock_instance._settings = TranscriptionSettings(enabled=True)
        mock_instance.transcribe = AsyncMock(return_value="olá mundo")
        MockSvc.return_value = mock_instance

        # Call the factory the same way cli.py does
        svc = _build_transcription_service(settings)

    assert svc is not None, "factory must return a service when enabled=true"
    assert svc is mock_instance, "factory must return the instance from TranscriptionService()"

    # Wire the factory-built service into a pipeline and invoke handle()
    pipeline, bridge = _build_pipeline_with_service(transcription_service=svc)
    env = _audio_envelope(text="")
    adapter = MagicMock()
    adapter.self_user_id = "bot1"
    adapter.name = "discord"

    with patch(
        "deilebot.foundation.pipeline.get_bot_settings",
        return_value=_mock_bot_settings(transcription_enabled=True),
    ):
        await pipeline.handle(env, adapter)

    assert bridge.invocations, "bridge.invoke must be called"
    inv = bridge.invocations[0]
    assert "olá mundo" in inv.inbound_text, (
        f"transcript not in inbound_text: {inv.inbound_text!r}"
    )


# ── AC-DISABLED ───────────────────────────────────────────────────────────────


def test_factory_returns_none_when_disabled():
    """AC-DISABLED: _build_transcription_service returns None when enabled=false."""
    from deilebot.cli import _build_transcription_service

    settings = _settings(enabled=False)
    svc = _build_transcription_service(settings)
    assert svc is None


# ── AC-WARN ───────────────────────────────────────────────────────────────────


def test_factory_warns_and_returns_none_when_openai_key_absent(monkeypatch, caplog):
    """AC-WARN: enabled=true + engine=openai + key absent → WARN log, service=None."""
    from deilebot.cli import _build_transcription_service

    monkeypatch.delenv("DEILE_BOT_TRANSCRIPTION_API_KEY", raising=False)
    settings = _settings(enabled=True, engine="openai")

    with caplog.at_level(logging.WARNING, logger="deilebot.cli"):
        svc = _build_transcription_service(settings)

    assert svc is None, "factory must return None when key is absent"
    warn_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("DEILE_BOT_TRANSCRIPTION_API_KEY" in m for m in warn_msgs), (
        f"expected WARN about missing key, got: {warn_msgs}"
    )


def test_factory_warns_and_returns_none_when_local_path_invalid(caplog):
    """AC-WARN: enabled=true + engine=local + invalid path → WARN log, service=None."""
    from deilebot.cli import _build_transcription_service

    settings = BotSettings(
        foundation=FoundationSettings(),
        transcription=TranscriptionSettings(
            enabled=True,
            engine="local",
            local_model_path=None,  # invalid — required for local engine
        ),
    )

    with caplog.at_level(logging.WARNING, logger="deilebot.cli"):
        svc = _build_transcription_service(settings)

    assert svc is None
    warn_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert warn_msgs, f"expected at least one WARN, got none"
