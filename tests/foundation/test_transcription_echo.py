"""Tests for echo_transcript feature (issue #33).

AC-1: echo_transcript=False (default) → reply without prefix.
AC-2: echo_transcript=True → reply prefixed; truncation at echo_max_chars + '…'.
AC-3: integration — handle() path with real EgressPipeline, adapter.send_message asserted.
AC-4: message without audio → never prefixed even when echo_transcript=True.
"""
from __future__ import annotations

from datetime import datetime, timezone
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
from deilebot.foundation.pipeline import (
    EgressPipeline,
    IngressPipeline,
    _build_transcript_echo,
)
from deilebot.foundation.settings import (
    BotSettings,
    FoundationSettings,
    TranscriptionSettings,
    reset_bot_settings_cache,
)
from deilebot.foundation.transcription import TranscriptionService


# ── shared fixtures ───────────────────────────────────────────────────────────

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


def _bot_settings(*, echo_transcript: bool, echo_max_chars: int = 200) -> BotSettings:
    fs = FoundationSettings(
        forced_model=None,
        default_model=None,
        agent_invocation_timeout_seconds=30,
    )
    ts = TranscriptionSettings(
        enabled=True,
        max_duration_seconds=120,
        max_minutes_per_month=60,
        echo_transcript=echo_transcript,
        echo_max_chars=echo_max_chars,
    )
    return BotSettings(foundation=fs, transcription=ts)


def _build_ingress_with_mock_egress(*, transcription_service=None):
    """Returns (IngressPipeline, egress_mock, bridge_invocations)."""
    bridge = MagicMock()
    invocations: list[AgentInvocation] = []

    async def _capture_invoke(inv: AgentInvocation) -> AgentResponse:
        invocations.append(inv)
        return AgentResponse(text="resposta", markup=None)

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
        transcription_service=transcription_service,
    )
    return pipeline, egress, invocations


def _build_ingress_with_real_egress(*, transcription_service=None):
    """Returns (IngressPipeline, adapter_mock). EgressPipeline is real."""
    # shared mocks
    store = MagicMock()
    store.upsert_user = AsyncMock()
    store.upsert_channel = AsyncMock()
    store.record_inbound = AsyncMock()
    store.record_outbound = AsyncMock()
    store.get_recent_messages = AsyncMock(return_value=[])

    audit = MagicMock()
    audit.log = AsyncMock()

    event_bus = MagicMock()
    event_bus.publish = AsyncMock()

    metrics = MagicMock()
    metrics.inc = MagicMock()
    metrics.observe = MagicMock()

    dlq = MagicMock()
    dlq.enqueue = AsyncMock()

    rate_limit = MagicMock()
    rate_limit.acquire_inbound = AsyncMock()

    egress = EgressPipeline(
        formatters={},
        rate_limit=rate_limit,
        store=store,
        audit=audit,
        event_bus=event_bus,
        metrics=metrics,
        dlq=dlq,
    )

    bridge = MagicMock()

    async def _invoke(inv: AgentInvocation) -> AgentResponse:
        return AgentResponse(text="resposta do agente", markup=None)

    bridge.invoke = _invoke

    identity = MagicMock()
    identity.resolve = AsyncMock(return_value=_BOT_USER)

    permissions = MagicMock()
    permissions.check = AsyncMock(return_value=_mock_permission())
    permissions.is_owner = AsyncMock(return_value=False)

    intent = MagicMock()
    intent.decide = AsyncMock(return_value=_mock_intent())

    capability_catalog = MagicMock()
    capability_catalog.snapshot = AsyncMock(return_value=MagicMock())
    capability_catalog.render_for_system_prompt = MagicMock(return_value="")

    persona_selector = MagicMock()
    persona_selector.resolve = AsyncMock(return_value="developer")

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

    adapter = MagicMock()
    adapter.self_user_id = "bot1"
    adapter.name = "discord"
    adapter.send_message = AsyncMock(return_value="sent-msg-id")
    adapter.react = AsyncMock()
    adapter.send_typing = AsyncMock()

    return pipeline, adapter


# ── AC-1: default off → no prefix ────────────────────────────────────────────

async def test_ac1_default_off_no_prefix():
    """AC-1: echo_transcript=False (default) → send_response called without transcript_echo."""
    svc = MagicMock(spec=TranscriptionService)
    svc._settings = TranscriptionSettings(enabled=True)
    svc.transcribe = AsyncMock(return_value="olá mundo")

    pipeline, egress, _ = _build_ingress_with_mock_egress(transcription_service=svc)
    env = _audio_envelope(text="")
    adapter = MagicMock()
    adapter.self_user_id = "bot1"
    adapter.name = "discord"
    adapter.react = AsyncMock()
    adapter.send_typing = AsyncMock()

    with patch(
        "deilebot.foundation.pipeline.get_bot_settings",
        return_value=_bot_settings(echo_transcript=False),
    ):
        await pipeline.handle(env, adapter)

    egress.send_response.assert_called_once()
    _, kwargs = egress.send_response.call_args[0], egress.send_response.call_args[1]
    assert kwargs.get("transcript_echo") is None, (
        f"Expected transcript_echo=None but got {kwargs.get('transcript_echo')!r}"
    )


# ── AC-2: on + truncation (unit tests of helper + pipeline) ──────────────────

def test_ac2_helper_short_transcript_no_ellipsis():
    """AC-2 unit: short transcript → no ellipsis."""
    prefix = _build_transcript_echo("olá", 200)
    assert prefix == '🎙️ Transcrevi seu áudio: "olá"\n'
    assert "…" not in prefix


def test_ac2_helper_long_transcript_truncated():
    """AC-2 unit: transcript exceeding max_chars → truncated at exactly max_chars + '…'."""
    long = "x" * 300
    prefix = _build_transcript_echo(long, 200)
    assert prefix == f'🎙️ Transcrevi seu áudio: "{"x" * 200}…"\n'


def test_ac2_helper_exact_boundary():
    """AC-2 unit: transcript at exactly max_chars → no ellipsis."""
    exact = "a" * 200
    prefix = _build_transcript_echo(exact, 200)
    assert "…" not in prefix
    assert f'"{"a" * 200}"' in prefix


async def test_ac2_pipeline_echo_on_calls_send_response_with_prefix():
    """AC-2 pipeline: echo_transcript=True → send_response called with non-None transcript_echo."""
    svc = MagicMock(spec=TranscriptionService)
    svc._settings = TranscriptionSettings(enabled=True)
    svc.transcribe = AsyncMock(return_value="olá mundo")

    pipeline, egress, _ = _build_ingress_with_mock_egress(transcription_service=svc)
    env = _audio_envelope(text="")
    adapter = MagicMock()
    adapter.self_user_id = "bot1"
    adapter.name = "discord"
    adapter.react = AsyncMock()
    adapter.send_typing = AsyncMock()

    with patch(
        "deilebot.foundation.pipeline.get_bot_settings",
        return_value=_bot_settings(echo_transcript=True, echo_max_chars=200),
    ):
        await pipeline.handle(env, adapter)

    egress.send_response.assert_called_once()
    kwargs = egress.send_response.call_args[1]
    echo = kwargs.get("transcript_echo")
    assert echo is not None, "transcript_echo should be set when echo_transcript=True"
    assert echo.startswith("🎙️ Transcrevi seu áudio:")
    assert "olá mundo" in echo


async def test_ac2_pipeline_long_transcript_truncated():
    """AC-2 pipeline: long transcript is truncated in the echo prefix."""
    long_transcript = "palavra " * 50  # 400 chars
    svc = MagicMock(spec=TranscriptionService)
    svc._settings = TranscriptionSettings(enabled=True)
    svc.transcribe = AsyncMock(return_value=long_transcript.strip())

    pipeline, egress, _ = _build_ingress_with_mock_egress(transcription_service=svc)
    env = _audio_envelope(text="")
    adapter = MagicMock()
    adapter.self_user_id = "bot1"
    adapter.name = "discord"
    adapter.react = AsyncMock()
    adapter.send_typing = AsyncMock()

    with patch(
        "deilebot.foundation.pipeline.get_bot_settings",
        return_value=_bot_settings(echo_transcript=True, echo_max_chars=200),
    ):
        await pipeline.handle(env, adapter)

    kwargs = egress.send_response.call_args[1]
    echo = kwargs.get("transcript_echo")
    assert echo is not None
    assert "…" in echo, "Long transcript should be truncated with ellipsis"


# ── AC-3: integration — real EgressPipeline, assert on adapter.send_message ──

async def test_ac3_real_egress_send_message_starts_with_prefix():
    """AC-3: handle() with real EgressPipeline, adapter.send_message text starts with prefix."""
    svc = MagicMock(spec=TranscriptionService)
    svc._settings = TranscriptionSettings(enabled=True)
    svc.transcribe = AsyncMock(return_value="transcrição do áudio")

    pipeline, adapter = _build_ingress_with_real_egress(transcription_service=svc)
    env = _audio_envelope(text="")

    with patch(
        "deilebot.foundation.pipeline.get_bot_settings",
        return_value=_bot_settings(echo_transcript=True, echo_max_chars=200),
    ):
        await pipeline.handle(env, adapter)

    adapter.send_message.assert_called()
    first_call_text = adapter.send_message.call_args_list[0][0][1]
    assert first_call_text.startswith("🎙️ Transcrevi seu áudio:"), (
        f"Expected reply to start with echo prefix, got: {first_call_text!r}"
    )
    assert "transcrição do áudio" in first_call_text


async def test_ac3_real_egress_echo_off_no_prefix_in_send_message():
    """AC-3 variant: echo_transcript=False → adapter.send_message text does NOT start with prefix."""
    svc = MagicMock(spec=TranscriptionService)
    svc._settings = TranscriptionSettings(enabled=True)
    svc.transcribe = AsyncMock(return_value="transcrição silenciosa")

    pipeline, adapter = _build_ingress_with_real_egress(transcription_service=svc)
    env = _audio_envelope(text="")

    with patch(
        "deilebot.foundation.pipeline.get_bot_settings",
        return_value=_bot_settings(echo_transcript=False),
    ):
        await pipeline.handle(env, adapter)

    adapter.send_message.assert_called()
    first_call_text = adapter.send_message.call_args_list[0][0][1]
    assert not first_call_text.startswith("🎙️"), (
        f"Expected NO echo prefix but got: {first_call_text!r}"
    )


# ── AC-4: no audio → never prefixed ──────────────────────────────────────────

async def test_ac4_text_only_message_no_prefix():
    """AC-4: text-only message → send_response called with transcript_echo=None."""
    pipeline, egress, _ = _build_ingress_with_mock_egress(transcription_service=None)
    env = _text_envelope(text="olá bot")
    adapter = MagicMock()
    adapter.self_user_id = "bot1"
    adapter.name = "discord"
    adapter.react = AsyncMock()
    adapter.send_typing = AsyncMock()

    with patch(
        "deilebot.foundation.pipeline.get_bot_settings",
        return_value=_bot_settings(echo_transcript=True),
    ):
        await pipeline.handle(env, adapter)

    egress.send_response.assert_called_once()
    kwargs = egress.send_response.call_args[1]
    assert kwargs.get("transcript_echo") is None, (
        "Text-only message must never receive echo prefix, even with echo_transcript=True"
    )


async def test_ac4_text_only_real_egress_no_prefix_in_send_message():
    """AC-4 real egress: text-only message → adapter.send_message text has no echo prefix."""
    pipeline, adapter = _build_ingress_with_real_egress(transcription_service=None)
    env = _text_envelope(text="apenas texto")

    with patch(
        "deilebot.foundation.pipeline.get_bot_settings",
        return_value=_bot_settings(echo_transcript=True),
    ):
        await pipeline.handle(env, adapter)

    adapter.send_message.assert_called()
    first_call_text = adapter.send_message.call_args_list[0][0][1]
    assert not first_call_text.startswith("🎙️"), (
        f"Text-only message should not have echo prefix: {first_call_text!r}"
    )
