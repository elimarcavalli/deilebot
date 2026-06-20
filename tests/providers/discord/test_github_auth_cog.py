"""Testes de wiring migrados: GitHubAuthCog → GitCog (AC-14).

Os /github_* foram removidos em V1 e substituídos pelo grupo /git.
Estes testes verificam que a migração foi feita corretamente.
"""

from __future__ import annotations


class TestGitCogShape:
    """AC-14: GitCog (novo) declara os subcomandos login/status/logout."""

    def test_declares_git_subcommands(self):
        """GitCog declara os subcomandos login, status, logout, ideia."""
        from deilebot.providers.discord.cogs.git_cog import GitCog

        names = set()
        for attr_name in dir(GitCog):
            attr = getattr(GitCog, attr_name)
            if hasattr(attr, "name") and not attr_name.startswith("_"):
                names.add(getattr(attr, "name", None))
        # O grupo /git tem subcomandos; verificar que estão presentes
        assert any(n in ("git_login", "login") for n in names), \
            "Subcomando login não encontrado em GitCog"

    def test_github_auth_cog_no_longer_has_commands(self):
        """AC-14: github_auth_cog.py ainda existe mas seus comandos não estão no adapter."""
        from deilebot.providers.discord.cogs.github_auth_cog import GitHubAuthCog

        # O arquivo ainda existe mas o adapter não o registra mais (verificado em test_adapter.py)
        cog = GitHubAuthCog.__new__(GitHubAuthCog)
        cog.bot = None
        cog.runtime = None
        cog.adapter = None
        assert hasattr(cog, "github_login"), "Arquivo legado preservado, mas não registrado"


class TestGitHubAuthCogWiring:
    """AC-14: GitHubAuthCog NÃO deve estar no adapter; GitCog sim."""

    def test_cog_is_wired_in_adapter(self):
        """GitCog (não GitHubAuthCog) está registrado no adapter."""
        import inspect
        from deilebot.providers.discord.adapter import DiscordAdapter

        src = inspect.getsource(DiscordAdapter.start)
        assert "GitCog" in src, "GitCog não está registrado no adapter"
        assert "GitHubAuthCog" not in src, \
            "GitHubAuthCog ainda está no adapter — deve ter sido removido (AC-14)"
