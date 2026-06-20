"""Tests for CronCog — /agendar, /agendamentos, /cancelar slash commands."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(
    *,
    author_id: int = 1,
    author_name: str = "testuser",
    channel_id: int = 200,
    has_interaction: bool = False,
) -> MagicMock:
    from unittest.mock import AsyncMock
    ctx = MagicMock()
    ctx.defer = AsyncMock()
    ctx.send = AsyncMock()
    ctx.channel.id = channel_id
    ctx.author.id = author_id
    ctx.author.display_name = author_name
    ctx.author.name = author_name
    ctx.interaction = MagicMock() if has_interaction else None
    return ctx


def _fake_entry(
    id: str = "abc-123",
    prompt: str = "task",
    cron: str | None = None,
    run_at: datetime | None = None,
    enabled: bool = True,
    created_by: str = "discord:1",
) -> SimpleNamespace:
    """Return a CronEntry-like object (attribute access, not dict)."""
    return SimpleNamespace(
        id=id,
        prompt=prompt,
        cron=cron,
        run_at=run_at,
        enabled=enabled,
        created_by=created_by,
    )


def _make_store(
    *,
    list_return: list | None = None,
    get_return=None,
    remove_return: bool = True,
) -> MagicMock:
    """Return a mock CronStore using the synchronous API (called via asyncio.to_thread)."""
    store = MagicMock()
    store.add = MagicMock(return_value=None)
    store.list_all = MagicMock(return_value=list_return if list_return is not None else [])
    store.get = MagicMock(return_value=get_return)
    store.remove = MagicMock(return_value=remove_return)
    return store


def _make_cog(store: MagicMock) -> "CronCog":
    from deilebot.providers.discord.cogs.cron_cog import CronCog

    cog = CronCog.__new__(CronCog)
    cog.bot = None
    cog.store = store
    return cog


async def _call_agendar(cog, ctx, prompt: str, quando: str) -> None:
    await cog.agendar.callback(cog, ctx, prompt=prompt, quando=quando)


async def _call_agendamentos(cog, ctx) -> None:
    await cog.agendamentos.callback(cog, ctx)


async def _call_cancelar(cog, ctx, id: str) -> None:
    await cog.cancelar.callback(cog, ctx, id=id)


# ---------------------------------------------------------------------------
# _parse_quando unit tests
# ---------------------------------------------------------------------------

class TestParseQuando:
    def test_valid_cron_expression(self):
        from deilebot.providers.discord.cogs.cron_cog import _parse_quando

        cron, run_at = _parse_quando("*/5 * * * *")
        assert cron == "*/5 * * * *"
        assert run_at is None

    def test_valid_iso_datetime_naive(self):
        from deilebot.providers.discord.cogs.cron_cog import _parse_quando

        cron, run_at = _parse_quando("2025-06-15T10:30:00")
        assert cron is None
        assert run_at is not None
        assert run_at.year == 2025
        assert run_at.tzinfo == timezone.utc

    def test_valid_iso_datetime_with_tz(self):
        from deilebot.providers.discord.cogs.cron_cog import _parse_quando

        cron, run_at = _parse_quando("2025-06-15T10:30:00+00:00")
        assert cron is None
        assert run_at is not None

    def test_invalid_input_raises_value_error(self):
        from deilebot.providers.discord.cogs.cron_cog import _parse_quando

        with pytest.raises(ValueError):
            _parse_quando("not a cron or date")

    def test_invalid_cron_four_fields_raises(self):
        from deilebot.providers.discord.cogs.cron_cog import _parse_quando

        with pytest.raises(ValueError):
            _parse_quando("* * * *")  # only 4 fields


# ---------------------------------------------------------------------------
# /agendar — happy paths
# ---------------------------------------------------------------------------

class TestAgendar:
    async def test_agendar_with_iso_datetime_creates_task(self):
        store = _make_store()
        cog = _make_cog(store)
        ctx = _make_ctx()

        await _call_agendar(cog, ctx, "enviar relatório", "2025-08-01T09:00:00")

        store.add.assert_called_once()
        entry_arg = store.add.call_args.args[0]
        assert entry_arg.prompt == "enviar relatório"
        assert entry_arg.cron is None
        assert isinstance(entry_arg.run_at, datetime)

        sent_text = ctx.send.call_args[0][0]
        assert entry_arg.id in sent_text
        assert "✅" in sent_text

    async def test_agendar_with_cron_creates_recurring_task(self):
        store = _make_store()
        cog = _make_cog(store)
        ctx = _make_ctx()

        await _call_agendar(cog, ctx, "verificar pipeline", "0 8 * * 1")

        store.add.assert_called_once()
        entry_arg = store.add.call_args.args[0]
        assert entry_arg.cron == "0 8 * * 1"
        assert entry_arg.run_at is None

        sent_text = ctx.send.call_args[0][0]
        assert entry_arg.id in sent_text
        assert "recorrente" in sent_text.lower() or "0 8 * * 1" in sent_text

    async def test_agendar_invalid_quando_sends_error(self):
        store = _make_store()
        cog = _make_cog(store)
        ctx = _make_ctx()

        await _call_agendar(cog, ctx, "tarefa", "invalid-date-or-cron")

        ctx.send.assert_awaited_once()
        sent_text = ctx.send.call_args[0][0]
        assert "❌" in sent_text
        store.add.assert_not_called()

    async def test_agendar_store_error_sends_warning(self):
        store = _make_store()
        store.add = MagicMock(side_effect=RuntimeError("DB unavailable"))
        cog = _make_cog(store)
        ctx = _make_ctx()

        await _call_agendar(cog, ctx, "tarefa", "*/10 * * * *")

        sent_text = ctx.send.call_args[0][0]
        assert "⚠️" in sent_text


# ---------------------------------------------------------------------------
# /agendamentos — happy paths
# ---------------------------------------------------------------------------

class TestAgendamentos:
    async def test_agendamentos_empty_list(self):
        store = _make_store(list_return=[])
        cog = _make_cog(store)
        ctx = _make_ctx()

        await _call_agendamentos(cog, ctx)

        sent_text = ctx.send.call_args[0][0]
        assert "📭" in sent_text

    async def test_agendamentos_lists_tasks(self):
        tasks = [
            _fake_entry(id="t1", prompt="checar logs", cron="0 * * * *", created_by="discord:1"),
            _fake_entry(id="t2", prompt="backup db", cron=None,
                        run_at=datetime(2025, 9, 1, tzinfo=timezone.utc), created_by="discord:1"),
        ]
        store = _make_store(list_return=tasks)
        cog = _make_cog(store)
        ctx = _make_ctx(author_id=1)

        await _call_agendamentos(cog, ctx)

        sent_text = ctx.send.call_args[0][0]
        assert "t1" in sent_text
        assert "t2" in sent_text
        assert "checar logs" in sent_text

    async def test_agendamentos_store_error_sends_warning(self):
        store = _make_store()
        store.list_all = MagicMock(side_effect=RuntimeError("DB error"))
        cog = _make_cog(store)
        ctx = _make_ctx()

        await _call_agendamentos(cog, ctx)

        sent_text = ctx.send.call_args[0][0]
        assert "⚠️" in sent_text


# ---------------------------------------------------------------------------
# /cancelar — happy paths and error handling
# ---------------------------------------------------------------------------

class TestCancelar:
    async def test_cancelar_existing_task_succeeds(self):
        entry = _fake_entry(id="task-001", created_by="discord:1")
        store = _make_store(get_return=entry, remove_return=True)
        cog = _make_cog(store)
        ctx = _make_ctx(author_id=1)

        await _call_cancelar(cog, ctx, "task-001")

        store.remove.assert_called_once_with("task-001")
        sent_text = ctx.send.call_args[0][0]
        assert "✅" in sent_text
        assert "task-001" in sent_text

    async def test_cancelar_not_found_sends_not_found(self):
        store = _make_store(get_return=None)
        cog = _make_cog(store)
        ctx = _make_ctx()

        await _call_cancelar(cog, ctx, "nonexistent-id")

        sent_text = ctx.send.call_args[0][0]
        assert "❌" in sent_text
        assert "nonexistent-id" in sent_text

    async def test_cancelar_empty_id_sends_error(self):
        store = _make_store()
        cog = _make_cog(store)
        ctx = _make_ctx()

        await _call_cancelar(cog, ctx, "   ")

        sent_text = ctx.send.call_args[0][0]
        assert "❌" in sent_text
        store.remove.assert_not_called()

    async def test_cancelar_store_error_sends_warning(self):
        entry = _fake_entry(id="task-xyz", created_by="discord:1")
        store = _make_store(get_return=entry)
        store.remove = MagicMock(side_effect=RuntimeError("DB crash"))
        cog = _make_cog(store)
        ctx = _make_ctx(author_id=1)

        await _call_cancelar(cog, ctx, "task-xyz")

        sent_text = ctx.send.call_args[0][0]
        assert "⚠️" in sent_text
