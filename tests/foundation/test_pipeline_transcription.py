"""AC-3 (integração handle→inbound_text) e AC-8 (não-regressão enabled=false).
AC-31 (language resolution via resolve_language in _materialize_attachments).
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
from deilebot.foundation.transcription import TranscriptionError, TranscriptionService
from deilebot.foundation.transcription_budget import TranscriptionBudgetTracker


# ── helpers ──────────────────────────────────────────────────────────────────

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


def _text_envelope(text: str = "hello") -> MessageEnvelope:
    return MessageEnvelope(
        message_id="msg2",
        channel=_CHANNEL,
        author=_BOT_USER,
        sent_at=_FIXED_TS,
        text=text,
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


def _build_pipeline(*, transcription_service=None) -> tuple[IngressPipeline, MagicMock]:
    """Returns (pipeline, bridge_mock). bridge_mock.invoke captures AgentInvocations."""
    bridge = MagicMock()
    invocations: list[AgentInvocation] = []

    async def _capture_invoke(inv: AgentInvocation) -> AgentResponse:
        invocations.append(inv)
        try:
            from deile.common.markup_ast import MarkupAST
            markup = MarkupAST.from_plain("ok")
        except Exception:
            markup = None
        return AgentResponse(text="ok", markup=markup)

    bridge.invoke = _capture_invoke
    bridge.invocations = invocations  # expose for assertions

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
        transcription_service=transcription_service,
    )
    return pipeline, bridge


def _mock_bot_settings(*, transcription_enabled: bool = True):
    fs = FoundationSettings(
        forced_model=None,
        default_model=None,
        agent_invocation_timeout_seconds=30,
    )
    ts = TranscriptionSettings(
        enabled=transcription_enabled,
        max_duration_seconds=120,
        max_minutes_per_month=60,
    )
    bs = BotSettings(foundation=fs, transcription=ts)
    return bs


# ── AC-3 — integration: handle() → inbound_text contains transcript ──────────

async def test_handle_audio_only_injects_transcript():
    """AC-3: handle() with audio-only env → AgentInvocation.inbound_text contains transcript."""
    svc = MagicMock(spec=TranscriptionService)
    svc._settings = TranscriptionSettings(enabled=True)
    svc.transcribe = AsyncMock(return_value="olá mundo")

    pipeline, bridge = _build_pipeline(transcription_service=svc)
    env = _audio_envelope(text="")  # audio-only, no text
    adapter = MagicMock()
    adapter.self_user_id = "bot1"
    adapter.name = "discord"

    with patch(
        "deilebot.foundation.pipeline.get_bot_settings",
        return_value=_mock_bot_settings(transcription_enabled=True),
    ):
        await pipeline.handle(env, adapter)

    assert bridge.invocations, "bridge.invoke was not called"
    inv = bridge.invocations[0]
    assert "olá mundo" in inv.inbound_text, (
        f"transcript not in inbound_text: {inv.inbound_text!r}"
    )


async def test_handle_audio_with_text_prepends_transcript():
    """Transcript is prefixed; user text is preserved after it."""
    svc = MagicMock(spec=TranscriptionService)
    svc._settings = TranscriptionSettings(enabled=True)
    svc.transcribe = AsyncMock(return_value="olá mundo")

    pipeline, bridge = _build_pipeline(transcription_service=svc)
    env = _audio_envelope(text="por favor transcreva")
    adapter = MagicMock()
    adapter.self_user_id = "bot1"
    adapter.name = "discord"

    with patch(
        "deilebot.foundation.pipeline.get_bot_settings",
        return_value=_mock_bot_settings(transcription_enabled=True),
    ):
        await pipeline.handle(env, adapter)

    inv = bridge.invocations[0]
    assert "olá mundo" in inv.inbound_text
    assert "por favor transcreva" in inv.inbound_text
    # Transcript prefix comes before user text
    assert inv.inbound_text.index("olá mundo") < inv.inbound_text.index("por favor transcreva")


# ── AC-6 — pipeline swallows STT errors, handle() does not propagate ─────────

async def test_handle_stt_error_does_not_propagate():
    """STT failure must not raise out of handle(); agent still gets invoked."""
    svc = MagicMock(spec=TranscriptionService)
    svc._settings = TranscriptionSettings(enabled=True)
    svc.transcribe = AsyncMock(side_effect=TranscriptionError("timeout"))

    pipeline, bridge = _build_pipeline(transcription_service=svc)
    env = _audio_envelope()
    adapter = MagicMock()
    adapter.self_user_id = "bot1"
    adapter.name = "discord"

    with patch(
        "deilebot.foundation.pipeline.get_bot_settings",
        return_value=_mock_bot_settings(transcription_enabled=True),
    ):
        # Must not raise
        await pipeline.handle(env, adapter)

    # Agent was still invoked (degraded, not crashed)
    assert bridge.invocations


# ── AC-8 — non-regression: enabled=false → AUDIO passes through URL-only ─────

async def test_audio_url_only_when_disabled():
    """AC-8: enabled=false → _materialize_attachments returns URL-only for AUDIO."""
    # No transcription_service at all (or enabled=False) — AUDIO stays URL-only
    from deilebot.foundation.pipeline import IngressPipeline as Pipe

    pipe = Pipe.__new__(Pipe)
    atts = (
        Attachment(
            kind=AttachmentKind.AUDIO,
            url="http://cdn.example.com/v.ogg",
            mime="audio/ogg",
            filename="v.ogg",
        ),
    )
    out = await pipe._materialize_attachments(atts)  # no transcription_service
    assert len(out) == 1
    entry = out[0]
    assert entry["kind"] == "AUDIO"
    assert entry["url"] == "http://cdn.example.com/v.ogg"
    assert "transcript" not in entry
    assert "skip_reason" not in entry


async def test_existing_image_tests_unaffected():
    """AC-8: IMAGE attachment materialization is not affected by transcription changes."""
    from deilebot.foundation.pipeline import IngressPipeline as Pipe

    pipe = Pipe.__new__(Pipe)
    atts = (
        Attachment(
            kind=AttachmentKind.IMAGE,
            url=None,
            mime="image/png",
            filename="pic.png",
        ),
    )
    out = await pipe._materialize_attachments(atts)
    entry = out[0]
    assert entry["kind"] == "IMAGE"
    assert "data_base64" not in entry
    assert "transcript" not in entry


# ── AC-31: language resolution in _materialize_attachments ───────────────────

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


async def test_materialize_passes_resolved_language_pt():
    """AC-31: setting=pt → transcribe() called with language='pt'."""
    from deilebot.foundation.pipeline import IngressPipeline as Pipe

    pipe = Pipe.__new__(Pipe)
    pipe._logger = MagicMock()

    svc = MagicMock(spec=TranscriptionService)
    svc._settings = TranscriptionSettings(enabled=True, language="pt")
    captured: list = []

    async def _mock_transcribe(att, *, language=None):
        captured.append(language)
        return "olá"

    svc.transcribe = _mock_transcribe

    env = _audio_envelope_with_locale(None)
    await pipe._materialize_attachments(env.attachments, transcription_service=svc, env=env)

    assert captured == ["pt"], f"Expected ['pt'], got {captured}"


async def test_materialize_passes_resolved_language_auto():
    """AC-31: setting=auto → transcribe() called with language=None."""
    from deilebot.foundation.pipeline import IngressPipeline as Pipe

    pipe = Pipe.__new__(Pipe)
    pipe._logger = MagicMock()

    svc = MagicMock(spec=TranscriptionService)
    svc._settings = TranscriptionSettings(enabled=True, language="auto")
    captured: list = []

    async def _mock_transcribe(att, *, language=None):
        captured.append(language)
        return "transcript"

    svc.transcribe = _mock_transcribe

    env = _audio_envelope_with_locale("pt-BR")
    await pipe._materialize_attachments(env.attachments, transcription_service=svc, env=env)

    assert captured == [None], f"Expected [None], got {captured}"


async def test_materialize_inherit_guild_from_raw():
    """AC-31: setting=inherit_guild, raw['guild_locale']='pt-BR' → language='pt'."""
    from deilebot.foundation.pipeline import IngressPipeline as Pipe

    pipe = Pipe.__new__(Pipe)
    pipe._logger = MagicMock()

    svc = MagicMock(spec=TranscriptionService)
    svc._settings = TranscriptionSettings(enabled=True, language="inherit_guild")
    captured: list = []

    async def _mock_transcribe(att, *, language=None):
        captured.append(language)
        return "transcript"

    svc.transcribe = _mock_transcribe

    env = _audio_envelope_with_locale("pt-BR")
    await pipe._materialize_attachments(env.attachments, transcription_service=svc, env=env)

    assert captured == ["pt"], f"Expected ['pt'], got {captured}"


async def test_materialize_inherit_guild_no_locale_falls_back_to_none():
    """AC-31: setting=inherit_guild, no guild_locale → language=None (auto-detect)."""
    from deilebot.foundation.pipeline import IngressPipeline as Pipe

    pipe = Pipe.__new__(Pipe)
    pipe._logger = MagicMock()

    svc = MagicMock(spec=TranscriptionService)
    svc._settings = TranscriptionSettings(enabled=True, language="inherit_guild")
    captured: list = []

    async def _mock_transcribe(att, *, language=None):
        captured.append(language)
        return "transcript"

    svc.transcribe = _mock_transcribe

    env = _audio_envelope_with_locale(None)
    await pipe._materialize_attachments(env.attachments, transcription_service=svc, env=env)

    assert captured == [None], f"Expected [None], got {captured}"
