"""StatusCog — `/status` slash command (público).

Mostra ao user (qualquer um) o estado de saúde do sistema:
- modelo LLM em uso
- worker reachable y/n
- p50 latência últimos N turnos
- mensagens recebidas hoje
- versão do bot

Não revela secrets nem dados privados de outros users.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import discord
from discord.ext import commands

logger = logging.getLogger("deilebot.cogs.status")


class StatusCog(commands.Cog):
    def __init__(self, bot: commands.Bot, runtime: Any, adapter: Any) -> None:
        self.bot = bot
        self.runtime = runtime
        self.adapter = adapter

    @commands.hybrid_command(name="status", description="Mostra estado do bot, worker e modelo")
    async def status(self, ctx: commands.Context) -> None:
        await ctx.defer(ephemeral=True)

        # 1. Modelo configurado
        model = "?"
        try:
            from deilebot.foundation.settings import get_bot_settings
            settings = get_bot_settings()
            model = settings.foundation.forced_model or settings.foundation.default_model or "(default tier)"
        except Exception:
            pass

        # 2. Worker reachable?
        worker_status = "✅ reachable"
        worker_endpoint = os.environ.get(
            "DEILE_WORKER_ENDPOINT",
            "http://deile-worker.deile.svc.cluster.local:8766",
        )
        try:
            import httpx
            t0 = time.monotonic()
            async with httpx.AsyncClient(timeout=3) as cli:
                r = await cli.get(f"{worker_endpoint.rstrip('/')}/v1/health")
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            if r.status_code == 200:
                worker_status = f"✅ ok ({elapsed_ms}ms)"
            else:
                worker_status = f"⚠️ HTTP {r.status_code}"
        except Exception as exc:
            worker_status = f"❌ {type(exc).__name__}"

        # 3. Histórico do bot (mensagens hoje)
        msgs_today = "?"
        try:
            from datetime import datetime, timezone
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            import sqlite3
            sqlite_path = os.environ.get("DEILE_BOT_SQLITE_PATH", "/home/deile/data/deilebot.sqlite")
            conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM message WHERE substr(created_at,1,10)=?",
                    (today,),
                ).fetchone()
                msgs_today = str(row[0]) if row else "0"
            finally:
                conn.close()
        except Exception:
            pass

        # 4. Tools registradas / habilitadas (no agent embedded)
        tools_info = "?"
        try:
            agent = await self._get_agent()
            if agent is not None:
                enabled = agent.tool_registry.list_enabled()
                tools_info = f"{len(enabled)} tool(s) habilitada(s)"
        except Exception:
            pass

        # 5. Build embed
        embed = discord.Embed(
            title="🤖 DEILE Status",
            color=0x5865F2,
            description="Estado de saúde do sistema",
        )
        embed.add_field(name="Modelo", value=f"`{model}`", inline=False)
        embed.add_field(name="Worker", value=worker_status, inline=True)
        embed.add_field(name="Mensagens hoje", value=msgs_today, inline=True)
        embed.add_field(name="Tools", value=tools_info, inline=True)
        embed.set_footer(text="Use /help para ver os comandos disponíveis")

        await ctx.send(embed=embed, ephemeral=True)

    async def _get_agent(self) -> Optional[Any]:
        """Pull the agent from runtime.bridge if available."""
        try:
            bridge = getattr(self.runtime.pipeline, "bridge", None)
            if bridge is None:
                return None
            provider = getattr(bridge, "_agent_provider", None)
            if provider is None:
                return None
            return await provider()
        except Exception:
            return None
