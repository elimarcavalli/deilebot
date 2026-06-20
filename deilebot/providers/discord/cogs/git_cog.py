"""GitCog — grupo nativo /git com subcomandos login/status/logout/ideia.

Substitui diretamente GitHubAuthCog e IdeaCog (quebra direta, sem alias).
Suporta GitHub (PAT + OAuth device flow) e GitLab (PAT em V1).

Subcomandos:
  /git login   — Modal com campo Forge (github/gitlab) + TextInput(token).
  /git status  — Revalida ao vivo; 3 estados por forge (✅/⚠️/⛔).
  /git logout  — Remove credencial de um forge (opcional) ou pede confirmação.
  /git ideia   — Integra o antigo IdeaCog; seleciona forge automaticamente.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands

from deilebot.foundation.agent_bridge import AgentInvocation
from deilebot.foundation.exceptions import AgentInvocationError, AgentInvocationTimeout
from deilebot.foundation.forge_auth import (
    ForgeAuthError,
    ForgeKind,
    ForgeTimeoutError,
    GitHubForgeAuth,
    GitLabForgeAuth,
    get_forge_auth,
)
from deilebot.foundation.settings import get_bot_settings
from deilebot.providers.discord.cogs.admin_cog import _is_owner_check

logger = logging.getLogger("deilebot.cogs.git")

_COLOR_OK = 0x57F287
_COLOR_WARN = 0xFEE75C
_COLOR_ERR = 0xED4245
_COLOR_INFO = 0x5865F2
_COLOR_GITLAB = 0xFC6D26  # laranja GitLab
_COLOR_GITHUB = 0x5865F2  # azul Discord / GitHub

# Repositório-alvo hardcoded (configurável é V2, rastreado em #20).
_IDEA_TARGET_REPO = "elimarcavalli/deile"

_GITHUB_URL_RE = re.compile(r"https://github\.com/[^\s\"'<>]+/issues/\d+", re.IGNORECASE)
_GITLAB_URL_RE = re.compile(r"https://[^\s\"'<>]+/-/issues/\d+", re.IGNORECASE)

_NOTIFY_USER_ID: str = os.environ.get("DEILE_PIPELINE_NOTIFY_USER_ID", "")

_EXTRA_SYSTEM_PROMPTS: Dict[str, str] = {
    "github": (
        "CONTEXT: The user is submitting a raw idea for the DEILE project. "
        "Your ONLY job in this response is to open a new GitHub INTENT issue "
        f"in the repository {_IDEA_TARGET_REPO} following the template at "
        ".github/ISSUE_TEMPLATE/intent.md. Fill in all sections based on the idea. "
        "When done, respond with EXACTLY one line containing the full GitHub issue URL "
        f"(e.g. https://github.com/{_IDEA_TARGET_REPO}/issues/N). "
        "Do not ask for clarification — infer and act now."
    ),
    "gitlab": (
        "CONTEXT: The user is submitting a raw idea for the DEILE project. "
        "Your ONLY job in this response is to open a new GitLab INTENT issue "
        f"in the repository {_IDEA_TARGET_REPO} following the template at "
        ".gitlab/issue_templates/intent.md. Fill in all sections based on the idea. "
        "When done, respond with EXACTLY one line containing the full GitLab issue URL "
        f"(e.g. https://gitlab.com/{_IDEA_TARGET_REPO}/-/issues/N). "
        "Do not ask for clarification — infer and act now."
    ),
}


def _extract_issue_url(text: str, forge: str) -> Optional[str]:
    """Extrai URL de issue do texto, conforme o forge."""
    if forge == "gitlab":
        m = _GITLAB_URL_RE.search(text)
    else:
        m = _GITHUB_URL_RE.search(text)
    return m.group(0) if m else None


def _forge_label(forge: str) -> str:
    return "GitHub" if forge == "github" else "GitLab"


async def _safe_dm(text: str) -> None:
    if not _NOTIFY_USER_ID:
        return
    try:
        from deilebot.bridge.dm_tool import send_discord_dm
        await send_discord_dm(_NOTIFY_USER_ID, text)
    except Exception as exc:
        logger.warning("git_cog: owner DM failed: %s", exc)


# ----- Modal de login -------------------------------------------------------

class _PatLoginModal(discord.ui.Modal, title="Login forge — colar token"):
    """Modal onde o operador escolhe o forge e cola o PAT."""

    forge_select: discord.ui.TextInput = discord.ui.TextInput(
        label="Forge (github ou gitlab)",
        placeholder="github",
        style=discord.TextStyle.short,
        required=True,
        min_length=6,
        max_length=8,
    )
    token: discord.ui.TextInput = discord.ui.TextInput(
        label="Personal Access Token",
        placeholder="ghp_… / github_pat_… / glpat-…",
        style=discord.TextStyle.short,
        required=True,
        min_length=10,
        max_length=255,
    )

    def __init__(self, home=None, forge_settings=None) -> None:
        super().__init__()
        self._home = home
        self._forge_settings = forge_settings

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        forge_raw = str(self.forge_select.value or "").strip().lower()
        raw_token = str(self.token.value or "").strip()

        if forge_raw not in ("github", "gitlab"):
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Forge inválido",
                    color=_COLOR_ERR,
                    description="Informe `github` ou `gitlab` no campo Forge.",
                ),
                ephemeral=True,
            )
            return

        kind = ForgeKind.GITHUB if forge_raw == "github" else ForgeKind.GITLAB
        timeout = 15.0
        host = None
        if self._forge_settings is not None:
            if kind == ForgeKind.GITHUB:
                timeout = self._forge_settings.github.timeout
            else:
                timeout = self._forge_settings.gitlab.timeout
                host = self._forge_settings.gitlab.host or None

        svc = get_forge_auth(kind, home=self._home, timeout=timeout, host=host)
        try:
            identity = await svc.install_credentials(raw_token, method="pat")
        except ForgeTimeoutError as exc:
            await interaction.followup.send(
                embed=discord.Embed(
                    title=f"⏱️ Timeout — {_forge_label(forge_raw)}",
                    color=_COLOR_ERR,
                    description=str(exc),
                ),
                ephemeral=True,
            )
            return
        except ForgeAuthError as exc:
            await interaction.followup.send(
                embed=discord.Embed(
                    title=f"❌ Falha no login {_forge_label(forge_raw)}",
                    color=_COLOR_ERR,
                    description=str(exc),
                ),
                ephemeral=True,
            )
            return
        except Exception as exc:
            logger.exception("git_cog: pat install falhou")
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Erro inesperado",
                    color=_COLOR_ERR,
                    description=f"{type(exc).__name__}",
                ),
                ephemeral=True,
            )
            return

        color = _COLOR_GITHUB if forge_raw == "github" else _COLOR_GITLAB
        await interaction.followup.send(
            embed=discord.Embed(
                title=f"✅ {_forge_label(forge_raw)} conectado",
                color=color,
                description=(
                    f"Autenticado como **`{identity.login}`** "
                    f"({identity.token_type}, via PAT).\n"
                    "A credencial git foi instalada com segurança "
                    "(`~/.git-credentials`, modo 0600)."
                ),
            ),
            ephemeral=True,
        )


# ----- View de seleção de forge --------------------------------------------

class _ForgeChoiceView(discord.ui.View):
    """Botões para o usuário escolher um forge."""

    def __init__(self, forges: list, timeout: float = 60.0) -> None:
        super().__init__(timeout=timeout)
        self.chosen: Optional[str] = None
        for forge in forges:
            label = "GitHub" if forge == "github" else "GitLab"
            style = discord.ButtonStyle.primary if forge == "github" else discord.ButtonStyle.danger
            btn = discord.ui.Button(label=label, style=style, custom_id=forge)
            btn.callback = self._make_callback(forge)
            self.add_item(btn)
        cancel = discord.ui.Button(
            label="Cancelar", style=discord.ButtonStyle.secondary, custom_id="cancel"
        )
        cancel.callback = self._make_callback(None)
        self.add_item(cancel)

    def _make_callback(self, value):
        async def callback(interaction: discord.Interaction):
            self.chosen = value
            self.stop()
            await interaction.response.defer()
        return callback


# ----- GitCog (grupo /git) -------------------------------------------------

class GitCog(commands.Cog, name="git"):
    """Cog que expõe o grupo /git (login/status/logout/ideia).

    Usa hybrid_group para criar um grupo de subcomandos Discord nativo.
    """

    def __init__(self, bot: Any, runtime: Any, adapter: Any) -> None:
        self.bot = bot
        self.runtime = runtime
        self.adapter = adapter
        self._bg_tasks: set = set()

    async def _is_owner(self, ctx: commands.Context) -> bool:
        return await _is_owner_check(self.adapter)(ctx)

    def _get_service(self, kind: ForgeKind, home=None):
        settings = get_bot_settings().forge
        if kind == ForgeKind.GITHUB:
            return GitHubForgeAuth(home=home, timeout=settings.github.timeout)
        host = settings.gitlab.host or "gitlab.com"
        return GitLabForgeAuth(home=home, timeout=settings.gitlab.timeout, host=host)

    # ----- grupo /git -------------------------------------------------------

    @commands.hybrid_group(name="git", description="Comandos de autenticação forge")
    async def git(self, ctx: commands.Context) -> None:
        """Raiz do grupo /git — mostra ajuda se invocado sem subcomando."""
        if ctx.invoked_subcommand is None:
            await ctx.send(
                "Subcomandos disponíveis: `login`, `status`, `logout`, `ideia`",
                ephemeral=True,
            )

    # ----- /git login -------------------------------------------------------

    @git.command(name="login", description="Autentica o git do bot num forge (owner only)")
    @app_commands.default_permissions(administrator=True)
    async def git_login(self, ctx: commands.Context) -> None:
        if not await self._is_owner(ctx):
            await ctx.send("⛔ owner only")
            return
        if ctx.interaction is None:
            await ctx.send(
                "Use a slash command `/git login` — este comando precisa "
                "de uma interação do Discord."
            )
            return
        settings = get_bot_settings().forge
        await ctx.interaction.response.send_modal(
            _PatLoginModal(forge_settings=settings)
        )

    # ----- /git status ------------------------------------------------------

    @git.command(name="status", description="Mostra o status de autenticação dos forges (owner only)")
    @app_commands.default_permissions(administrator=True)
    async def git_status(self, ctx: commands.Context) -> None:
        if not await self._is_owner(ctx):
            await ctx.send("⛔ owner only")
            return
        await ctx.defer(ephemeral=True)

        embed = discord.Embed(title="🔐 Git — status de autenticação", color=_COLOR_INFO)

        for kind in (ForgeKind.GITHUB, ForgeKind.GITLAB):
            svc = self._get_service(kind)
            label = _forge_label(kind.value)
            token = svc.stored_token()
            if token is None:
                embed.add_field(
                    name=f"⛔ {label}",
                    value="Não configurado. Use `/git login`.",
                    inline=False,
                )
                continue
            try:
                identity = await svc.validate_token(token)
                embed.add_field(
                    name=f"✅ {label}",
                    value=f"`{identity.login}` ({identity.token_type})",
                    inline=False,
                )
            except ForgeTimeoutError as exc:
                embed.add_field(
                    name=f"⚠️ {label} — timeout",
                    value=str(exc),
                    inline=False,
                )
            except ForgeAuthError:
                meta = svc.stored_metadata() or {}
                login = meta.get("login", "?")
                embed.add_field(
                    name=f"⚠️ {label} — token inválido",
                    value=(
                        f"Havia um login (`{login}`), mas o token não é mais válido.\n"
                        "Refaça o `/git login`."
                    ),
                    inline=False,
                )

        await ctx.send(embed=embed, ephemeral=True)

    # ----- /git logout ------------------------------------------------------

    @git.command(name="logout", description="Remove a credencial de um forge (owner only)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(forge="Forge a desconectar: github | gitlab (padrão: todos configurados)")
    async def git_logout(
        self, ctx: commands.Context, forge: Optional[Literal["github", "gitlab"]] = None
    ) -> None:
        if not await self._is_owner(ctx):
            await ctx.send("⛔ owner only")
            return
        await ctx.defer(ephemeral=True)

        if forge:
            kinds = [ForgeKind(forge)]
        else:
            configured = [
                k for k in (ForgeKind.GITHUB, ForgeKind.GITLAB)
                if self._get_service(k).stored_token() is not None
            ]
            if not configured:
                await ctx.send(
                    embed=discord.Embed(
                        title="🔓 Git — nada a remover",
                        color=_COLOR_WARN,
                        description="Não havia nenhum login ativo.",
                    ),
                    ephemeral=True,
                )
                return
            kinds = configured

        removed_labels = []
        for kind in kinds:
            svc = self._get_service(kind)
            if await svc.logout():
                removed_labels.append(_forge_label(kind.value))

        if removed_labels:
            await ctx.send(
                embed=discord.Embed(
                    title="🔓 Git — logout",
                    color=_COLOR_OK,
                    description=f"Credencial removida: {', '.join(removed_labels)}",
                ),
                ephemeral=True,
            )
        else:
            await ctx.send(
                embed=discord.Embed(
                    title="🔓 Git — nada a remover",
                    color=_COLOR_WARN,
                    description="Não havia login ativo para os forges especificados.",
                ),
                ephemeral=True,
            )

    # ----- /git ideia -------------------------------------------------------

    @git.command(
        name="ideia",
        description="Submete uma ideia para criar uma issue INTENT no forge configurado",
    )
    @app_commands.describe(
        texto="Descreva a ideia em linguagem natural",
        forge="Forge alvo: github | gitlab (padrão: detectado automaticamente)",
    )
    async def git_ideia(
        self,
        ctx: commands.Context,
        *,
        texto: str,
        forge: Optional[Literal["github", "gitlab"]] = None,
    ) -> None:
        await ctx.defer(ephemeral=False)

        author_name = getattr(ctx.author, "display_name", None) or ctx.author.name
        author_id = str(ctx.author.id)

        configured = [
            k for k in (ForgeKind.GITHUB, ForgeKind.GITLAB)
            if self._get_service(k).stored_token() is not None
        ]

        if not configured:
            await ctx.send(
                "⛔ Nenhum forge configurado. Use `/git login` para autenticar."
            )
            return

        chosen_kind: Optional[ForgeKind] = None
        if forge:
            chosen_kind = ForgeKind(forge)
        elif len(configured) == 1:
            chosen_kind = configured[0]
        else:
            view = _ForgeChoiceView([k.value for k in configured])
            msg = await ctx.send(
                "Qual forge usar para esta ideia?", view=view, ephemeral=True
            )
            await view.wait()
            if view.chosen is None:
                await msg.edit(content="Cancelado.", view=None)
                return
            chosen_kind = ForgeKind(view.chosen)
            await msg.edit(
                content=f"Usando {_forge_label(chosen_kind.value)}.", view=None
            )

        forge_str = chosen_kind.value
        extra_prompt = _EXTRA_SYSTEM_PROMPTS[forge_str]
        host = (
            get_bot_settings().forge.gitlab.host or "gitlab.com"
            if forge_str == "gitlab"
            else "github.com"
        )

        await _safe_dm(
            f"💡 **Nova ideia recebida** de `{author_name}` (id={author_id}) "
            f"[forge={forge_str}]:\n> {texto}"
        )

        inv = AgentInvocation(
            bot_user_id=f"discord-{author_id}",
            persona="developer",
            forced_model=None,
            inbound_text=texto,
            extra_system_prompt=extra_prompt,
            bot_context={
                "provider": "discord",
                "channel_id": str(ctx.channel.id),
                "user_message_id": str(getattr(ctx, "message", None).id)
                if getattr(ctx, "message", None) else None,
                "user_id": author_id,
                "author_display_name": author_name,
                "source": "slash:/git ideia",
                "forge_kind": forge_str,
                "forge_host": host,
                "forge_repo": _IDEA_TARGET_REPO,
            },
            timeout_seconds=180,
        )

        try:
            bridge = self.runtime.pipeline.bridge
            response = await bridge.invoke(inv)
            response_text = response.text or ""
        except AgentInvocationTimeout:
            logger.warning("git_cog: bridge timed out for user %s", author_id)
            await _safe_dm(f"⏱️ Timeout ao processar ideia de `{author_name}`.")
            await ctx.send("⏱️ O agente demorou demais. Tente novamente em instantes.")
            return
        except AgentInvocationError as exc:
            logger.error("git_cog: bridge error for user %s: %s", author_id, exc)
            await _safe_dm(f"⚠️ Erro ao processar ideia de `{author_name}`: {exc}")
            await ctx.send("⚠️ Não foi possível processar a ideia agora. Tente novamente.")
            return

        issue_url = _extract_issue_url(response_text, forge_str)
        if issue_url:
            await ctx.send(f"✅ Issue criada com sucesso!\n🔗 {issue_url}")
            await _safe_dm(
                f"✅ **Issue criada** [{forge_str}] a partir de ideia de `{author_name}`:\n🔗 {issue_url}"
            )
        else:
            short = response_text[:1800] if len(response_text) > 1800 else response_text
            await ctx.send(f"📝 DEILE processou a ideia:\n{short}")
            await _safe_dm(
                f"⚠️ **Ideia processada** de `{author_name}` — URL não encontrada na resposta."
            )

        if ctx.interaction is not None:
            try:
                await ctx.interaction.delete_original_response()
            except Exception:
                pass

    # ----- on_command_error: mensagem de migração dos /github_* legados -----

    @commands.Cog.listener()
    async def on_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if not isinstance(error, commands.CommandNotFound):
            return
        name = getattr(ctx, "invoked_with", "") or ""
        _LEGACY_MAP = {
            "github_login": "login",
            "github_status": "status",
            "github_logout": "logout",
        }
        if name in _LEGACY_MAP:
            new = _LEGACY_MAP[name]
            await ctx.send(
                f"⚠️ `/github_{new}` foi renomeado para `/git {new}` — veja o README.",
                ephemeral=True,
            )
