"""End-to-end checks for the deilebot control-plane server.

We boot the *real* `ControlPlaneServer` against a fake provider adapter
and exercise it through the *real* `BotControlClient`. The fake adapter
only stubs the few methods the routes call.
"""

from __future__ import annotations

import pytest
from deilebot_client import (BotClientAuthError, BotControlClient,
                              BotControlSettings)

from deilebot.foundation.capabilities import ProviderCapabilities
from deilebot.foundation.envelope import AttachmentKind
from deilebot.runtime.control_plane import (ControlPlaneServer,
                                             ControlPlaneSettings)


class FakeAdapter:
    name = "discord"
    capabilities = ProviderCapabilities(
        can_edit_message=True, can_react=True, can_send_dm=True,
        can_threads=True, can_polls=False, can_inline_keyboards=False,
        can_slash_commands=True, can_voice_messages=False,
        can_send_typing=True, can_fetch_user_profile=True,
        has_conversation_window=True,
        max_message_chars=2000, max_attachments_per_message=10,
        supported_attachment_kinds=frozenset({AttachmentKind.IMAGE, AttachmentKind.FILE}),
    )
    _client = object()

    def __init__(self):
        self.calls = []

    async def send_message(self, channel, text, reply_to=None, attachments=()):
        self.calls.append(("send_message", channel.provider_channel_id, text))
        return "msg-99"

    async def react(self, channel, message_id, emoji):
        self.calls.append(("react", channel.provider_channel_id, message_id, emoji))

    async def send_dm(self, user, text, attachments=()):
        self.calls.append(("send_dm", user.provider_user_id, text))
        return "dm-99"


@pytest.fixture
async def daemon():
    settings = ControlPlaneSettings(host="127.0.0.1", port=0, auth_token="cp-test")
    srv = ControlPlaneServer(settings, version="cp-test")
    adapter = FakeAdapter()
    srv.register_adapter("discord", adapter)
    port = await srv.start()
    yield srv, adapter, port
    await srv.stop()


async def test_health_no_auth_required(daemon):
    srv, _adapter, port = daemon
    # No token at all — health is the one public endpoint
    settings = BotControlSettings(endpoint=f"http://127.0.0.1:{port}", auth_token="")
    async with BotControlClient(settings) as cli:
        h = await cli.health()
        assert h.ok is True
        assert h.providers == ["discord"]


async def test_protected_endpoint_requires_token(daemon):
    _, _, port = daemon
    settings = BotControlSettings(endpoint=f"http://127.0.0.1:{port}", auth_token="")
    async with BotControlClient(settings) as cli:
        with pytest.raises(BotClientAuthError):
            await cli.discord_channel_post(channel_id="1", text="x")


async def test_round_trip_post_react_dm(daemon):
    _, adapter, port = daemon
    settings = BotControlSettings(endpoint=f"http://127.0.0.1:{port}", auth_token="cp-test")
    async with BotControlClient(settings) as cli:
        post = await cli.discord_channel_post(channel_id="1", text="hi")
        assert post.message_id == "msg-99"
        await cli.discord_reaction_add(channel_id="1", message_id="msg-99", emoji="🚀")
        dm = await cli.discord_dm_send(user_id="42", text="psst")
        assert dm.message_id == "dm-99"
    assert ("send_message", "1", "hi") in adapter.calls
    assert ("react", "1", "msg-99", "🚀") in adapter.calls
    assert ("send_dm", "42", "psst") in adapter.calls


async def test_dm_validates_payload(daemon):
    _, _adapter, port = daemon
    settings = BotControlSettings(endpoint=f"http://127.0.0.1:{port}", auth_token="cp-test")
    async with BotControlClient(settings) as cli:
        # Local pydantic validation — never reaches the network
        with pytest.raises(Exception):
            await cli.discord_dm_send(user_id="42", bot_user_id="ulid", text="hi")


# ---------------------------------------------------------------------------
# WhatsApp send_template endpoint (Phase 2)
# ---------------------------------------------------------------------------


class FakeWhatsAppAdapter:
    name = "whatsapp"
    capabilities = ProviderCapabilities(
        can_edit_message=False, can_react=True, can_send_dm=True,
        can_threads=False, can_polls=False, can_inline_keyboards=True,
        can_slash_commands=False, can_voice_messages=False,
        can_send_typing=False, can_fetch_user_profile=False,
        has_conversation_window=True,
        max_message_chars=4096, max_attachments_per_message=1,
        supported_attachment_kinds=frozenset({AttachmentKind.IMAGE}),
    )
    _client = object()

    def __init__(self):
        self.template_calls = []

    async def send_template(self, user, template):
        self.template_calls.append({
            "to": user.provider_user_id,
            "name": template.name,
            "language": template.language,
            "body_params": template.body_params,
        })
        return f"wamid.tpl-{len(self.template_calls)}"


@pytest.fixture
async def wa_daemon():
    settings = ControlPlaneSettings(host="127.0.0.1", port=0, auth_token="cp-test")
    srv = ControlPlaneServer(settings, version="cp-test")
    adapter = FakeWhatsAppAdapter()
    srv.register_adapter("whatsapp", adapter)
    # Wire metrics so the route handler emits the cost counter.
    from deilebot.foundation.metrics import MetricsCollector
    srv.metrics = MetricsCollector()
    port = await srv.start()
    yield srv, adapter, port
    await srv.stop()


async def test_whatsapp_send_template_round_trip(wa_daemon):
    srv, adapter, port = wa_daemon
    settings = BotControlSettings(endpoint=f"http://127.0.0.1:{port}", auth_token="cp-test")
    async with BotControlClient(settings) as cli:
        resp = await cli.whatsapp_send_template(
            to="5511999",
            template_name="appointment_reminder",
            language="pt_BR",
            body_params=["Maria", "10:00"],
            category="utility",
        )
        assert resp.message_id.startswith("wamid.tpl-")
        assert resp.to == "5511999"
        assert resp.template_name == "appointment_reminder"
    assert len(adapter.template_calls) == 1
    assert adapter.template_calls[0]["body_params"] == ("Maria", "10:00")
    # Metric incremented by the route handler
    snap = srv.metrics.snapshot()
    wa_series = snap["counters"].get("bot_whatsapp_conversations_total", [])
    assert wa_series
    assert any(s["labels"].get("category") == "utility" for s in wa_series)


async def test_whatsapp_send_template_no_adapter_returns_503(wa_daemon):
    srv, _adapter, port = wa_daemon
    # Drop the adapter to simulate not-yet-ready
    srv.adapters.pop("whatsapp", None)
    settings = BotControlSettings(endpoint=f"http://127.0.0.1:{port}", auth_token="cp-test")
    async with BotControlClient(settings) as cli:
        with pytest.raises(Exception):  # NotReady from client envelope mapping
            await cli.whatsapp_send_template(
                to="5511999", template_name="x", language="en_US",
            )


async def test_whatsapp_send_template_failure_emits_fail_metric(wa_daemon):
    """Adapter failure → status=fail metric so the operator sees attempts that failed."""
    from deilebot.foundation.exceptions import ProviderError

    srv, _, port = wa_daemon

    class FailingAdapter(FakeWhatsAppAdapter):
        async def send_template(self, user, template):
            raise ProviderError("upstream 132001: template not found", context={})

    srv.adapters["whatsapp"] = FailingAdapter()
    settings = BotControlSettings(endpoint=f"http://127.0.0.1:{port}", auth_token="cp-test")
    async with BotControlClient(settings) as cli:
        with pytest.raises(Exception):
            await cli.whatsapp_send_template(
                to="5511999",
                template_name="ghost",
                language="pt_BR",
                category="marketing",
            )
    snap = srv.metrics.snapshot()
    wa_series = snap["counters"].get("bot_whatsapp_conversations_total", [])
    assert any(
        s["labels"].get("status") == "fail"
        and s["labels"].get("category") == "marketing"
        for s in wa_series
    ), f"expected fail/marketing entry, got {wa_series}"
