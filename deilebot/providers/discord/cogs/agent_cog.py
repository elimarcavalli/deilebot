"""AgentCog — /deile = passthrough direto ao worker via slash_dispatch.

A lógica de safety + persist + dispatch vive em
``deilebot.foundation.slash_dispatch`` — esse cog é apenas a casca
Discord-aware (defer/send/delete deferred). O harness de integração
``/v1/test/simulate`` chama o MESMO ``run_slash_dispatch`` para
garantir que teste e produção exerçam o mesmo caminho.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from deilebot.foundation.envelope import (
    BotUser,
    Channel,
    ChannelScope,
    MessageEnvelope,
)
from deilebot.foundation.slash_dispatch import run_slash_dispatch

_logger = logging.getLogger("deilebot.agent_cog")


class AgentCog(commands.Cog):
    """``/deile`` slash command — thin wrapper around ``run_slash_dispatch``."""

    def __init__(self, bot: Any, runtime: Any, adapter: Any):
        self.bot = bot
        self.runtime = runtime
        self.adapter = adapter

    def _make_envelope(self, ctx: Any, text: str) -> MessageEnvelope:
        """Build a force-respond MessageEnvelope from a slash command context."""
        channel = ctx.channel
        scope = (
            ChannelScope.DM
            if isinstance(channel, discord.DMChannel)
            else ChannelScope.THREAD if isinstance(channel, discord.Thread)
            else ChannelScope.GROUP
        )
        channel_name = getattr(channel, "name", None)
        channel_id = str(channel.id)
        user_id = str(ctx.author.id)
        display_name = (
            getattr(ctx.author, "display_name", None) or ctx.author.name
        )
        message_id = str(getattr(getattr(ctx, "message", None), "id", None) or f"slash:{user_id}")
        return MessageEnvelope(
            message_id=message_id,
            channel=Channel(
                provider="discord",
                provider_channel_id=channel_id,
                name=channel_name,
                scope=scope,
            ),
            author=BotUser(
                bot_user_id=f"discord:{user_id}",
                provider="discord",
                provider_user_id=user_id,
                display_name=display_name,
            ),
            sent_at=datetime.now(tz=timezone.utc),
            text=text,
            raw={"force_respond": True},
        )

    @commands.hybrid_command(
        name="deile",
        description="Passthrough direto ao DEILE worker (sem o bot narrar em cima)",
    )
    @app_commands.describe(
        prompt="O que o DEILE deve fazer (vai DIRETO ao worker; sujeito a safety gate)",
    )
    async def deile(self, ctx: commands.Context, *, prompt: str) -> None:
        await ctx.defer(ephemeral=False)
        try:
            channel = ctx.channel
            scope = (
                ChannelScope.DM
                if isinstance(channel, discord.DMChannel)
                else ChannelScope.THREAD if isinstance(channel, discord.Thread)
                else ChannelScope.GROUP
            )
            channel_name = getattr(channel, "name", None)
            channel_id = str(channel.id)
            display_name = (
                getattr(ctx.author, "display_name", None)
                or ctx.author.name
            )
            result = await run_slash_dispatch(
                prompt=prompt,
                user_id=str(ctx.author.id),
                display_name=display_name,
                channel_id=channel_id,
                store=self.runtime.pipeline.store,
                identity=self.runtime.pipeline.identity,
                channel_scope=scope,
                channel_name=channel_name,
            )

            if result.kind == "blocked":
                await ctx.send(
                    f"❌ **Pedido bloqueado por safety:** {result.reason}\n\n"
                    "Reformule sem termos sensíveis, ou peça via DM normal — "
                    "o LLM lê contexto antes de agir.",
                )
            elif result.kind == "error":
                await ctx.send(f"❌ **Dispatch falhou:** {result.reason}")
            # "dispatched" → o worker já editou status message ao vivo; sem M3.
        finally:
            if ctx.interaction is not None:
                try:
                    await ctx.interaction.delete_original_response()
                except Exception:
                    pass
