"""CronCog — slash commands for scheduling DEILE tasks.

Commands:
    /agendar prompt:<text> quando:<text>   Schedule a new task.
    /agendamentos                           List scheduled tasks.
    /cancelar id:<text>                     Cancel a task by id.

``quando`` accepts (delegated to :func:`deile.cron.parsing.parse_natural_schedule`):
    - 5-field cron (``*/5 * * * *``) — recurring, UTC.
    - ISO 8601 ±TZ (``2026-05-15T12:30Z`` / ``+00:00``).
    - ISO 8601 naive (``2026-05-15T12:30``) — assumido BRT.
    - BR humano (``15/05/2026 09:30`` / ``15/05 09:30``) — BRT.
    - Linguagem natural (``amanhã 14h``, ``hoje 23:00``) — BRT.

Dependencies:
    ``deile.cron.store.CronStore`` — accessed directly (no tool layer).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from discord import app_commands
from discord.ext import commands

from deile.cron.parsing import ScheduleParseError, parse_natural_schedule
from deile.cron.store import CronEntry, make_id
from deilebot.foundation.logging import get_logger

_logger = get_logger("cron_cog")


def _parse_quando(quando: str) -> tuple[str | None, datetime | None]:
    """Thin wrapper around :func:`parse_natural_schedule` for backwards
    compatibility — re-raises ``ScheduleParseError`` as ``ValueError`` so
    callers don't have to import the parsing module."""
    try:
        return parse_natural_schedule(quando)
    except ScheduleParseError as exc:
        raise ValueError(str(exc)) from exc


class CronCog(commands.Cog):
    """Cog that exposes scheduling commands backed by CronStore."""

    def __init__(self, bot: Any, store: Any) -> None:
        self.bot = bot
        self.store = store

    # ------------------------------------------------------------------
    # /agendar
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="agendar",
        description="Agenda uma tarefa para o agente DEILE",
    )
    @app_commands.describe(
        prompt="O que o agente deve fazer",
        quando="Quando: '15/05 09:30' (BRT) | 'amanhã 14h' | '2026-05-15T12:30Z' | '*/5 * * * *' (cron)",
    )
    async def agendar(self, ctx: commands.Context, *, prompt: str, quando: str) -> None:
        await ctx.defer(ephemeral=False)
        try:
            cron_expr, run_at = _parse_quando(quando)
        except ValueError as exc:
            await ctx.send(f"❌ {exc}")
            return

        try:
            # Build the CronEntry on the cog side; the store accepts a
            # constructed entry rather than building one for you. The
            # store API is synchronous — push it to a thread so the
            # discord.py event loop keeps reacting.
            entry = CronEntry(
                id=make_id(),
                prompt=prompt,
                cron=cron_expr,
                run_at=run_at,
                created_by=f"discord:{ctx.author.id}",
                notify_user_id=str(ctx.author.id),
            )
            await asyncio.to_thread(self.store.add, entry)
            task_id = entry.id

            if cron_expr:
                await ctx.send(
                    f"✅ Tarefa agendada (recorrente)!\n"
                    f"🆔 `{task_id}`\n"
                    f"🕐 Cron: `{cron_expr}`\n"
                    f"📝 {prompt}"
                )
            else:
                run_at_str = run_at.isoformat() if run_at else "?"
                await ctx.send(
                    f"✅ Tarefa agendada!\n"
                    f"🆔 `{task_id}`\n"
                    f"🕐 Em: {run_at_str}\n"
                    f"📝 {prompt}"
                )
        except Exception as exc:
            _logger.error("agendar store.add failed: %s", exc, exc_info=True)
            await ctx.send(f"⚠️ Não foi possível agendar a tarefa: {exc}")

    # ------------------------------------------------------------------
    # /agendamentos
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="agendamentos",
        description="Lista as tarefas agendadas no DEILE",
    )
    async def agendamentos(self, ctx: commands.Context) -> None:
        await ctx.defer(ephemeral=False)
        try:
            # CronStore.list_all is synchronous and returns
            # List[CronEntry], NOT a coroutine.
            tasks = await asyncio.to_thread(self.store.list_all)
        except Exception as exc:
            _logger.error("agendamentos store.list_all failed: %s", exc, exc_info=True)
            await ctx.send(f"⚠️ Não foi possível listar as tarefas: {exc}")
            return

        # SECURITY: filter by ownership unless caller is owner. Without this
        # any user could see (and act on) other users' scheduled prompts —
        # leaking private content.
        is_owner = await self._is_owner(ctx.author.id)
        author_key = f"discord:{ctx.author.id}"
        if not is_owner:
            tasks = [t for t in tasks if (getattr(t, "created_by", "") or "") == author_key]

        if not tasks:
            msg = "📭 Nenhuma tarefa agendada no momento." if is_owner else "📭 Você não tem tarefas agendadas."
            await ctx.send(msg)
            return

        lines = ["📋 **Tarefas agendadas:**"]
        for task in tasks:
            # ``task`` is a CronEntry dataclass — attribute access, not dict.
            task_id = task.id or "?"
            prompt = task.prompt or ""
            cron_expr = task.cron
            run_at = task.run_at
            status = "enabled" if task.enabled else "disabled"

            if cron_expr:
                schedule_str = f"cron: `{cron_expr}`"
            elif run_at:
                schedule_str = f"em: {run_at.isoformat()}"
            else:
                schedule_str = "sem agendamento"

            short_prompt = (prompt[:80] + "…") if len(prompt) > 80 else prompt
            lines.append(f"• `{task_id}` [{status}] {schedule_str} — {short_prompt}")

        message = "\n".join(lines)
        # Discord message limit is 2000 chars
        if len(message) > 1900:
            message = message[:1897] + "…"

        await ctx.send(message)

    # ------------------------------------------------------------------
    # /cancelar
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="cancelar",
        description="Cancela uma tarefa agendada pelo seu ID",
    )
    @app_commands.describe(id="ID da tarefa a cancelar")
    async def cancelar(self, ctx: commands.Context, *, id: str) -> None:
        await ctx.defer(ephemeral=False)
        task_id = id.strip()
        if not task_id:
            await ctx.send("❌ Informe o ID da tarefa a cancelar.")
            return

        # SECURITY: only the author of the cron OR an owner can cancel it.
        # Otherwise any user could delete another user's schedules.
        try:
            entry = await asyncio.to_thread(self.store.get, task_id)
        except Exception as exc:
            _logger.error("cancelar store.get failed: %s", exc, exc_info=True)
            await ctx.send(f"⚠️ Não foi possível verificar a tarefa: {exc}")
            return
        if entry is None:
            await ctx.send(f"❌ Tarefa `{task_id}` não encontrada.")
            return
        is_owner = await self._is_owner(ctx.author.id)
        author_key = f"discord:{ctx.author.id}"
        if not is_owner and (getattr(entry, "created_by", "") or "") != author_key:
            await ctx.send(f"❌ Você não tem permissão para cancelar a tarefa `{task_id}`.")
            return

        try:
            # CronStore exposes ``remove`` (sync), not ``delete``.
            deleted = await asyncio.to_thread(self.store.remove, task_id)
        except Exception as exc:
            _logger.error("cancelar store.remove failed: %s", exc, exc_info=True)
            await ctx.send(f"⚠️ Não foi possível cancelar a tarefa: {exc}")
            return

        if deleted:
            await ctx.send(f"✅ Tarefa `{task_id}` cancelada com sucesso.")
        else:
            await ctx.send(f"❌ Tarefa `{task_id}` não encontrada.")

    async def _is_owner(self, user_id: int) -> bool:
        """Check if the caller is configured as owner in bot settings."""
        try:
            from deilebot.foundation.settings import get_bot_settings
            settings = get_bot_settings()
            owners = set(settings.permissions.owners or [])
            return f"discord:{user_id}" in owners
        except Exception:  # noqa: BLE001
            return False
