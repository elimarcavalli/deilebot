"""Integração: Discord → agent_bridge → AgentInvocation com forge context.

Cobre AC-10 (bot_context["forge_kind"] efetivamente propagado ao AgentBridge.invoke)
e AC-11 (extra_system_prompt referencia template por forge).
"""

from __future__ import annotations

from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deilebot.foundation.agent_bridge import AgentInvocation, AgentResponse
from deilebot.foundation.forge_auth import ForgeKind


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_ctx(author_id: int = 42, channel_id: int = 100) -> MagicMock:
    ctx = MagicMock()
    ctx.defer = AsyncMock()
    ctx.send = AsyncMock()
    ctx.channel.id = channel_id
    ctx.author.id = author_id
    ctx.author.display_name = "alice"
    ctx.author.name = "alice"
    ctx.interaction = None
    return ctx


def _make_runtime(response_text: str = "done") -> MagicMock:
    from deile.common.markup_ast import MarkupAST

    response = AgentResponse(
        text=response_text,
        markup=MarkupAST.from_plain(response_text),
        elapsed_ms=5,
        model_used="test",
    )
    bridge = MagicMock()
    bridge.invoke = AsyncMock(return_value=response)
    pipeline = MagicMock()
    pipeline.bridge = bridge
    runtime = MagicMock()
    runtime.pipeline = pipeline
    return runtime


def _make_cog(runtime=None):
    from deilebot.providers.discord.cogs.git_cog import GitCog

    cog = GitCog.__new__(GitCog)
    cog.bot = None
    cog.runtime = runtime
    cog.adapter = None
    cog._bg_tasks = set()

    async def _always_owner(ctx):
        return True

    cog._is_owner = _always_owner
    return cog


# ── AC-10: bot_context["forge_kind"] propagado ao bridge.invoke ─────────────

async def test_forge_kind_in_bot_context_github(monkeypatch, tmp_path):
    """Disparar /git ideia com forge=github → bridge.invoke recebe forge_kind='github'."""
    from deilebot.foundation.forge_auth import GitHubForgeAuth, GitLabForgeAuth

    svc_gh = GitHubForgeAuth(home=tmp_path)
    svc_gh._write_credential("ghp_VALID")
    svc_gl = GitLabForgeAuth(home=tmp_path)

    url = "https://github.com/elimarcavalli/deile/issues/1"
    runtime = _make_runtime(url)
    cog = _make_cog(runtime)

    def mock_get_service(kind, home=None):
        return svc_gh if kind == ForgeKind.GITHUB else svc_gl

    monkeypatch.setattr(cog, "_get_service", mock_get_service)
    ctx = _make_ctx()

    with patch("deilebot.providers.discord.cogs.git_cog._safe_dm", new=AsyncMock()):
        await cog.git_ideia.callback(cog, ctx, texto="minha ideia", forge="github")

    inv: AgentInvocation = runtime.pipeline.bridge.invoke.call_args[0][0]
    assert inv.bot_context["forge_kind"] == "github"
    assert inv.bot_context["forge_host"] == "github.com"
    assert inv.bot_context["forge_repo"] == "elimarcavalli/deile"


async def test_forge_kind_in_bot_context_gitlab(monkeypatch, tmp_path):
    """Disparar /git ideia com forge=gitlab → bridge.invoke recebe forge_kind='gitlab'."""
    from deilebot.foundation.forge_auth import GitHubForgeAuth, GitLabForgeAuth

    svc_gl = GitLabForgeAuth(home=tmp_path)
    svc_gl._write_credential("glpat-VALID")
    svc_gh = GitHubForgeAuth(home=tmp_path)

    url = "https://gitlab.com/elimarcavalli/deile/-/issues/2"
    runtime = _make_runtime(url)
    cog = _make_cog(runtime)

    def mock_get_service(kind, home=None):
        return svc_gl if kind == ForgeKind.GITLAB else svc_gh

    monkeypatch.setattr(cog, "_get_service", mock_get_service)
    ctx = _make_ctx()

    with patch("deilebot.providers.discord.cogs.git_cog._safe_dm", new=AsyncMock()):
        await cog.git_ideia.callback(cog, ctx, texto="ideia gitlab", forge="gitlab")

    inv: AgentInvocation = runtime.pipeline.bridge.invoke.call_args[0][0]
    assert inv.bot_context["forge_kind"] == "gitlab"
    assert inv.bot_context["forge_host"] == "gitlab.com"
    assert inv.bot_context["forge_repo"] == "elimarcavalli/deile"


# ── AC-11: extra_system_prompt contém o template correto por forge ───────────

async def test_extra_prompt_references_github_template(monkeypatch, tmp_path):
    """forge=github → extra_system_prompt referencia .github/ISSUE_TEMPLATE/intent.md."""
    from deilebot.foundation.forge_auth import GitHubForgeAuth, GitLabForgeAuth

    svc_gh = GitHubForgeAuth(home=tmp_path)
    svc_gh._write_credential("ghp_VALID")
    svc_gl = GitLabForgeAuth(home=tmp_path)

    runtime = _make_runtime("https://github.com/elimarcavalli/deile/issues/10")
    cog = _make_cog(runtime)

    def mock_get_service(kind, home=None):
        return svc_gh if kind == ForgeKind.GITHUB else svc_gl

    monkeypatch.setattr(cog, "_get_service", mock_get_service)
    ctx = _make_ctx()

    with patch("deilebot.providers.discord.cogs.git_cog._safe_dm", new=AsyncMock()):
        await cog.git_ideia.callback(cog, ctx, texto="ideia", forge="github")

    inv: AgentInvocation = runtime.pipeline.bridge.invoke.call_args[0][0]
    assert ".github/ISSUE_TEMPLATE/intent.md" in inv.extra_system_prompt


async def test_extra_prompt_references_gitlab_template(monkeypatch, tmp_path):
    """forge=gitlab → extra_system_prompt referencia .gitlab/issue_templates/intent.md."""
    from deilebot.foundation.forge_auth import GitHubForgeAuth, GitLabForgeAuth

    svc_gl = GitLabForgeAuth(home=tmp_path)
    svc_gl._write_credential("glpat-VALID")
    svc_gh = GitHubForgeAuth(home=tmp_path)

    runtime = _make_runtime("https://gitlab.com/elimarcavalli/deile/-/issues/5")
    cog = _make_cog(runtime)

    def mock_get_service(kind, home=None):
        return svc_gl if kind == ForgeKind.GITLAB else svc_gh

    monkeypatch.setattr(cog, "_get_service", mock_get_service)
    ctx = _make_ctx()

    with patch("deilebot.providers.discord.cogs.git_cog._safe_dm", new=AsyncMock()):
        await cog.git_ideia.callback(cog, ctx, texto="ideia", forge="gitlab")

    inv: AgentInvocation = runtime.pipeline.bridge.invoke.call_args[0][0]
    assert ".gitlab/issue_templates/intent.md" in inv.extra_system_prompt
