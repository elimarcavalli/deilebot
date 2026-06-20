"""
AC-1 (download→transcript, Telegram), AC-2 (fiação handle→inbound_text, Telegram),
AC-3 (serviço compartilhado parametrizado Discord+Telegram),
AC-4 (falhas de download → skip_reason, sem crash),
AC-5 (não-regressão Discord).
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

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
from deilebot.foundation.pipeline import IngressPipeline
from deilebot.foundation.settings import (
    BotSettings,
    FoundationSettings,
    TranscriptionSettings,
    reset_bot_settings_cache,
)
from deilebot.foundation.transcription import TranscriptionError, TranscriptionService
from deilebot.foundation.transcription_budget import TranscriptionBudgetTracker

_FIXED_TS = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

_DISCORD_BOT_USER = BotUser(
    bot_user_id="u-discord",
    provider="discord",
    provider_user_id="111",
    display_name="DiscordUser",
)
_TELEGRAM_BOT_USER = BotUser(
    bot_user_id="u-telegram",
    provider="telegram",
    provider_user_id="222",
    display_name="TelegramUser",
)
_DISCORD_CHANNEL = Channel(
    provider="discord",
    provider_channel_id="ch-discord",
    name="test-discord",
    scope=ChannelScope.DM,
)
_TELEGRAM_CHANNEL = Channel(
    provider="telegram",
    provider_channel_id="ch-telegram",
    name="test-telegram",
    scope=ChannelScope.DM,
)


def _audio_envelope(provider: str, url: str = "http://cdn.example.com/audio.ogg") -> MessageEnvelope:
    if provider == "discord":
        channel = _DISCORD_CHANNEL
        author = _DISCORD_BOT_USER
    else:
        channel = _TELEGRAM_CHANNEL
        author = _TELEGRAM_BOT_USER
    return MessageEnvelope(
        message_id=f"msg-{provider}",
        channel=channel,
        author=author,
        sent_at=_FIXED_TS,
        text="",
        attachments=(
            Attachment(
                kind=AttachmentKind.AUDIO,
                url=url,
                mime="audio/ogg",
                filename="voice.ogg",
            ),
        ),
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


def _build_pipeline(*, transcription_service=None):
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
    bridge.invocations = invocations

    identity = MagicMock()
    identity.resolve = AsyncMock(side_effect=lambda prov, uid, name, *, is_bot=False: _make_user(prov, uid, name, is_bot))

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


def _make_user(provider, uid, name, is_bot):
    return BotUser(
        bot_user_id=f"{provider}-{uid}",
        provider=provider,
        provider_user_id=str(uid),
        display_name=name,
        is_bot=bool(is_bot),
    )


def _mock_bot_settings(*, enabled: bool = True):
    from unittest.mock import patch
    fs = FoundationSettings(
        forced_model=None,
        default_model=None,
        agent_invocation_timeout_seconds=30,
    )
    ts = TranscriptionSettings(enabled=enabled, max_duration_seconds=120, max_minutes_per_month=60)
    return BotSettings(foundation=fs, transcription=ts)


# ── AC-1: _materialize_attachments with Telegram audio URL → transcript ───────

async def test_ac1_telegram_audio_url_produces_transcript():
    """AC-1: _materialize_attachments with Telegram AUDIO att (url set) → entry has transcript."""
    from deilebot.foundation.pipeline import IngressPipeline as Pipe

    pipe = Pipe.__new__(Pipe)
    pipe._logger = MagicMock()

    svc = MagicMock(spec=TranscriptionService)
    svc._settings = TranscriptionSettings(enabled=True)
    svc.transcribe = AsyncMock(return_value="olá mundo")

    att = Attachment(
        kind=AttachmentKind.AUDIO,
        url="https://api.telegram.org/file/botTOKEN/voice/file_1.oga",
        mime="audio/ogg",
        filename=None,
    )
    env = MessageEnvelope(
        message_id="tg-1",
        channel=_TELEGRAM_CHANNEL,
        author=_TELEGRAM_BOT_USER,
        sent_at=_FIXED_TS,
        text="",
        attachments=(att,),
    )

    results = await pipe._materialize_attachments((att,), transcription_service=svc, env=env)

    assert len(results) == 1
    entry = results[0]
    assert entry.get("transcript") == "olá mundo", (
        f"expected transcript in entry, got: {entry}"
    )
    assert entry["kind"] == "AUDIO"


# ── AC-2: full handle() path for Telegram → inbound_text contains transcript ──

async def test_ac2_telegram_handle_wires_transcript_to_inbound_text():
    """AC-2: handle() with Telegram audio env → AgentInvocation.inbound_text contains transcript."""
    from unittest.mock import patch

    svc = MagicMock(spec=TranscriptionService)
    svc._settings = TranscriptionSettings(enabled=True)
    svc.transcribe = AsyncMock(return_value="olá mundo")

    pipeline, bridge = _build_pipeline(transcription_service=svc)
    env = _audio_envelope("telegram")
    adapter = MagicMock()
    adapter.self_user_id = "bot1"
    adapter.name = "telegram"

    with patch(
        "deilebot.foundation.pipeline.get_bot_settings",
        return_value=_mock_bot_settings(enabled=True),
    ):
        await pipeline.handle(env, adapter)

    assert bridge.invocations, "bridge.invoke was not called"
    inv = bridge.invocations[0]
    assert "olá mundo" in inv.inbound_text, (
        f"transcript not in inbound_text: {inv.inbound_text!r}"
    )


# ── AC-3: same TranscriptionService handles Discord and Telegram ──────────────

@pytest.mark.parametrize("provider", ["discord", "telegram"])
async def test_ac3_shared_transcription_service(provider):
    """AC-3: same TranscriptionService instance handles both Discord and Telegram."""
    from unittest.mock import patch

    svc = MagicMock(spec=TranscriptionService)
    svc._settings = TranscriptionSettings(enabled=True)
    svc.transcribe = AsyncMock(return_value="shared transcript")

    pipeline, bridge = _build_pipeline(transcription_service=svc)
    env = _audio_envelope(provider)
    adapter = MagicMock()
    adapter.self_user_id = "bot1"
    adapter.name = provider

    with patch(
        "deilebot.foundation.pipeline.get_bot_settings",
        return_value=_mock_bot_settings(enabled=True),
    ):
        await pipeline.handle(env, adapter)

    assert svc.transcribe.called, f"transcribe not called for {provider}"
    inv = bridge.invocations[0]
    assert "shared transcript" in inv.inbound_text


# ── AC-4: download failures → skip_reason set, pipeline does not crash ─────────

@pytest.mark.parametrize("error_label,exc", [
    ("timeout", TranscriptionError("download timeout: timed out")),
    ("url_expired_4xx", TranscriptionError("download failed: HTTP 401")),
    ("not_found_404", TranscriptionError("download failed: HTTP 404")),
])
async def test_ac4_download_failures_produce_skip_reason(error_label, exc):
    """AC-4: STT/download failure → skip_reason in entry, pipeline handles without crash."""
    from deilebot.foundation.pipeline import IngressPipeline as Pipe

    pipe = Pipe.__new__(Pipe)
    pipe._logger = MagicMock()

    svc = MagicMock(spec=TranscriptionService)
    svc._settings = TranscriptionSettings(enabled=True)
    svc.transcribe = AsyncMock(side_effect=exc)

    att = Attachment(
        kind=AttachmentKind.AUDIO,
        url="https://api.telegram.org/file/botTOKEN/voice/file_x.oga",
        mime="audio/ogg",
        filename=None,
    )
    env = MessageEnvelope(
        message_id="tg-fail",
        channel=_TELEGRAM_CHANNEL,
        author=_TELEGRAM_BOT_USER,
        sent_at=_FIXED_TS,
        text="fallback text",
        attachments=(att,),
    )

    results = await pipe._materialize_attachments((att,), transcription_service=svc, env=env)
    entry = results[0]

    assert "transcript" not in entry, f"transcript must be absent on {error_label}"
    assert "skip_reason" in entry, f"skip_reason must be set on {error_label}"


async def test_ac4_handle_does_not_propagate_stt_error():
    """AC-4: STT failure in handle() → pipeline completes, inbound_text falls back to env.text."""
    from unittest.mock import patch

    svc = MagicMock(spec=TranscriptionService)
    svc._settings = TranscriptionSettings(enabled=True)
    svc.transcribe = AsyncMock(side_effect=TranscriptionError("download timeout"))

    pipeline, bridge = _build_pipeline(transcription_service=svc)
    env = _audio_envelope("telegram")
    adapter = MagicMock()
    adapter.self_user_id = "bot1"
    adapter.name = "telegram"

    with patch(
        "deilebot.foundation.pipeline.get_bot_settings",
        return_value=_mock_bot_settings(enabled=True),
    ):
        await pipeline.handle(env, adapter)

    assert bridge.invocations, "pipeline must complete and call bridge"
    inv = bridge.invocations[0]
    assert inv.inbound_text == env.text or inv.inbound_text == ""


# ── AC-5: non-regression — Discord path unchanged ─────────────────────────────

async def test_ac5_discord_transcript_still_works():
    """AC-5: Discord audio path from #23 still works after Telegram wiring added."""
    from unittest.mock import patch

    svc = MagicMock(spec=TranscriptionService)
    svc._settings = TranscriptionSettings(enabled=True)
    svc.transcribe = AsyncMock(return_value="discord transcript")

    pipeline, bridge = _build_pipeline(transcription_service=svc)
    env = _audio_envelope("discord")
    adapter = MagicMock()
    adapter.self_user_id = "bot1"
    adapter.name = "discord"

    with patch(
        "deilebot.foundation.pipeline.get_bot_settings",
        return_value=_mock_bot_settings(enabled=True),
    ):
        await pipeline.handle(env, adapter)

    assert bridge.invocations, "bridge must be called for Discord"
    inv = bridge.invocations[0]
    assert "discord transcript" in inv.inbound_text


async def test_ac5_telegram_does_not_break_discord_no_transcription():
    """AC-5: Without transcription_service, Discord audio stays URL-only (no regression)."""
    from deilebot.foundation.pipeline import IngressPipeline as Pipe

    pipe = Pipe.__new__(Pipe)
    atts = (Attachment(kind=AttachmentKind.AUDIO, url="http://cdn.discord.com/v.ogg", mime="audio/ogg"),)
    out = await pipe._materialize_attachments(atts)
    assert out[0]["kind"] == "AUDIO"
    assert "transcript" not in out[0]
