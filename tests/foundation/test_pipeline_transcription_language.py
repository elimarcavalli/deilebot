"""AC-3: Integration tests — MessagePipeline.handle() resolves language from guild_locale.

(a) guild preferred_locale="pt-BR" + language="inherit_guild" → Whisper receives language="pt"
(b) guild preferred_locale="de-DE" + language="inherit_guild" → Whisper called without language (None)
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType
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
    reset_bot_settings_cache,
)
from deilebot.foundation.transcription import TranscriptionService
from deilebot.foundation.transcription_budget import TranscriptionBudgetTracker


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


def _audio_envelope_with_locale(guild_locale: Optional[str]) -> MessageEnvelope:
    raw_data: dict = {}
    if guild_locale is not None:
        raw_data["guild_locale"] = guild_locale
    return MessageEnvelope(
        message_id="msg-locale",
        channel=_CHANNEL,
        author=_BOT_USER,
        sent_at=_FIXED_TS,
        text="",
        attachments=(
            Attachment(
                kind=AttachmentKind.AUDIO,
                url="http://cdn.example.com/audio.ogg",
                mime="audio/ogg",
                filename="voice.ogg",
            ),
        ),
        raw=MappingProxyType(raw_data),
    )


def _mock_intent(should_respond: bool = True):
    d = MagicMock()
    d.should_respond = should_respond
    d.reason = "test"
    return d


def _mock_permission(allowed: bool = True):
    d = MagicMock()
    d.allowed = allowed
    d.reason = "ok"
    return d


def _build_pipeline_with_stt(stt_service) -> tuple:
    """Build IngressPipeline with a mock STT client, return (pipeline, bridge_mock)."""
    bridge = MagicMock()
    invocations: list = []

    async def _capture_invoke(inv: AgentInvocation) -> AgentResponse:
        invocations.append(inv)
        try:
            from deile.common.markup_ast import MarkupAST
            markup = MarkupAST.from_plain("ok")
        except Exception:
            markup = None
        return AgentResponse(text="ok", markup=markup)

    bridge.invoke = _capture_invoke
    bridge.invocations = invocations

    identity = MagicMock()
    identity.resolve = AsyncMock(return_value=_BOT_USER)

    permissions = MagicMock()
    permissions.check = AsyncMock(return_value=_mock_permission())
    permissions.is_owner = AsyncMock(return_value=False)

    rate_limit = MagicMock()
    rate_limit.acquire_inbound = AsyncMock()

    store = MagicMock()
    store.upsert_user = AsyncMock()
    store.upsert_channel = AsyncMock()
    store.record_inbound = AsyncMock()
    store.get_recent_messages = AsyncMock(return_value=[])

    intent = MagicMock()
    intent.decide = AsyncMock(return_value=_mock_intent())

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
        transcription_service=stt_service,
    )
    return pipeline, bridge


def _mock_bot_settings(*, transcription_enabled: bool = True, language: str = "inherit_guild"):
    fs = FoundationSettings(
        forced_model=None,
        default_model=None,
        agent_invocation_timeout_seconds=30,
    )
    ts = TranscriptionSettings(
        enabled=transcription_enabled,
        max_duration_seconds=120,
        max_minutes_per_month=60,
        language=language,
    )
    return BotSettings(foundation=fs, transcription=ts)


# ── AC-3(a): guild preferred_locale="pt-BR" + inherit_guild → language="pt" ─────────────

async def test_handle_inherit_guild_ptbr_resolves_to_pt():
    """AC-3(a): env with guild preferred_locale='pt-BR' + language='inherit_guild'
    → Whisper receives language='pt'.
    """
    captured_languages: list = []

    svc = MagicMock(spec=TranscriptionService)
    svc._settings = TranscriptionSettings(enabled=True, language="inherit_guild")

    async def _mock_transcribe(att, *, language=None):
        captured_languages.append(language)
        return "olá mundo"

    svc.transcribe = _mock_transcribe

    pipeline, bridge = _build_pipeline_with_stt(svc)
    env = _audio_envelope_with_locale("pt-BR")
    adapter = MagicMock()
    adapter.self_user_id = "bot1"
    adapter.name = "discord"

    with patch(
        "deilebot.foundation.pipeline.get_bot_settings",
        return_value=_mock_bot_settings(language="inherit_guild"),
    ):
        await pipeline.handle(env, adapter)

    assert captured_languages, "transcribe() was not called"
    assert captured_languages[0] == "pt", (
        f"Expected language='pt' for pt-BR guild, got {captured_languages[0]!r}"
    )


# ── AC-3(b): guild preferred_locale="de-DE" + inherit_guild → language=None ─────────────

async def test_handle_inherit_guild_dede_falls_back_to_none():
    """AC-3(b): env with guild preferred_locale='de-DE' + language='inherit_guild'
    → Whisper called without language (receives None → auto-detect).
    """
    captured_languages: list = []

    svc = MagicMock(spec=TranscriptionService)
    svc._settings = TranscriptionSettings(enabled=True, language="inherit_guild")

    async def _mock_transcribe(att, *, language=None):
        captured_languages.append(language)
        return "transcript"

    svc.transcribe = _mock_transcribe

    pipeline, bridge = _build_pipeline_with_stt(svc)
    env = _audio_envelope_with_locale("de-DE")
    adapter = MagicMock()
    adapter.self_user_id = "bot1"
    adapter.name = "discord"

    with patch(
        "deilebot.foundation.pipeline.get_bot_settings",
        return_value=_mock_bot_settings(language="inherit_guild"),
    ):
        await pipeline.handle(env, adapter)

    assert captured_languages, "transcribe() was not called"
    assert captured_languages[0] is None, (
        f"Expected language=None for de-DE guild, got {captured_languages[0]!r}"
    )
