"""PrivacyCog — comandos públicos de privacidade do USER (não-owner).

`/forget_me` apaga histórico do próprio user (mensagens, sessions DEILE).
Atende GDPR/LGPD self-service. Não exige permissão owner.
"""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord.ext import commands

logger = logging.getLogger("deilebot.cogs.privacy")


class PrivacyCog(commands.Cog):
    def __init__(self, bot: commands.Bot, runtime: Any, adapter: Any) -> None:
        self.bot = bot
        self.runtime = runtime
        self.adapter = adapter

    @commands.hybrid_command(
        name="forget_me",
        description="Apaga seu histórico (mensagens, sessões) do bot",
    )
    async def forget_me(self, ctx: commands.Context) -> None:
        await ctx.defer(ephemeral=True)
        provider_user_id = str(ctx.author.id)
        deleted_msgs = 0
        deleted_sessions = 0

        # 1. Apagar mensagens do conversation_store
        try:
            store = self.runtime.pipeline.store
            user = await store.get_user_by_provider("discord", provider_user_id)
            if user is not None and hasattr(store, "delete_messages_for"):
                deleted_msgs = await store.delete_messages_for(user.bot_user_id)
            elif user is not None:
                # Fallback: raw SQL on the inner sqlite (best-effort).
                import sqlite3
                import os
                sqlite_path = os.environ.get(
                    "DEILE_BOT_SQLITE_PATH", "/home/deile/data/deilebot.sqlite"
                )
                conn = sqlite3.connect(sqlite_path)
                try:
                    cur = conn.execute(
                        "DELETE FROM message WHERE bot_user_id = ?", (user.bot_user_id,)
                    )
                    conn.commit()
                    deleted_msgs = cur.rowcount or 0
                finally:
                    conn.close()
        except Exception as exc:
            logger.warning("forget_me message delete failed: %s", exc, exc_info=True)

        # 2. Apagar session DEILE
        try:
            agent_provider = getattr(self.runtime.pipeline.bridge, "_agent_provider", None)
            if agent_provider is not None:
                agent = await agent_provider()
                session_id = f"bot_session_{user.bot_user_id}" if user else None
                if session_id and hasattr(agent, "delete_session"):
                    await agent.delete_session(session_id)
                    deleted_sessions = 1
        except Exception as exc:
            logger.warning("forget_me session delete failed: %s", exc, exc_info=True)

        embed = discord.Embed(
            title="🧹 Memória limpa",
            color=0x57F287,
            description=(
                f"Apaguei **{deleted_msgs}** mensagem(ns) e "
                f"**{deleted_sessions}** sessão(ões) do meu lado.\n"
                "O Discord ainda mantém o histórico no chat do seu cliente — "
                "isso é fora do meu controle."
            ),
        )
        await ctx.send(embed=embed, ephemeral=True)
