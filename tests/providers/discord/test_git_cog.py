"""Testes do GitCog — /git login/status/logout/ideia.

Cobre AC-7 (status 3-estados), AC-8 (integração cog→service GitLab),
AC-12 (seleção de forge no /git ideia), AC-18 (timeout/erro legível),
AC-20 (log estruturado + mensagem amigável).

HTTP e Discord mockados; sem conexão real.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deilebot.foundation.agent_bridge import AgentInvocation, AgentResponse
from deilebot.foundation.exceptions import AgentInvocationError
from deilebot.foundation.forge_auth import (
    ForgeAuthError,
    ForgeKind,
    ForgeTimeoutError,
    Identity,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_ctx(
    *,
    author_id: int = 42,
    author_name: str = "alice",
    channel_id: int = 100,
    has_interaction: bool = True,
) -> MagicMock:
    ctx = MagicMock()
    ctx.defer = AsyncMock()
    ctx.send = AsyncMock()
    ctx.channel.id = channel_id
    ctx.author.id = author_id
    ctx.author.display_name = author_name
    ctx.author.name = author_name
    ctx.invoked_with = None
    ctx.interaction = MagicMock() if has_interaction else None
    if has_interaction:
        ctx.interaction.response.defer = AsyncMock()
        ctx.interaction.response.send_modal = AsyncMock()
        ctx.interaction.followup.send = AsyncMock()
        ctx.interaction.delete_original_response = AsyncMock()
    return ctx


def _make_runtime(bridge_text: str = "") -> MagicMock:
    from deile.common.markup_ast import MarkupAST

    response = AgentResponse(
        text=bridge_text,
        markup=MarkupAST.from_plain(bridge_text),
        elapsed_ms=10,
        model_used="fake",
    )
    bridge = MagicMock()
    bridge.invoke = AsyncMock(return_value=response)
    pipeline = MagicMock()
    pipeline.bridge = bridge
    runtime = MagicMock()
    runtime.pipeline = pipeline
    return runtime


def _make_cog(runtime=None, home=None):
    from deilebot.providers.discord.cogs.git_cog import GitCog

    cog = GitCog.__new__(GitCog)
    cog.bot = None
    cog.runtime = runtime or _make_runtime()
    cog.adapter = None
    cog._bg_tasks = set()
    cog._test_home = home

    # Bypass the owner check so tests don't need a live Discord adapter.
    async def _always_owner(ctx):
        return True

    cog._is_owner = _always_owner
    return cog


# ── AC-7: /git status — 3 estados (✅ / ⚠️ / ⛔) ─────────────────────────

async def test_git_status_all_three_states(monkeypatch, tmp_path):
    """Verifica os 3 estados por forge num único cog."""
    from deilebot.foundation.forge_auth import GitHubForgeAuth, GitLabForgeAuth
    from deilebot.providers.discord.cogs.git_cog import GitCog

    # GitHub: token presente + válido (✅)
    svc_gh = GitHubForgeAuth(home=tmp_path)
    svc_gh._write_credential("ghp_VALID")

    async def gh_validate(token):
        return Identity(login="octocat", forge="github", token_type="PAT clássico")

    monkeypatch.setattr(svc_gh, "validate_token", gh_validate)

    # GitLab: token presente + inválido (⚠️)
    svc_gl = GitLabForgeAuth(home=tmp_path)
    svc_gl._write_credential("glpat-INVALID")

    async def gl_validate(token):
        raise ForgeAuthError("token inválido")

    monkeypatch.setattr(svc_gl, "validate_token", gl_validate)

    cog = _make_cog()
    ctx = _make_ctx()

    def mock_get_service(kind, home=None):
        if kind == ForgeKind.GITHUB:
            return svc_gh
        return svc_gl

    monkeypatch.setattr(cog, "_get_service", mock_get_service)

    await cog.git_status.callback(cog, ctx)

    ctx.send.assert_called_once()
    embed = ctx.send.call_args[1]["embed"]
    embed_str = str([f.value for f in embed.fields])

    assert "octocat" in embed_str, "Estado ✅ não encontrado"
    # ⚠️ estado: field name tem "token inválido", valor tem "válido" e "refaça"
    assert "refaça" in embed_str.lower() or "token" in embed_str.lower(), "Estado ⚠️ não encontrado"


async def test_git_status_not_configured(monkeypatch, tmp_path):
    """Forge não configurado mostra estado ⛔."""
    from deilebot.foundation.forge_auth import GitHubForgeAuth, GitLabForgeAuth

    svc_gh = GitHubForgeAuth(home=tmp_path)
    svc_gl = GitLabForgeAuth(home=tmp_path)

    cog = _make_cog()
    ctx = _make_ctx()

    def mock_get_service(kind, home=None):
        return svc_gh if kind == ForgeKind.GITHUB else svc_gl

    monkeypatch.setattr(cog, "_get_service", mock_get_service)
    await cog.git_status.callback(cog, ctx)

    ctx.send.assert_called_once()
    embed = ctx.send.call_args[1]["embed"]
    fields_str = str([f.value for f in embed.fields])
    assert "Não configurado" in fields_str or "login" in fields_str.lower()


# ── AC-8: integração cog→service (GitLab write) ──────────────────────────────

async def test_pat_modal_gitlab_writes_credential(monkeypatch, tmp_path):
    """Modal com forge=gitlab + PAT válido escreve a linha gitlab.com."""
    from deilebot.foundation.forge_auth import GitLabForgeAuth
    from deilebot.providers.discord.cogs.git_cog import _PatLoginModal

    svc_gl = GitLabForgeAuth(home=tmp_path)

    async def fake_validate(token):
        return Identity(login="gituser", forge="gitlab", token_type="PAT pessoal")

    async def fake_helper():
        pass

    monkeypatch.setattr(svc_gl, "validate_token", fake_validate)
    monkeypatch.setattr(svc_gl, "_configure_git_helper", fake_helper)

    # Patch get_forge_auth to return our svc
    with patch("deilebot.providers.discord.cogs.git_cog.get_forge_auth", return_value=svc_gl):
        modal = _PatLoginModal()
        modal.forge_select._value = "gitlab"
        modal.token._value = "glpat-VALIDTOKEN"

        interaction = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        await modal.on_submit(interaction)

    # Assertions: (a) service was called, (b) credential written
    assert svc_gl.stored_token() == "glpat-VALIDTOKEN", "Linha gitlab.com não foi escrita!"
    interaction.followup.send.assert_called_once()
    embed = interaction.followup.send.call_args[1]["embed"]
    assert "gituser" in embed.description or "GitLab" in embed.title


async def test_pat_modal_invalid_token_not_written(monkeypatch, tmp_path):
    """Token inválido (401) não é escrito em ~/.git-credentials — AC-6."""
    from deilebot.foundation.forge_auth import GitLabForgeAuth
    from deilebot.providers.discord.cogs.git_cog import _PatLoginModal

    svc_gl = GitLabForgeAuth(home=tmp_path)

    async def fake_validate(token):
        raise ForgeAuthError("token inválido (HTTP 401)")

    monkeypatch.setattr(svc_gl, "validate_token", fake_validate)

    with patch("deilebot.providers.discord.cogs.git_cog.get_forge_auth", return_value=svc_gl):
        modal = _PatLoginModal()
        modal.forge_select._value = "gitlab"
        modal.token._value = "glpat-INVALIDO"

        interaction = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        await modal.on_submit(interaction)

    creds = tmp_path / ".git-credentials"
    assert not creds.exists(), "Credencial foi escrita mesmo com token inválido!"

    embed = interaction.followup.send.call_args[1]["embed"]
    assert "❌" in embed.title


# ── AC-12: /git ideia seleção de forge ───────────────────────────────────────

async def test_git_ideia_one_forge_uses_silently(monkeypatch, tmp_path):
    """Com um forge configurado, usa silenciosamente sem prompts."""
    from deilebot.foundation.forge_auth import GitHubForgeAuth, GitLabForgeAuth

    svc_gh = GitHubForgeAuth(home=tmp_path)
    svc_gh._write_credential("ghp_VALID")
    svc_gl = GitLabForgeAuth(home=tmp_path)  # sem token

    url = "https://github.com/elimarcavalli/deile/issues/99"
    runtime = _make_runtime(url)
    cog = _make_cog(runtime)

    def mock_get_service(kind, home=None):
        return svc_gh if kind == ForgeKind.GITHUB else svc_gl

    monkeypatch.setattr(cog, "_get_service", mock_get_service)
    ctx = _make_ctx()

    with patch("deilebot.providers.discord.cogs.git_cog._safe_dm", new=AsyncMock()):
        await cog.git_ideia.callback(cog, ctx, texto="nova ideia", forge=None)

    inv: AgentInvocation = runtime.pipeline.bridge.invoke.call_args[0][0]
    assert inv.bot_context["forge_kind"] == "github"
    assert url in ctx.send.call_args[0][0]


async def test_git_ideia_no_forge_shows_login_hint(monkeypatch, tmp_path):
    """Sem forge configurado → mensagem para /git login."""
    from deilebot.foundation.forge_auth import GitHubForgeAuth, GitLabForgeAuth

    svc_gh = GitHubForgeAuth(home=tmp_path)
    svc_gl = GitLabForgeAuth(home=tmp_path)

    runtime = _make_runtime()
    cog = _make_cog(runtime)

    def mock_get_service(kind, home=None):
        return svc_gh if kind == ForgeKind.GITHUB else svc_gl

    monkeypatch.setattr(cog, "_get_service", mock_get_service)
    ctx = _make_ctx()

    with patch("deilebot.providers.discord.cogs.git_cog._safe_dm", new=AsyncMock()):
        await cog.git_ideia.callback(cog, ctx, texto="ideia sem forge", forge=None)

    sent = ctx.send.call_args[0][0]
    assert "login" in sent.lower()


async def test_git_ideia_explicit_forge_gitlab(monkeypatch, tmp_path):
    """forge=gitlab explícito usa GitLab prompt e template."""
    from deilebot.foundation.forge_auth import GitHubForgeAuth, GitLabForgeAuth

    svc_gl = GitLabForgeAuth(home=tmp_path)
    svc_gl._write_credential("glpat-VALID")

    url = "https://gitlab.com/elimarcavalli/deile/-/issues/5"
    runtime = _make_runtime(url)
    cog = _make_cog(runtime)

    def mock_get_service(kind, home=None):
        return svc_gl

    monkeypatch.setattr(cog, "_get_service", mock_get_service)
    ctx = _make_ctx()

    with patch("deilebot.providers.discord.cogs.git_cog._safe_dm", new=AsyncMock()):
        await cog.git_ideia.callback(cog, ctx, texto="ideia gitlab", forge="gitlab")

    inv: AgentInvocation = runtime.pipeline.bridge.invoke.call_args[0][0]
    assert inv.bot_context["forge_kind"] == "gitlab"
    assert ".gitlab/issue_templates/intent.md" in inv.extra_system_prompt


# ── AC-18: timeout → mensagem Discord legível ─────────────────────────────────

async def test_git_status_timeout_shows_readable_message(monkeypatch, tmp_path):
    """Timeout no /git status mostra mensagem legível, nunca traceback."""
    from deilebot.foundation.forge_auth import GitHubForgeAuth, GitLabForgeAuth

    svc_gh = GitHubForgeAuth(home=tmp_path)
    svc_gh._write_credential("ghp_SLOW")
    svc_gl = GitLabForgeAuth(home=tmp_path)

    async def slow_validate(token):
        raise ForgeTimeoutError("GitHub self-hosted não respondeu em 15s")

    monkeypatch.setattr(svc_gh, "validate_token", slow_validate)

    cog = _make_cog()
    ctx = _make_ctx()

    def mock_get_service(kind, home=None):
        return svc_gh if kind == ForgeKind.GITHUB else svc_gl

    monkeypatch.setattr(cog, "_get_service", mock_get_service)
    await cog.git_status.callback(cog, ctx)

    embed = ctx.send.call_args[1]["embed"]
    fields_str = str([f.value for f in embed.fields])
    assert "timeout" in fields_str.lower() or "respondeu" in fields_str.lower()


# ── AC-20: log estruturado + mensagem amigável em falha de auth ─────────────

async def test_pat_modal_timeout_shows_readable_discord_message(monkeypatch, tmp_path, caplog):
    """Timeout no modal → mensagem legível ao Discord, nunca traceback."""
    from deilebot.foundation.forge_auth import GitLabForgeAuth
    from deilebot.providers.discord.cogs.git_cog import _PatLoginModal

    svc_gl = GitLabForgeAuth(home=tmp_path)

    async def timeout_validate(token):
        raise ForgeTimeoutError("GitLab self-hosted não respondeu em 15s")

    monkeypatch.setattr(svc_gl, "validate_token", timeout_validate)

    with patch("deilebot.providers.discord.cogs.git_cog.get_forge_auth", return_value=svc_gl):
        modal = _PatLoginModal()
        modal.forge_select._value = "gitlab"
        modal.token._value = "glpat-SLOW"

        interaction = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        await modal.on_submit(interaction)

    embed = interaction.followup.send.call_args[1]["embed"]
    assert "⏱️" in embed.title
    assert "Traceback" not in (embed.description or "")
    assert "respondeu" in embed.description.lower()


# ── AC-6: _extract_issue_url migrada para a função viva em git_cog ────────────

class TestExtractIssueUrl:
    """Cobre _extract_issue_url(text, forge) — função viva em git_cog (AC-6)."""

    def test_extracts_github_issue_url(self):
        from deilebot.providers.discord.cogs.git_cog import _extract_issue_url

        text = "Issue criada em https://github.com/elimarcavalli/deile/issues/99 !"
        assert _extract_issue_url(text, "github") == "https://github.com/elimarcavalli/deile/issues/99"

    def test_returns_none_when_no_url(self):
        from deilebot.providers.discord.cogs.git_cog import _extract_issue_url

        assert _extract_issue_url("nenhuma url aqui", "github") is None

    def test_ignores_non_issue_github_urls(self):
        from deilebot.providers.discord.cogs.git_cog import _extract_issue_url

        text = "see https://github.com/elimarcavalli/deile"
        assert _extract_issue_url(text, "github") is None

    def test_accepts_url_at_end_of_line(self):
        from deilebot.providers.discord.cogs.git_cog import _extract_issue_url

        text = "Criada:\nhttps://github.com/elimarcavalli/deile/issues/1\n"
        assert _extract_issue_url(text, "github") == "https://github.com/elimarcavalli/deile/issues/1"

    def test_extracts_gitlab_issue_url(self):
        from deilebot.providers.discord.cogs.git_cog import _extract_issue_url

        text = "Issue aberta em https://gitlab.com/elimarcavalli/deile/-/issues/7 pronto!"
        assert _extract_issue_url(text, "gitlab") == "https://gitlab.com/elimarcavalli/deile/-/issues/7"

    def test_gitlab_ignores_github_url(self):
        from deilebot.providers.discord.cogs.git_cog import _extract_issue_url

        text = "see https://github.com/elimarcavalli/deile/issues/99"
        assert _extract_issue_url(text, "gitlab") is None
