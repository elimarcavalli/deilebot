"""Tests for WhatsApp adapter, normalizer, formatter, webhook routes, and media."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from deile.common.markup_ast import MarkupAST, MarkupSpan, SpanKind
from pydantic import SecretStr

from deilebot.foundation.envelope import (AttachmentKind, ChannelScope,
                                          ConversationWindow)
from deilebot.foundation.exceptions import (CapabilityNotSupported,
                                            ProviderError)
from deilebot.providers.whatsapp.adapter import WhatsAppAdapter
from deilebot.providers.whatsapp.formatter import WhatsAppFormatter
from deilebot.providers.whatsapp.media import WhatsAppMedia
from deilebot.providers.whatsapp.normalizer import WhatsAppNormalizer
from deilebot.providers.whatsapp.settings import (WHATSAPP_CAPABILITIES,
                                                  WhatsAppSettings)
from deilebot.providers.whatsapp.webhook_routes import register_routes

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _settings(verify_token: str = "secret") -> WhatsAppSettings:
    return WhatsAppSettings(
        access_token=SecretStr("tok"),
        phone_number_id="100",
        verify_token=SecretStr(verify_token),
    )


def _adapter(verify_token: str = "secret") -> WhatsAppAdapter:
    return WhatsAppAdapter(_settings(verify_token))


def _text_payload(body: str = "ola", wa_id: str = "5511999") -> dict:
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "metadata": {"phone_number_id": "100"},
                    "contacts": [{"wa_id": wa_id, "profile": {"name": "Alice"}}],
                    "messages": [{
                        "id": "wamid.XYZ",
                        "from": wa_id,
                        "type": "text",
                        "text": {"body": body},
                        "timestamp": "1730000000",
                    }],
                },
            }],
        }],
    }


# ---------------------------------------------------------------------------
# TestCapabilities
# ---------------------------------------------------------------------------

class TestCapabilities:
    def test_window_active(self):
        assert WHATSAPP_CAPABILITIES.has_conversation_window is True
        assert WHATSAPP_CAPABILITIES.can_edit_message is False


# ---------------------------------------------------------------------------
# TestFormatter
# ---------------------------------------------------------------------------

class TestFormatter:
    def test_bold_italic(self):
        f = WhatsAppFormatter()
        ast = MarkupAST([
            MarkupSpan(SpanKind.BOLD, "B"),
            MarkupSpan(SpanKind.PLAIN, " "),
            MarkupSpan(SpanKind.ITALIC, "I"),
        ])
        assert f.render(ast) == "*B* _I_"


# ---------------------------------------------------------------------------
# TestNormalizer
# ---------------------------------------------------------------------------

class TestNormalizer:
    def test_text_message(self):
        env = WhatsAppNormalizer().to_envelope(_text_payload())
        assert env is not None
        assert env.text == "ola"
        assert env.channel.scope == ChannelScope.DM
        assert env.author.display_name == "Alice"

    def test_image_message(self):
        payload = {
            "entry": [{
                "changes": [{
                    "value": {
                        "metadata": {"phone_number_id": "100"},
                        "contacts": [],
                        "messages": [{
                            "id": "wamid.IMG",
                            "from": "5511999",
                            "type": "image",
                            "image": {"id": "img123", "mime_type": "image/jpeg"},
                            "timestamp": "1730000001",
                        }],
                    },
                }],
            }],
        }
        env = WhatsAppNormalizer().to_envelope(payload)
        assert env is not None
        assert env.text == ""
        assert len(env.attachments) == 1
        assert env.attachments[0].kind == AttachmentKind.IMAGE
        assert env.attachments[0].mime == "image/jpeg"

    def test_interactive_list_reply(self):
        """Interactive list_reply is normalised to text (button title is ignored — body absent)."""
        payload = {
            "entry": [{
                "changes": [{
                    "value": {
                        "metadata": {"phone_number_id": "100"},
                        "contacts": [],
                        "messages": [{
                            "id": "wamid.INT",
                            "from": "5511999",
                            "type": "interactive",
                            "interactive": {
                                "type": "list_reply",
                                "list_reply": {"id": "row1", "title": "Option A"},
                            },
                            "timestamp": "1730000002",
                        }],
                    },
                }],
            }],
        }
        env = WhatsAppNormalizer().to_envelope(payload)
        assert env is not None
        assert env.attachments == ()

    def test_empty_payload_returns_none(self):
        assert WhatsAppNormalizer().to_envelope({}) is None

    def test_malformed_payload_returns_none(self):
        assert WhatsAppNormalizer().to_envelope({"entry": [{"changes": [{"value": {}}]}]}) is None


# ---------------------------------------------------------------------------
# TestAdapter
# ---------------------------------------------------------------------------

class TestAdapter:
    def test_caps(self):
        a = _adapter()
        assert a.name == "whatsapp"
        assert not a.capabilities.can_edit_message

    async def test_start_no_token_raises(self):
        a = WhatsAppAdapter(WhatsAppSettings())
        with pytest.raises(ProviderError):
            await a.start()

    async def test_edit_unsupported(self):
        from deilebot._testing import make_channel
        a = _adapter()
        with pytest.raises(CapabilityNotSupported):
            await a.edit_message(make_channel(), "x", "new")

    async def test_handle_webhook_dispatches_to_callback(self):
        received = []

        async def on_inbound(env, src):
            received.append(env)

        a = _adapter()
        a.on_inbound = on_inbound
        result = await a.handle_webhook(_text_payload())
        assert result["status"] == "ok"
        assert len(received) == 1
        assert received[0].text == "ola"

    async def test_handle_webhook_no_callback_ignored(self):
        a = _adapter()
        result = await a.handle_webhook(_text_payload())
        assert result["status"] == "ignored"


# ---------------------------------------------------------------------------
# TestConversationWindow
# ---------------------------------------------------------------------------

class TestConversationWindow:
    def test_window_open(self):
        win = ConversationWindow(
            last_inbound_at=datetime.now(timezone.utc) - timedelta(hours=1),
            window_hours=24,
        )
        assert win.is_open

    def test_window_closed(self):
        win = ConversationWindow(
            last_inbound_at=datetime.now(timezone.utc) - timedelta(hours=25),
            window_hours=24,
        )
        assert not win.is_open


# ---------------------------------------------------------------------------
# TestWebhookRoutes
# ---------------------------------------------------------------------------

class TestWebhookRoutes:
    def _make_app(self, verify_token: str = "secret") -> web.Application:
        app = web.Application()
        register_routes(app, _adapter(verify_token))
        return app

    async def test_get_verify_ok(self, aiohttp_client):
        client = await aiohttp_client(self._make_app())
        resp = await client.get(
            "/webhook/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "secret",
                "hub.challenge": "my_challenge_123",
            },
        )
        assert resp.status == 200
        body = await resp.text()
        assert body == "my_challenge_123"

    async def test_get_verify_wrong_token(self, aiohttp_client):
        client = await aiohttp_client(self._make_app())
        resp = await client.get(
            "/webhook/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong",
                "hub.challenge": "x",
            },
        )
        assert resp.status == 403

    async def test_get_verify_bad_mode(self, aiohttp_client):
        client = await aiohttp_client(self._make_app())
        resp = await client.get(
            "/webhook/whatsapp",
            params={
                "hub.mode": "unsubscribe",
                "hub.verify_token": "secret",
                "hub.challenge": "x",
            },
        )
        assert resp.status == 400

    async def test_post_dispatch_ok(self, aiohttp_client):
        received = []

        adapter = _adapter()
        async def on_inbound(env, src):
            received.append(env)
        adapter.on_inbound = on_inbound

        app = web.Application()
        register_routes(app, adapter)
        client = await aiohttp_client(app)

        resp = await client.post("/webhook/whatsapp", json=_text_payload())
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert len(received) == 1

    async def test_post_invalid_json(self, aiohttp_client):
        client = await aiohttp_client(self._make_app())
        resp = await client.post(
            "/webhook/whatsapp",
            data=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400

    async def test_custom_path(self, aiohttp_client):
        app = web.Application()
        register_routes(app, _adapter(), path="/custom/hook")
        client = await aiohttp_client(app)
        resp = await client.get(
            "/custom/hook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "secret",
                "hub.challenge": "abc",
            },
        )
        assert resp.status == 200


# ---------------------------------------------------------------------------
# TestWhatsAppMedia
# ---------------------------------------------------------------------------

class TestWhatsAppMedia:
    def _mock_client(self):
        c = MagicMock()
        c.upload_media = AsyncMock(return_value="media-id-123")
        c.get_media_url = AsyncMock(return_value="https://example.com/media/abc")
        c.download_media_bytes = AsyncMock(return_value=b"\xff\xd8\xff")
        return c

    async def test_upload_returns_media_id(self):
        media = WhatsAppMedia(self._mock_client())
        mid = await media.upload(b"data", "image/jpeg")
        assert mid == "media-id-123"

    async def test_get_url_returns_url(self):
        media = WhatsAppMedia(self._mock_client())
        url = await media.get_url("media-id-123")
        assert url.startswith("https://")

    async def test_download_returns_bytes(self):
        media = WhatsAppMedia(self._mock_client())
        data = await media.download("https://example.com/media/abc")
        assert data == b"\xff\xd8\xff"
