"""Tests for BotSettings."""

from __future__ import annotations

import pytest

from deilebot.foundation.settings import (FoundationSettings,
                                           get_bot_settings,
                                           reset_bot_settings_cache)


@pytest.fixture(autouse=True)
def _reset_cache_and_env(monkeypatch):
    reset_bot_settings_cache()
    keys_to_clear = [
        "DEILE_BOT_INTENT_CLASSIFIER",
        "DEILE_BOT_DATA_RETENTION_DAYS",
        "DEILE_BOT_AGENT_BRIDGE_MODE",
    ]
    for k in keys_to_clear:
        monkeypatch.delenv(k, raising=False)
    yield
    reset_bot_settings_cache()


class TestFoundationSettings:
    def test_defaults(self):
        s = FoundationSettings()
        assert s.intent_classifier == "heuristic"
        assert s.rate_limit_user_burst == 5
        assert s.agent_bridge_mode == "in_process"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("DEILE_BOT_INTENT_CLASSIFIER", "llm")
        s = FoundationSettings()
        assert s.intent_classifier == "llm"

    def test_invalid_classifier_rejected(self, monkeypatch):
        monkeypatch.setenv("DEILE_BOT_INTENT_CLASSIFIER", "magic")
        with pytest.raises(Exception):
            FoundationSettings()

    def test_int_coerced_from_env(self, monkeypatch):
        monkeypatch.setenv("DEILE_BOT_DATA_RETENTION_DAYS", "30")
        s = FoundationSettings()
        assert s.data_retention_days == 30


class TestGetBotSettings:
    def test_singleton_behavior(self):
        a = get_bot_settings()
        b = get_bot_settings()
        assert a is b

    def test_reset_cache(self, monkeypatch):
        a = get_bot_settings()
        reset_bot_settings_cache()
        monkeypatch.setenv("DEILE_BOT_INTENT_CLASSIFIER", "always_respond")
        b = get_bot_settings()
        assert b.foundation.intent_classifier == "always_respond"
        assert a is not b


class TestForgeSettings:
    """AC-5: ForgeSettings carrega blocos github+gitlab; BotSettings.github NÃO existe."""

    def test_forge_settings_defaults(self):
        from deilebot.foundation.settings import ForgeSettings, ForgeProviderSettings

        fs = ForgeSettings()
        assert fs.github.host == "github.com"
        assert fs.github.oauth_scope == "repo"
        assert fs.github.timeout == 15.0
        assert fs.gitlab.host == "gitlab.com"
        assert "api" in fs.gitlab.oauth_scope

    def test_bot_settings_has_forge_not_github(self):
        """BotSettings.github não existe mais — quebra direta (AC-5)."""
        from deilebot.foundation.settings import BotSettings

        settings = BotSettings()
        assert hasattr(settings, "forge"), "BotSettings.forge não existe!"
        assert not hasattr(settings, "github"), (
            "BotSettings.github ainda existe — deveria ter sido removido (AC-5 quebra direta)"
        )

    def test_forge_provider_settings_timeout(self):
        from deilebot.foundation.settings import ForgeProviderSettings

        s = ForgeProviderSettings(host="gitlab.myorg.com", timeout=30.0)
        assert s.timeout == 30.0
        assert s.host == "gitlab.myorg.com"

    def test_gitlab_env_token_readable(self, monkeypatch):
        """GITLAB_TOKEN / GL_TOKEN lidos do env (via _build_settings)."""
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-env-test")
        reset_bot_settings_cache()
        # Não testamos o token em si (não está em settings), mas verificamos
        # que o _build_settings não quebra com a env var presente.
        from deilebot.foundation.settings import get_bot_settings
        s = get_bot_settings()
        assert s.forge.gitlab is not None


class TestNoProvidersImportInFoundation:
    """Sanity: foundation must not import deilebot.providers (F6)."""

    def test_no_provider_imports(self):
        import pathlib

        foundation_dir = pathlib.Path(__file__).parent.parent.parent / "deilebot" / "foundation"
        for py in foundation_dir.rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            assert "from deilebot.providers" not in text, f"{py} imports providers"
