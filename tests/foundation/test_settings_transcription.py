"""AC-7 — TranscriptionSettings: YAML load, env-only api_key, defaults.
AC-31 — language field + resolve_language().
"""
from __future__ import annotations

import os

import pytest

from deilebot.foundation.settings import (
    BotSettings,
    TranscriptionSettings,
    reset_bot_settings_cache,
    resolve_language,
)


def test_transcription_defaults():
    s = TranscriptionSettings()
    assert s.engine == "openai"
    assert s.enabled is False
    assert s.max_duration_seconds == 120
    assert s.max_minutes_per_month == 60


def test_transcription_from_dict():
    s = TranscriptionSettings(
        engine="openai",
        enabled=True,
        max_duration_seconds=60,
        max_minutes_per_month=30,
    )
    assert s.enabled is True
    assert s.max_duration_seconds == 60
    assert s.max_minutes_per_month == 30


def test_api_key_not_in_settings_model():
    """api_key must not be a field of TranscriptionSettings (never from YAML)."""
    fields = TranscriptionSettings.model_fields
    assert "api_key" not in fields


def test_api_key_only_from_env(monkeypatch):
    monkeypatch.setenv("DEILE_BOT_TRANSCRIPTION_API_KEY", "sk-test-123")
    key = os.environ.get("DEILE_BOT_TRANSCRIPTION_API_KEY")
    assert key == "sk-test-123"


def test_api_key_absent_from_env(monkeypatch):
    monkeypatch.delenv("DEILE_BOT_TRANSCRIPTION_API_KEY", raising=False)
    key = os.environ.get("DEILE_BOT_TRANSCRIPTION_API_KEY")
    assert key is None


def test_bot_settings_includes_transcription():
    bs = BotSettings()
    assert hasattr(bs, "transcription")
    assert isinstance(bs.transcription, TranscriptionSettings)
    assert bs.transcription.enabled is False


def test_enabled_false_by_default_preserves_url_only_behavior():
    """Default disabled means no STT attempted — existing attachment behavior unchanged."""
    s = TranscriptionSettings()
    assert s.enabled is False


# ── AC-31: language field ────────────────────────────────────────────────────

def test_language_default_is_auto():
    s = TranscriptionSettings()
    assert s.language == "auto"


def test_language_explicit_pt():
    s = TranscriptionSettings(language="pt")
    assert s.language == "pt"


def test_language_explicit_en():
    s = TranscriptionSettings(language="en")
    assert s.language == "en"


def test_language_inherit_guild():
    s = TranscriptionSettings(language="inherit_guild")
    assert s.language == "inherit_guild"


def test_language_invalid_raises():
    with pytest.raises(Exception):
        TranscriptionSettings(language="fr")


# ── AC-31: resolve_language ──────────────────────────────────────────────────

def test_resolve_language_pt_returns_pt():
    assert resolve_language("pt", None) == "pt"
    assert resolve_language("pt", "pt-BR") == "pt"


def test_resolve_language_en_returns_en():
    assert resolve_language("en", None) == "en"
    assert resolve_language("en", "en-US") == "en"


def test_resolve_language_auto_returns_none():
    assert resolve_language("auto", None) is None
    assert resolve_language("auto", "pt-BR") is None


def test_resolve_language_inherit_guild_pt_br():
    assert resolve_language("inherit_guild", "pt-BR") == "pt"


def test_resolve_language_inherit_guild_en_us():
    assert resolve_language("inherit_guild", "en-US") == "en"


def test_resolve_language_inherit_guild_no_locale():
    assert resolve_language("inherit_guild", None) is None
    assert resolve_language("inherit_guild", "") is None


def test_resolve_language_inherit_guild_unsupported_locale():
    """Unsupported BCP-47 primary subtag → None (Whisper auto)."""
    assert resolve_language("inherit_guild", "es-ES") is None
    assert resolve_language("inherit_guild", "fr-FR") is None
    assert resolve_language("inherit_guild", "ja") is None


def test_resolve_language_unknown_setting_returns_none():
    assert resolve_language("unknown_value", "pt-BR") is None
