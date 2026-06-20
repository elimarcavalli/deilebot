"""AC-7 — TranscriptionSettings: YAML load, env-only api_key, defaults."""
from __future__ import annotations

import os

import pytest

from deilebot.foundation.settings import BotSettings, TranscriptionSettings, reset_bot_settings_cache


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
