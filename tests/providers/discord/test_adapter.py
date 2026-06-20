"""Smoke tests for DiscordAdapter (no live discord connection)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from deilebot.foundation.envelope import AttachmentKind
from deilebot.foundation.exceptions import ProviderError
from deilebot.providers.discord.adapter import DiscordAdapter
from deilebot.providers.discord.settings import (DISCORD_CAPABILITIES,
                                                  DiscordBotSettings)


class TestAdapter:
    def test_capabilities(self):
        adapter = DiscordAdapter(DiscordBotSettings(token=SecretStr("x")))
        assert adapter.name == "discord"
        assert adapter.capabilities == DISCORD_CAPABILITIES
        assert AttachmentKind.IMAGE in adapter.capabilities.supported_attachment_kinds
        assert adapter.capabilities.max_message_chars == 2000

    async def test_start_without_token_raises(self):
        adapter = DiscordAdapter(DiscordBotSettings(token=SecretStr("")))
        with pytest.raises(ProviderError):
            await adapter.start()

    async def test_send_without_start_raises(self):
        from deilebot._testing import make_channel

        adapter = DiscordAdapter(DiscordBotSettings(token=SecretStr("x")))
        with pytest.raises(ProviderError):
            await adapter.send_message(
                make_channel(provider="discord"), "hi"
            )


class TestAdapterCogWiring:
    """AC-9: grupo /git registrado; cogs legados ausentes do adapter."""

    def test_git_cog_wired_in_adapter(self):
        """DiscordAdapter.start registra GitCog via add_cog."""
        import inspect
        from deilebot.providers.discord.adapter import DiscordAdapter

        src = inspect.getsource(DiscordAdapter.start)
        assert "GitCog" in src, "GitCog não está registrado em DiscordAdapter.start"
        assert "add_cog(GitCog(" in src, "GitCog não é adicionado via add_cog"

    def test_only_current_cogs_wired(self):
        """Apenas cogs atuais estão registrados; nenhum cog legado removido em V1."""
        import inspect
        from deilebot.providers.discord.adapter import DiscordAdapter

        src = inspect.getsource(DiscordAdapter.start)
        legacy_cog_names = ["Github" + "AuthCog", "Idea" + "Cog"]
        for legacy in legacy_cog_names:
            assert legacy not in src, f"{legacy} ainda está em DiscordAdapter.start"

    def test_git_commands_absent_from_old_modules(self):
        """AC-14: /github_login|status|logout não existem como comandos declarados."""
        from deilebot.providers.discord.cogs.git_cog import GitCog

        cmd_names = set()
        for attr_name in dir(GitCog):
            attr = getattr(GitCog, attr_name)
            if hasattr(attr, "name") and not attr_name.startswith("_"):
                cmd_names.add(getattr(attr, "name", None))

        assert "github_login" not in cmd_names, "/github_login ainda existe como comando!"
        assert "github_status" not in cmd_names, "/github_status ainda existe como comando!"
        assert "github_logout" not in cmd_names, "/github_logout ainda existe como comando!"
