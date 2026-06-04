"""Tests for MonitorCog — /monitor (owner-only) bridge to deile-monitor.

O MonitorClient é injetado via monkeypatch de ``cog._client`` para que
NENHUM HTTP real aconteça. O gate de owner é exercido construindo um
``adapter.runtime`` cuja cadeia de permissões devolve o veredito desejado.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.defer = AsyncMock()
    ctx.send = AsyncMock()
    ctx.author.id = 42
    ctx.author.display_name = "alice"
    ctx.author.name = "alice"
    return ctx


def _make_adapter(*, is_owner: bool) -> MagicMock:
    """Adapter cujo runtime.pipeline.permissions.is_owner devolve ``is_owner``."""
    permissions = MagicMock()
    permissions.is_owner = AsyncMock(return_value=is_owner)
    identity = MagicMock()
    identity.resolve = AsyncMock(return_value=SimpleNamespace(bot_user_id="bu-1"))
    pipeline = MagicMock()
    pipeline.permissions = permissions
    pipeline.identity = identity
    runtime = MagicMock()
    runtime.pipeline = pipeline
    adapter = MagicMock()
    adapter.runtime = runtime
    return adapter


class _FakeMonitorClientError(Exception):
    """Dublê de MonitorClientError — desacopla o teste do módulo real do
    repo deile (que mergeia primeiro, ver spec §8; pode não estar instalado
    no ambiente de CI do bot)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _make_cog(*, is_owner: bool, fake_client):
    from deilebot.providers.discord.cogs.monitor_cog import MonitorCog

    cog = MonitorCog.__new__(MonitorCog)
    cog.bot = None
    cog.adapter = _make_adapter(is_owner=is_owner)
    cog.runtime = cog.adapter.runtime
    # Injeta (factory, error_type) sem HTTP nem o módulo real do deile.
    cog._load_monitor = lambda: (lambda: fake_client, _FakeMonitorClientError)
    return cog


class _FakeClient:
    """MonitorClient fake — sem rede. Configurável por teste."""

    def __init__(self, *, status=None, command_result=None, ask_sequence=None):
        self._status = status or {}
        self._command_result = command_result or {}
        # ask_sequence: lista de respostas que get_ask_result devolve em ordem.
        self._ask_sequence = list(ask_sequence or [])
        self.commands_posted = []
        self.questions_asked = []

    async def get_status(self):
        return self._status

    async def post_command(self, command):
        self.commands_posted.append(command)
        return self._command_result

    async def ask(self, question):
        self.questions_asked.append(question)
        return "req-1"

    async def get_ask_result(self, request_id):
        if self._ask_sequence:
            return self._ask_sequence.pop(0)
        return {"status": "done", "answer": "(default)"}


# ---------------------------------------------------------------------------
# Owner gate
# ---------------------------------------------------------------------------


async def test_status_non_owner_blocked():
    fake = _FakeClient(status={"last_tick": "x"})
    cog = _make_cog(is_owner=False, fake_client=fake)
    ctx = _make_ctx()
    await cog.status.callback(cog, ctx)
    ctx.send.assert_awaited_once()
    assert "owner only" in ctx.send.await_args.args[0]


async def test_pause_non_owner_blocked():
    fake = _FakeClient(command_result={"command": "pause 30m", "effect": "ok"})
    cog = _make_cog(is_owner=False, fake_client=fake)
    ctx = _make_ctx()
    await cog.pause.callback(cog, ctx, duration="30m")
    assert "owner only" in ctx.send.await_args.args[0]
    assert fake.commands_posted == []  # nunca chamou o monitor


async def test_ask_non_owner_blocked():
    fake = _FakeClient()
    cog = _make_cog(is_owner=False, fake_client=fake)
    ctx = _make_ctx()
    await cog.ask.callback(cog, ctx, question="como tá?")
    assert "owner only" in ctx.send.await_args.args[0]
    assert fake.questions_asked == []


# ---------------------------------------------------------------------------
# status → embed
# ---------------------------------------------------------------------------


async def test_status_renders_embed_from_fake_client():
    fake = _FakeClient(status={
        "last_tick": "2026-06-04T12:00:00Z",
        "age_s": 120,
        "paused": False,
        "anomalies_total": 2,
        "notifications_this_hour": 1,
        "known_anomalies": [
            {"fingerprint": "abc123def456", "severity": "P1", "type": "oauth_expired", "count": 3},
            {"fingerprint": "zzz999", "severity": "P2", "type": "pod_crash", "count": 1},
        ],
    })
    cog = _make_cog(is_owner=True, fake_client=fake)
    ctx = _make_ctx()
    await cog.status.callback(cog, ctx)
    ctx.send.assert_awaited_once()
    embed = ctx.send.await_args.kwargs["embed"]
    import discord

    assert isinstance(embed, discord.Embed)
    assert embed.color.value == 0xED4245  # P1 → vermelho
    field_text = " ".join(f.name + " " + str(f.value) for f in embed.fields)
    assert "oauth_expired" in field_text
    assert "abc123def456" in field_text


async def test_status_client_error_replies_clearly():
    fake = _FakeClient()

    async def _boom():
        raise _FakeMonitorClientError("MONITOR_UNREACHABLE", "no route to host")

    fake.get_status = _boom  # type: ignore[method-assign]
    cog = _make_cog(is_owner=True, fake_client=fake)
    ctx = _make_ctx()
    await cog.status.callback(cog, ctx)
    msg = ctx.send.await_args.args[0]
    assert "MONITOR_UNREACHABLE" in msg


# ---------------------------------------------------------------------------
# orders (pause/resume/ack/force_tick) → post_command + effect
# ---------------------------------------------------------------------------


async def test_pause_owner_posts_command_and_confirms():
    fake = _FakeClient(command_result={"command": "pause 30m", "effect": "pausado por 30m"})
    cog = _make_cog(is_owner=True, fake_client=fake)
    ctx = _make_ctx()
    await cog.pause.callback(cog, ctx, duration="30m")
    assert fake.commands_posted == ["pause 30m"]
    msg = ctx.send.await_args.args[0]
    assert "pausado por 30m" in msg


async def test_resume_owner_posts_resume():
    fake = _FakeClient(command_result={"command": "resume", "effect": "retomado"})
    cog = _make_cog(is_owner=True, fake_client=fake)
    ctx = _make_ctx()
    await cog.resume.callback(cog, ctx)
    assert fake.commands_posted == ["resume"]


async def test_force_tick_owner_posts_force_tick():
    fake = _FakeClient(command_result={"command": "force-tick", "effect": "tick disparado"})
    cog = _make_cog(is_owner=True, fake_client=fake)
    ctx = _make_ctx()
    await cog.force_tick.callback(cog, ctx)
    assert fake.commands_posted == ["force-tick"]


async def test_ack_owner_posts_ack_with_fingerprint():
    fake = _FakeClient(command_result={"command": "ack abc", "effect": "silenciado"})
    cog = _make_cog(is_owner=True, fake_client=fake)
    ctx = _make_ctx()
    await cog.ack.callback(cog, ctx, fingerprint="abc")
    assert fake.commands_posted == ["ack abc"]


# ---------------------------------------------------------------------------
# ask → defer + poll + reply
# ---------------------------------------------------------------------------


async def test_ask_defers_polls_and_replies(monkeypatch):
    """running → done: o cog faz defer, pollia até done e responde a answer."""
    # Evita o sleep real de 2s no loop de poll.
    import deilebot.providers.discord.cogs.monitor_cog as mc

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(mc.asyncio, "sleep", _no_sleep)

    fake = _FakeClient(ask_sequence=[
        {"status": "running"},
        {"status": "done", "answer": "o pipeline está saudável: 3 issues na fila"},
    ])
    cog = _make_cog(is_owner=True, fake_client=fake)
    ctx = _make_ctx()
    await cog.ask.callback(cog, ctx, question="como tá o pipeline?")
    ctx.defer.assert_awaited_once()
    assert fake.questions_asked == ["como tá o pipeline?"]
    msg = ctx.send.await_args.args[0]
    assert "pipeline está saudável" in msg


async def test_ask_error_status_replies_with_error(monkeypatch):
    import deilebot.providers.discord.cogs.monitor_cog as mc

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(mc.asyncio, "sleep", _no_sleep)

    fake = _FakeClient(ask_sequence=[{"status": "error", "error": "kubectl timeout"}])
    cog = _make_cog(is_owner=True, fake_client=fake)
    ctx = _make_ctx()
    await cog.ask.callback(cog, ctx, question="x")
    ctx.defer.assert_awaited_once()
    msg = ctx.send.await_args.args[0]
    assert "kubectl timeout" in msg


# ---------------------------------------------------------------------------
# Wiring — MonitorCog é registrado no adapter
# ---------------------------------------------------------------------------


def test_monitor_cog_is_wired():
    import inspect

    from deilebot.providers.discord.adapter import DiscordAdapter

    src = inspect.getsource(DiscordAdapter.start)
    assert "MonitorCog" in src
    assert "add_cog(MonitorCog(" in src
