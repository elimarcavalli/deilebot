"""PrivacyCog — comandos públicos de privacidade do USER (não-owner).

`/forget_me` apaga TODO o rastro de memória do próprio user:
- o transcript do conversation_store (mensagens, ambos os lados em DMs);
- a ``AgentSession`` viva do agente embutido (working memory em RAM);
- a sessão persistida do agente (a que sobrevive a um restart).

Atende GDPR/LGPD self-service e não exige permissão de owner. A lógica
compartilhada vive em ``deilebot.foundation.memory_ops.forget_user`` — o
MESMO código que `/forget` (admin) e o harness `/v1/test/simulate`
exercitam, então teste e produção nunca divergem.
"""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord.ext import commands

from deilebot.foundation.memory_ops import forget_user

logger = logging.getLogger("deilebot.cogs.privacy")


class PrivacyCog(commands.Cog):
    def __init__(self, bot: commands.Bot, runtime: Any, adapter: Any) -> None:
        self.bot = bot
        self.runtime = runtime
        self.adapter = adapter

    @commands.hybrid_command(
        name="forget_me",
        description="Apaga TODO o seu histórico e memória do bot (mensagens + sessão)",
    )
    async def forget_me(self, ctx: commands.Context) -> None:
        await ctx.defer(ephemeral=True)
        pipeline = self.runtime.pipeline
        store = pipeline.store

        user = await store.get_user_by_provider_id("discord", str(ctx.author.id))
        if user is None:
            # Nenhum bot_user ⇒ nada foi persistido ⇒ nada a esquecer.
            embed = discord.Embed(
                title="🧹 Nada para apagar",
                color=0x57F287,
                description=(
                    "Não encontrei nenhum histórico seu do meu lado — "
                    "você já está esquecido. 🙂"
                ),
            )
            await ctx.send(embed=embed, ephemeral=True)
            return

        result = await forget_user(
            store=store,
            bridge=pipeline.bridge,
            bot_user_id=user.bot_user_id,
        )

        lines = [
            f"Apaguei **{result.messages_deleted}** mensagem(ns) "
            f"em **{result.private_channels}** conversa(s) privada(s)."
        ]
        if result.session_cleared:
            lines.append(
                "Limpei também minha **memória de trabalho** sobre você "
                "(sessão viva + persistida) — no próximo papo eu começo do zero."
            )
        else:
            lines.append("Não havia memória de trabalho ativa sobre você.")
        lines.append(
            "O Discord ainda mantém o histórico no chat do seu cliente — "
            "isso é fora do meu controle."
        )

        color = 0x57F287
        if result.errors:
            color = 0xFEE75C
            logger.warning("forget_me partial failure: %s", result.errors)
            lines.append(
                "⚠️ Alguns passos falharam — avise o operador: "
                + "; ".join(result.errors)
            )

        embed = discord.Embed(
            title="🧹 Memória limpa",
            color=color,
            description="\n".join(lines),
        )
        await ctx.send(embed=embed, ephemeral=True)
