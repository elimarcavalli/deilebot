"""MonitorCog — `/monitor` (owner-only): fala com o deile-monitor.

O deile-monitor é o único pod que enxerga o cluster inteiro (kubectl +
forge + ``/state``). Este cog é a ponte do Discord para ele, via o
``MonitorClient`` (transporte HTTP em ``deile.infrastructure.deile_monitor_client``).

Subcomandos (todos owner-only — reusam o gate de ``admin_cog``):

* ``/monitor status``        — estado determinístico (sem LLM): último tick,
                               idade, pausa, anomalias abertas. Renderiza embed.
* ``/monitor pause <dur>``   — pausa o monitor por uma duração (ex.: ``30m``).
* ``/monitor resume``        — retoma o monitor.
* ``/monitor ack <fp>``      — silencia uma anomalia pelo fingerprint.
* ``/monitor force_tick``    — dispara um tick imediato.
* ``/monitor ask <pergunta>``— Q&A read-only sobre o cluster (defer + poll).

O ``MonitorClient`` é importado LAZILY dentro de cada método: o pacote
``deile`` é pesado e o import no topo quebraria a coletabilidade do módulo
em ambientes de teste sem o cluster.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from deilebot.foundation.logging import get_logger

from .admin_cog import _is_owner_check

_logger = get_logger("monitor_cog")

# Poll do /monitor ask: o servidor responde 202 e o resultado é colhido
# depois. Discord dá 15 min após o defer; ficamos bem abaixo disso.
_ASK_POLL_INTERVAL_S = 2.0
_ASK_POLL_TIMEOUT_S = 170.0

# Embed: limite de anomalias renderizadas individualmente (Discord cap 25
# fields; mantemos curto pra legibilidade no celular).
_MAX_ANOMALY_FIELDS = 5


def _severity_color(anomalies: list) -> int:
    """Cor do embed pela maior severidade aberta (P1 vermelho → verde se limpo)."""
    sevs = {str(a.get("severity", "")).upper() for a in anomalies}
    if "P1" in sevs:
        return 0xED4245  # vermelho
    if "P2" in sevs:
        return 0xFEE75C  # amarelo
    if anomalies:
        return 0x5865F2  # azul (anomalias menores)
    return 0x57F287  # verde (limpo)


def _fmt_age(age_s: Optional[int]) -> str:
    if age_s is None:
        return "desconhecida"
    if age_s < 90:
        return f"{age_s}s atrás"
    if age_s < 5400:
        return f"{age_s // 60}min atrás"
    return f"{age_s // 3600}h atrás"


def _build_status_embed(status: dict) -> discord.Embed:
    anomalies = status.get("known_anomalies") or []
    paused = bool(status.get("paused"))
    embed = discord.Embed(
        title="🛰️ DEILE Monitor",
        color=_severity_color(anomalies),
        description="⏸️ **pausado**" if paused else "▶️ ativo",
    )
    last_tick = status.get("last_tick") or "?"
    embed.add_field(
        name="Último tick",
        value=f"`{last_tick}` ({_fmt_age(status.get('age_s'))})",
        inline=False,
    )
    if paused and status.get("paused_until"):
        embed.add_field(name="Pausado até", value=f"`{status['paused_until']}`", inline=True)
    embed.add_field(
        name="Anomalias abertas",
        value=str(status.get("anomalies_total", len(anomalies))),
        inline=True,
    )
    embed.add_field(
        name="Notificações (hora)",
        value=str(status.get("notifications_this_hour", 0)),
        inline=True,
    )
    for anomaly in anomalies[:_MAX_ANOMALY_FIELDS]:
        sev = anomaly.get("severity") or "?"
        typ = anomaly.get("type") or "?"
        count = anomaly.get("count") or "?"
        fp = str(anomaly.get("fingerprint") or "")[:16]
        embed.add_field(
            name=f"[{sev}] {typ}",
            value=f"`{fp}` ×{count}",
            inline=False,
        )
    if len(anomalies) > _MAX_ANOMALY_FIELDS:
        embed.set_footer(text=f"... e mais {len(anomalies) - _MAX_ANOMALY_FIELDS} anomalia(s)")
    return embed


class MonitorCog(commands.Cog):
    """Ponte Discord → deile-monitor (owner-only)."""

    def __init__(self, bot: Any, runtime: Any, adapter: Any) -> None:
        self.bot = bot
        self.runtime = runtime
        self.adapter = adapter

    async def _is_owner(self, ctx: commands.Context) -> bool:
        return await _is_owner_check(self.adapter)(ctx)

    def _load_monitor(self):
        """Import lazy de ``(MonitorClient, MonitorClientError)``.

        Lazy porque o pacote ``deile`` é pesado e o ``MonitorClient`` vem do
        repo deile (PR que mergeia primeiro, ver spec §8) — manter o import
        fora do topo deixa o módulo coletável em qualquer ambiente. Centralizado
        num único helper para que o cog tenha UM ponto de acoplamento ao deile e
        os testes consigam injetar dublês sem tocar HTTP nem o módulo real."""
        from deile.infrastructure.deile_monitor_client import (
            MonitorClient, MonitorClientError)

        return MonitorClient, MonitorClientError

    @commands.hybrid_group(name="monitor", description="Fala com o deile-monitor (owner only)")
    @app_commands.default_permissions(administrator=True)
    async def monitor(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send("usage: /monitor status|pause|resume|ack|force_tick|ask")

    @monitor.command(name="status", description="Estado determinístico do monitor (sem LLM)")
    async def status(self, ctx: commands.Context) -> None:
        if not await self._is_owner(ctx):
            await ctx.send("⛔ owner only")
            return
        client_cls, MonitorClientError = self._load_monitor()

        try:
            data = await client_cls().get_status()
        except MonitorClientError as exc:
            await ctx.send(f"⚠️ monitor não respondeu (`{exc.code}`): {exc}")
            return
        await ctx.send(embed=_build_status_embed(data))

    async def _send_command(self, ctx: commands.Context, command: str) -> None:
        """Despacha uma ordem ao monitor e confirma com o ``effect`` retornado."""
        if not await self._is_owner(ctx):
            await ctx.send("⛔ owner only")
            return
        client_cls, MonitorClientError = self._load_monitor()

        try:
            res = await client_cls().post_command(command)
        except MonitorClientError as exc:
            await ctx.send(f"⚠️ ordem `{command}` falhou (`{exc.code}`): {exc}")
            return
        effect = res.get("effect") or "ok"
        await ctx.send(f"✅ `{res.get('command', command)}` — {effect}")

    @monitor.command(name="pause", description="Pausa o monitor por uma duração (ex.: 30m, 1h)")
    @app_commands.describe(duration="Duração da pausa: 30m, 1h, 600s (vazio = indefinido)")
    async def pause(self, ctx: commands.Context, duration: Optional[str] = None) -> None:
        cmd = f"pause {duration}".strip() if duration else "pause"
        await self._send_command(ctx, cmd)

    @monitor.command(name="resume", description="Retoma o monitor")
    async def resume(self, ctx: commands.Context) -> None:
        await self._send_command(ctx, "resume")

    @monitor.command(name="ack", description="Silencia uma anomalia pelo fingerprint")
    @app_commands.describe(fingerprint="Fingerprint da anomalia (ver /monitor status)")
    async def ack(self, ctx: commands.Context, fingerprint: str) -> None:
        await self._send_command(ctx, f"ack {fingerprint}")

    @monitor.command(name="force_tick", description="Dispara um tick do monitor imediatamente")
    async def force_tick(self, ctx: commands.Context) -> None:
        await self._send_command(ctx, "force-tick")

    @monitor.command(name="ask", description="Pergunta read-only sobre cluster/pipeline (Q&A)")
    @app_commands.describe(question="Pergunta em linguagem natural sobre o cluster")
    async def ask(self, ctx: commands.Context, *, question: str) -> None:
        if not await self._is_owner(ctx):
            await ctx.send("⛔ owner only")
            return
        # Defer: o Q&A roda um DEILE read-only no pod do monitor e pode levar
        # dezenas de segundos. Discord permite até 15 min depois do defer.
        await ctx.defer(ephemeral=False)
        client_cls, MonitorClientError = self._load_monitor()

        client = client_cls()
        try:
            request_id = await client.ask(question)
        except MonitorClientError as exc:
            await ctx.send(f"⚠️ não consegui perguntar ao monitor (`{exc.code}`): {exc}")
            return

        loop = asyncio.get_running_loop()
        deadline = loop.time() + _ASK_POLL_TIMEOUT_S
        result: dict = {}
        while loop.time() < deadline:
            try:
                result = await client.get_ask_result(request_id)
            except MonitorClientError as exc:
                await ctx.send(f"⚠️ erro ao buscar a resposta (`{exc.code}`): {exc}")
                return
            if result.get("status") != "running":
                break
            await asyncio.sleep(_ASK_POLL_INTERVAL_S)
        else:
            await ctx.send("⏱️ o monitor demorou demais para responder. Tente `/monitor ask` de novo.")
            return

        if result.get("status") == "done":
            answer = str(result.get("answer") or "(resposta vazia)")
            await ctx.send(answer[:1990])
        else:
            error = str(result.get("error") or "erro desconhecido")
            await ctx.send(f"⚠️ o monitor não conseguiu responder: {error[:1800]}")
