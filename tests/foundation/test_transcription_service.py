"""AC-1 (download+transcript), AC-4 (duration limit), AC-6 (graceful failure classes)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from deilebot.foundation.envelope import Attachment, AttachmentKind
from deilebot.foundation.settings import TranscriptionSettings
from deilebot.foundation.transcription import TranscriptionError, TranscriptionService
from deilebot.foundation.transcription_budget import TranscriptionBudgetTracker

# Minimal valid OGG header bytes (not playable, but enough to avoid import errors)
_FAKE_AUDIO = b"\x4f\x67\x67\x53" + b"\x00" * 60  # OggS magic + padding


@pytest.fixture
async def audio_server():
    routes = web.RouteTableDef()

    @routes.get("/audio.ogg")
    async def _ok(_):
        return web.Response(body=_FAKE_AUDIO, content_type="audio/ogg")

    @routes.get("/missing.ogg")
    async def _404(_):
        return web.Response(status=404)

    @routes.get("/server_error.ogg")
    async def _500(_):
        return web.Response(status=500)

    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app, handle_signals=False, access_log=None)
    await runner.setup()
    from aiohttp.web_runner import TCPSite
    site = TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


@pytest.fixture
async def budget(tmp_path: Path) -> TranscriptionBudgetTracker:
    tracker = TranscriptionBudgetTracker(tmp_path / "budget.sqlite")
    yield tracker
    await tracker.close()


def _make_svc(budget, *, max_duration=120, max_minutes=60, enabled=True):
    settings = TranscriptionSettings(
        enabled=enabled,
        max_duration_seconds=max_duration,
        max_minutes_per_month=max_minutes,
    )
    return TranscriptionService(settings=settings, budget=budget)


# ── AC-1: download + transcript (STT mocked) ──────────────────────────────────

async def test_transcribe_returns_text(audio_server, budget):
    """AC-1: enabled + AUDIO att with URL → download + STT → entry has transcript."""
    svc = _make_svc(budget)
    att = Attachment(
        kind=AttachmentKind.AUDIO,
        url=f"{audio_server}/audio.ogg",
        mime="audio/ogg",
        filename="voice.ogg",
    )
    # Mock duration and Whisper call
    with patch.object(svc, "_estimate_duration_seconds", return_value=10.0):
        with patch.object(svc, "_call_whisper", new=AsyncMock(return_value="olá mundo")):
            result = await svc.transcribe(att)

    assert result == "olá mundo"


async def test_transcribe_no_url_raises(budget):
    svc = _make_svc(budget)
    att = Attachment(kind=AttachmentKind.AUDIO, url=None, mime="audio/ogg")
    with pytest.raises(TranscriptionError, match="no URL"):
        await svc.transcribe(att)


# ── AC-4: duration enforcement (119s passes, 121s rejects) ───────────────────

async def test_duration_119s_passes(audio_server, budget):
    """119s is under the 120s limit — API must be called."""
    svc = _make_svc(budget, max_duration=120)
    att = Attachment(kind=AttachmentKind.AUDIO, url=f"{audio_server}/audio.ogg", mime="audio/ogg")
    api_called = []

    async def mock_whisper(*_a, **_kw):
        api_called.append(True)
        return "transcript"

    with patch.object(svc, "_estimate_duration_seconds", return_value=119.0):
        with patch.object(svc, "_call_whisper", new=AsyncMock(side_effect=mock_whisper)):
            result = await svc.transcribe(att)

    assert result == "transcript"
    assert api_called, "API was not called for 119s audio"


async def test_duration_121s_rejected_before_api(audio_server, budget):
    """121s is over the 120s limit — API must NOT be called."""
    svc = _make_svc(budget, max_duration=120)
    att = Attachment(kind=AttachmentKind.AUDIO, url=f"{audio_server}/audio.ogg", mime="audio/ogg")
    api_called = []

    async def mock_whisper(*_a, **_kw):
        api_called.append(True)
        return "transcript"

    with patch.object(svc, "_estimate_duration_seconds", return_value=121.0):
        with patch.object(svc, "_call_whisper", new=AsyncMock(side_effect=mock_whisper)):
            with pytest.raises(TranscriptionError, match="muito longo"):
                await svc.transcribe(att)

    assert not api_called, "API was called despite duration exceeding max"


# ── AC-6: graceful failure — each class of error raises TranscriptionError ───

async def test_download_http_404_raises(audio_server, budget):
    svc = _make_svc(budget)
    att = Attachment(kind=AttachmentKind.AUDIO, url=f"{audio_server}/missing.ogg", mime="audio/ogg")
    with pytest.raises(TranscriptionError, match="HTTP 404"):
        await svc.transcribe(att)


async def test_download_http_500_raises(audio_server, budget):
    svc = _make_svc(budget)
    att = Attachment(kind=AttachmentKind.AUDIO, url=f"{audio_server}/server_error.ogg", mime="audio/ogg")
    with pytest.raises(TranscriptionError, match="HTTP 500"):
        await svc.transcribe(att)


async def test_download_timeout_raises(budget, monkeypatch):
    svc = _make_svc(budget)
    att = Attachment(kind=AttachmentKind.AUDIO, url="http://127.0.0.1:19999/audio.ogg", mime="audio/ogg")

    import httpx

    async def _timeout(*_a, **_kw):
        raise httpx.TimeoutException("timeout", request=None)

    with patch.object(svc, "_download_audio", new=AsyncMock(side_effect=TranscriptionError("download timeout"))):
        with pytest.raises(TranscriptionError, match="timeout"):
            await svc.transcribe(att)


async def test_whisper_api_error_raises(audio_server, budget):
    svc = _make_svc(budget)
    att = Attachment(kind=AttachmentKind.AUDIO, url=f"{audio_server}/audio.ogg", mime="audio/ogg")

    async def _api_fail(*_a, **_kw):
        raise RuntimeError("API 429 rate limited")

    with patch.object(svc, "_estimate_duration_seconds", return_value=10.0):
        with patch.object(svc, "_call_whisper", new=AsyncMock(side_effect=_api_fail)):
            with pytest.raises(TranscriptionError, match="Whisper API error"):
                await svc.transcribe(att)


async def test_missing_api_key_raises(audio_server, budget, monkeypatch):
    monkeypatch.delenv("DEILE_BOT_TRANSCRIPTION_API_KEY", raising=False)
    svc = _make_svc(budget)
    att = Attachment(kind=AttachmentKind.AUDIO, url=f"{audio_server}/audio.ogg", mime="audio/ogg")

    with patch.object(svc, "_estimate_duration_seconds", return_value=10.0):
        # _call_whisper calls _get_api_key internally — don't mock _call_whisper
        with pytest.raises(TranscriptionError, match="API_KEY"):
            await svc.transcribe(att)


# ── AC-31: language parameter forwarded to _call_whisper ─────────────────────

async def test_transcribe_passes_language_to_whisper(audio_server, budget):
    """AC-31: language kwarg forwarded to _call_whisper when not None."""
    svc = _make_svc(budget)
    att = Attachment(
        kind=AttachmentKind.AUDIO,
        url=f"{audio_server}/audio.ogg",
        mime="audio/ogg",
        filename="voice.ogg",
    )
    captured_kwargs: list[dict] = []

    async def _mock_whisper(audio_bytes, filename, mime, *, language=None):
        captured_kwargs.append({"language": language})
        return "olá mundo"

    with patch.object(svc, "_estimate_duration_seconds", return_value=10.0):
        with patch.object(svc, "_call_whisper", new=AsyncMock(side_effect=_mock_whisper)):
            result = await svc.transcribe(att, language="pt")

    assert result == "olá mundo"
    assert captured_kwargs[0]["language"] == "pt"


async def test_transcribe_language_none_not_forwarded(audio_server, budget):
    """AC-31: language=None → _call_whisper receives language=None (auto-detect)."""
    svc = _make_svc(budget)
    att = Attachment(
        kind=AttachmentKind.AUDIO,
        url=f"{audio_server}/audio.ogg",
        mime="audio/ogg",
        filename="voice.ogg",
    )
    captured_kwargs: list[dict] = []

    async def _mock_whisper(audio_bytes, filename, mime, *, language=None):
        captured_kwargs.append({"language": language})
        return "transcript"

    with patch.object(svc, "_estimate_duration_seconds", return_value=10.0):
        with patch.object(svc, "_call_whisper", new=AsyncMock(side_effect=_mock_whisper)):
            await svc.transcribe(att, language=None)

    assert captured_kwargs[0]["language"] is None
