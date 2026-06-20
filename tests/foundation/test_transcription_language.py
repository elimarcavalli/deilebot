"""Tests for AC-1, AC-2, AC-4, AC-5, AC-6 of issue #31: language field in TranscriptionSettings
and resolve_language() pure function.
"""
from __future__ import annotations

import pytest

from deilebot.foundation.settings import TranscriptionSettings, resolve_language
from deilebot.foundation.transcription import TranscriptionService


# ── AC-1: language="auto" → TranscriptionService calls Whisper without language kwarg ──

async def test_language_auto_calls_whisper_without_language_kwarg(tmp_path):
    """AC-1: language='auto' → _call_whisper receives language=None (no kwarg)."""
    from pathlib import Path
    from unittest.mock import AsyncMock, patch

    from deilebot.foundation.envelope import Attachment, AttachmentKind
    from deilebot.foundation.transcription_budget import TranscriptionBudgetTracker

    budget = TranscriptionBudgetTracker(tmp_path / "budget.sqlite")
    settings = TranscriptionSettings(enabled=True, language="auto")
    svc = TranscriptionService(settings=settings, budget=budget)

    att = Attachment(
        kind=AttachmentKind.AUDIO,
        url="http://example.com/audio.ogg",
        mime="audio/ogg",
        filename="voice.ogg",
    )
    captured: list = []

    async def _mock_whisper(audio_bytes, filename, mime, *, language=None):
        captured.append(language)
        return "transcript"

    with patch.object(svc, "_estimate_duration_seconds", return_value=10.0):
        with patch.object(svc, "_call_whisper", new=AsyncMock(side_effect=_mock_whisper)):
            with patch.object(svc, "_download_audio", new=AsyncMock(return_value=b"audio")):
                await svc.transcribe(att, language=None)

    assert captured == [None], f"Expected language=None for 'auto', got {captured}"
    await budget.close()


# ── AC-2: explicit language="pt" and language="en" forwarded to Whisper ──────────────────

async def test_language_pt_forwarded_to_whisper(tmp_path):
    """AC-2: language='pt' → _call_whisper receives language='pt'."""
    from unittest.mock import AsyncMock, patch

    from deilebot.foundation.envelope import Attachment, AttachmentKind
    from deilebot.foundation.transcription_budget import TranscriptionBudgetTracker

    budget = TranscriptionBudgetTracker(tmp_path / "budget_pt.sqlite")
    settings = TranscriptionSettings(enabled=True, language="pt")
    svc = TranscriptionService(settings=settings, budget=budget)

    att = Attachment(
        kind=AttachmentKind.AUDIO,
        url="http://example.com/audio.ogg",
        mime="audio/ogg",
        filename="voice.ogg",
    )
    captured: list = []

    async def _mock_whisper(audio_bytes, filename, mime, *, language=None):
        captured.append(language)
        return "olá"

    with patch.object(svc, "_estimate_duration_seconds", return_value=10.0):
        with patch.object(svc, "_call_whisper", new=AsyncMock(side_effect=_mock_whisper)):
            with patch.object(svc, "_download_audio", new=AsyncMock(return_value=b"audio")):
                await svc.transcribe(att, language="pt")

    assert captured == ["pt"], f"Expected language='pt', got {captured}"
    await budget.close()


async def test_language_en_forwarded_to_whisper(tmp_path):
    """AC-2: language='en' → _call_whisper receives language='en'."""
    from unittest.mock import AsyncMock, patch

    from deilebot.foundation.envelope import Attachment, AttachmentKind
    from deilebot.foundation.transcription_budget import TranscriptionBudgetTracker

    budget = TranscriptionBudgetTracker(tmp_path / "budget_en.sqlite")
    settings = TranscriptionSettings(enabled=True, language="en")
    svc = TranscriptionService(settings=settings, budget=budget)

    att = Attachment(
        kind=AttachmentKind.AUDIO,
        url="http://example.com/audio.ogg",
        mime="audio/ogg",
        filename="voice.ogg",
    )
    captured: list = []

    async def _mock_whisper(audio_bytes, filename, mime, *, language=None):
        captured.append(language)
        return "hello"

    with patch.object(svc, "_estimate_duration_seconds", return_value=10.0):
        with patch.object(svc, "_call_whisper", new=AsyncMock(side_effect=_mock_whisper)):
            with patch.object(svc, "_download_audio", new=AsyncMock(return_value=b"audio")):
                await svc.transcribe(att, language="en")

    assert captured == ["en"], f"Expected language='en', got {captured}"
    await budget.close()


# ── AC-4: invalid language values raise pydantic.ValidationError on construction ─────────

def test_invalid_language_xx_raises_validation_error():
    """AC-4: language='xx' is not in Literal, must raise pydantic.ValidationError."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TranscriptionSettings(language="xx")


def test_invalid_language_portuguese_raises_validation_error():
    """AC-4: language='portuguese' is not in Literal, must raise pydantic.ValidationError."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TranscriptionSettings(language="portuguese")


# ── AC-5: resolve_language parametrized table ────────────────────────────────────────────

@pytest.mark.parametrize(
    "setting,guild_locale,expected",
    [
        # inherit_guild + supported BCP-47 → ISO-639-1
        ("inherit_guild", "pt-BR", "pt"),
        ("inherit_guild", "pt-PT", "pt"),
        ("inherit_guild", "en-US", "en"),
        ("inherit_guild", "en-GB", "en"),
        # inherit_guild + unsupported → None
        ("inherit_guild", "de-DE", None),
        ("inherit_guild", "es-ES", None),
        ("inherit_guild", "fr", None),
        # inherit_guild + absent/empty → None
        ("inherit_guild", "", None),
        ("inherit_guild", None, None),
        # auto → always None regardless of locale
        ("auto", "pt-BR", None),
        # explicit settings ignore locale
        ("pt", None, "pt"),
        ("en", "de-DE", "en"),
    ],
)
def test_resolve_language(setting, guild_locale, expected):
    """AC-5: resolve_language parametrized table."""
    result = resolve_language(setting, guild_locale)
    assert result == expected, (
        f"resolve_language({setting!r}, {guild_locale!r}) = {result!r}, expected {expected!r}"
    )


# ── AC-6: default language="auto" does not break existing behavior ───────────────────────

def test_default_language_is_auto():
    """AC-6: TranscriptionSettings() default language='auto'."""
    s = TranscriptionSettings()
    assert s.language == "auto"


def test_default_language_auto_resolve_returns_none():
    """AC-6: default 'auto' + no locale → resolve_language returns None (Whisper auto-detects)."""
    s = TranscriptionSettings()
    result = resolve_language(s.language, None)
    assert result is None


def test_default_language_auto_with_any_locale_still_none():
    """AC-6: 'auto' + any locale → None; no breakage for existing guild messages."""
    s = TranscriptionSettings()
    for locale in ("pt-BR", "en-US", "de-DE", "ja-JP"):
        result = resolve_language(s.language, locale)
        assert result is None, f"Expected None for auto+{locale!r}, got {result!r}"
