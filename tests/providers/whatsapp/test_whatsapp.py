"""Tests for WhatsApp adapter, normalizer, formatter, webhook routes, and media."""

from __future__ import annotations

import hashlib
import hmac
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

def _settings(verify_token: str = "secret", app_secret: str | None = None) -> WhatsAppSettings:
    return WhatsAppSettings(
        access_token=SecretStr("tok"),
        phone_number_id="100",
        verify_token=SecretStr(verify_token),
        app_secret=SecretStr(app_secret) if app_secret else None,
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
        """Interactive list_reply populates text with the row title and stashes id in raw."""
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
        assert env.text == "Option A"
        assert env.raw["interactive"]["kind"] == "list_reply"
        assert env.raw["interactive"]["id"] == "row1"

    def test_interactive_button_reply(self):
        """Reply Button selection populates text with title + raw['interactive']."""
        payload = {
            "entry": [{
                "changes": [{
                    "value": {
                        "metadata": {"phone_number_id": "100"},
                        "contacts": [],
                        "messages": [{
                            "id": "wamid.BTN",
                            "from": "5511999",
                            "type": "interactive",
                            "interactive": {
                                "type": "button_reply",
                                "button_reply": {"id": "yes", "title": "Sim"},
                            },
                            "timestamp": "1730000003",
                        }],
                    },
                }],
            }],
        }
        env = WhatsAppNormalizer().to_envelope(payload)
        assert env is not None
        assert env.text == "Sim"
        assert env.raw["interactive"]["kind"] == "button_reply"
        assert env.raw["interactive"]["id"] == "yes"

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

    async def test_get_verify_ok(self):
        async with TestClient(TestServer(self._make_app())) as client:
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

    async def test_get_verify_wrong_token(self):
        async with TestClient(TestServer(self._make_app())) as client:
            resp = await client.get(
                "/webhook/whatsapp",
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "wrong",
                    "hub.challenge": "x",
                },
            )
            assert resp.status == 403

    async def test_get_verify_bad_mode(self):
        async with TestClient(TestServer(self._make_app())) as client:
            resp = await client.get(
                "/webhook/whatsapp",
                params={
                    "hub.mode": "unsubscribe",
                    "hub.verify_token": "secret",
                    "hub.challenge": "x",
                },
            )
            assert resp.status == 400

    async def test_post_dispatch_ok(self):
        received = []

        adapter = _adapter()
        async def on_inbound(env, src):
            received.append(env)
        adapter.on_inbound = on_inbound

        app = web.Application()
        register_routes(app, adapter)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/webhook/whatsapp", json=_text_payload())
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"
        assert len(received) == 1

    async def test_post_invalid_json(self):
        async with TestClient(TestServer(self._make_app())) as client:
            resp = await client.post(
                "/webhook/whatsapp",
                data=b"not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    async def test_custom_path(self):
        app = web.Application()
        register_routes(app, _adapter(), path="/custom/hook")
        async with TestClient(TestServer(app)) as client:
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


# ---------------------------------------------------------------------------
# TestApiClientValidation
# ---------------------------------------------------------------------------

class TestApiClientValidation:
    """Validate that api_client raises ProviderError on missing response fields."""

    async def test_upload_missing_id_raises(self):
        from unittest.mock import patch

        import httpx

        from deilebot.providers.whatsapp.api_client import WhatsAppApiClient

        client = WhatsAppApiClient("tok", "100")
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={})  # missing "id"

        with patch.object(client, "_get_client", AsyncMock(return_value=MagicMock(
            post=AsyncMock(return_value=mock_resp)
        ))):
            with pytest.raises(ProviderError, match="missing 'id'"):
                await client.upload_media(b"data", "image/jpeg")

    async def test_get_media_url_missing_url_raises(self):
        from unittest.mock import patch

        from deilebot.providers.whatsapp.api_client import WhatsAppApiClient

        client = WhatsAppApiClient("tok", "100")
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={})  # missing "url"

        with patch.object(client, "_get_client", AsyncMock(return_value=MagicMock(
            get=AsyncMock(return_value=mock_resp)
        ))):
            with pytest.raises(ProviderError, match="missing 'url'"):
                await client.get_media_url("media-id-123")

    async def test_context_manager_closes_client(self):
        from deilebot.providers.whatsapp.api_client import WhatsAppApiClient

        client = WhatsAppApiClient("tok", "100")
        async with client:
            pass
        assert client._client is None

    async def test_post_missing_message_id_raises(self):
        from unittest.mock import patch

        from deilebot.providers.whatsapp.api_client import WhatsAppApiClient

        client = WhatsAppApiClient("tok", "100")
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"messages": [{}]})  # id absent

        with patch.object(client, "_get_client", AsyncMock(return_value=MagicMock(
            post=AsyncMock(return_value=mock_resp)
        ))):
            with pytest.raises(ProviderError, match="missing message 'id'"):
                await client.send_text("5511999", "hello")

    async def test_download_media_bytes_error_raises(self):
        from unittest.mock import patch

        import httpx

        from deilebot.providers.whatsapp.api_client import WhatsAppApiClient

        client = WhatsAppApiClient("tok", "100")
        mock_http = MagicMock()
        mock_http.get = AsyncMock(side_effect=Exception("connection error"))

        with patch.object(client, "_get_client", AsyncMock(return_value=mock_http)):
            with pytest.raises(ProviderError, match="download_media_bytes failed"):
                await client.download_media_bytes("https://example.com/expired")


# ---------------------------------------------------------------------------
# TestWebhookSignature
# ---------------------------------------------------------------------------

class TestWebhookSignature:
    def _make_sig(self, body: bytes, secret: str) -> str:
        mac = hmac.new(key=secret.encode(), msg=body, digestmod=hashlib.sha256).hexdigest()
        return f"sha256={mac}"

    def _make_app_with_secret(self, secret: str = "appsecret") -> web.Application:
        from deilebot.providers.whatsapp.adapter import WhatsAppAdapter
        app = web.Application()
        adapter = WhatsAppAdapter(_settings(app_secret=secret))
        register_routes(app, adapter)
        return app

    async def test_valid_signature_passes(self):
        body = b'{"object":"whatsapp_business_account","entry":[]}'
        sig = self._make_sig(body, "appsecret")
        app = self._make_app_with_secret()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/webhook/whatsapp",
                data=body,
                headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
            )
            assert resp.status == 200

    async def test_invalid_signature_rejected(self):
        body = b'{"object":"whatsapp_business_account","entry":[]}'
        app = self._make_app_with_secret()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/webhook/whatsapp",
                data=body,
                headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=deadbeef"},
            )
            assert resp.status == 403

    async def test_missing_signature_header_rejected(self):
        body = b'{"object":"whatsapp_business_account","entry":[]}'
        app = self._make_app_with_secret()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/webhook/whatsapp",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 403

    async def test_no_app_secret_skips_check(self):
        """Without app_secret configured, signature check is skipped (opt-in)."""
        from deilebot.providers.whatsapp.adapter import WhatsAppAdapter
        app = web.Application()
        register_routes(app, WhatsAppAdapter(_settings()))  # no app_secret
        body = b'{"entry":[]}'
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/webhook/whatsapp",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 200

    async def test_empty_verify_token_rejected(self):
        """Empty verify_token must not pass the handshake."""
        from deilebot.providers.whatsapp.adapter import WhatsAppAdapter
        app = web.Application()
        register_routes(app, WhatsAppAdapter(_settings(verify_token="")))
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/webhook/whatsapp",
                params={"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "x"},
            )
            assert resp.status == 403


# ---------------------------------------------------------------------------
# TestTemplateCatalog
# ---------------------------------------------------------------------------

from deilebot.foundation.envelope import TemplateMessage  # noqa: E402
from deilebot.foundation.interactive import (  # noqa: E402
    InteractiveButton, InteractiveButtonRow, InteractiveList,
    InteractiveListSection)
from deilebot.providers.whatsapp.template_catalog import (  # noqa: E402
    WhatsAppTemplate, WhatsAppTemplateCatalog)


class TestTemplateCatalog:
    def test_empty_catalog(self):
        c = WhatsAppTemplateCatalog.empty()
        assert len(c) == 0
        assert not c.has("x", "pt_BR")
        with pytest.raises(KeyError):
            c.get("x", "pt_BR")

    def test_from_yaml_missing_file(self, tmp_path):
        c = WhatsAppTemplateCatalog.from_yaml(tmp_path / "nope.yaml")
        assert len(c) == 0

    def test_from_mapping(self):
        raw = {
            "templates": [
                {
                    "name": "appointment_reminder",
                    "language": "pt_BR",
                    "category": "utility",
                    "body_params": [{"name": "patient"}, {"name": "time"}],
                    "header_params": [],
                },
            ]
        }
        c = WhatsAppTemplateCatalog.from_mapping(raw)
        assert len(c) == 1
        tpl = c.get("appointment_reminder", "pt_BR")
        assert tpl.category == "utility"
        assert len(tpl.body_params) == 2

    def test_from_yaml_loads_real_file(self, tmp_path):
        path = tmp_path / "tpl.yaml"
        path.write_text(
            "templates:\n"
            "  - name: hello\n"
            "    language: en_US\n"
            "    category: utility\n"
        )
        c = WhatsAppTemplateCatalog.from_yaml(path)
        assert c.has("hello", "en_US")

    def test_invalid_category_rejected(self):
        with pytest.raises(Exception):  # Pydantic ValidationError or ValueError
            WhatsAppTemplateCatalog.from_mapping({
                "templates": [{
                    "name": "x", "language": "en_US", "category": "spam",
                }]
            })

    def test_duplicate_name_language_rejected(self):
        with pytest.raises(ValueError):
            WhatsAppTemplateCatalog.from_mapping({
                "templates": [
                    {"name": "x", "language": "en_US", "category": "utility"},
                    {"name": "x", "language": "en_US", "category": "marketing"},
                ]
            })

    def test_same_name_different_languages(self):
        c = WhatsAppTemplateCatalog.from_mapping({
            "templates": [
                {"name": "x", "language": "en_US", "category": "utility"},
                {"name": "x", "language": "pt_BR", "category": "utility"},
            ]
        })
        assert c.has("x", "en_US")
        assert c.has("x", "pt_BR")

    def test_build_components_zero_params(self):
        tpl = WhatsAppTemplate(
            name="hi", language="en_US", category="utility",
        )
        assert tpl.build_components() == []

    def test_build_components_body_only(self):
        tpl = WhatsAppTemplate(
            name="hi", language="en_US", category="utility",
            body_params=({"name": "user"}, {"name": "code"}),
        )
        components = tpl.build_components(body_params=["alice", "12345"])
        assert components == [{
            "type": "body",
            "parameters": [
                {"type": "text", "text": "alice"},
                {"type": "text", "text": "12345"},
            ],
        }]

    def test_build_components_param_count_mismatch(self):
        tpl = WhatsAppTemplate(
            name="hi", language="en_US", category="utility",
            body_params=({"name": "user"},),
        )
        with pytest.raises(ValueError, match="expects 1 body params"):
            tpl.build_components(body_params=["a", "b"])


# ---------------------------------------------------------------------------
# TestAdapterTemplate (Phase 2)
# ---------------------------------------------------------------------------

class TestAdapterTemplate:
    """adapter.send_template — TemplateMessage shape with catalog validation."""

    def _adapter_with_catalog(self, catalog: WhatsAppTemplateCatalog) -> WhatsAppAdapter:
        return WhatsAppAdapter(_settings(), template_catalog=catalog)

    async def test_send_template_no_client_raises(self):
        a = self._adapter_with_catalog(WhatsAppTemplateCatalog.empty())
        u = MagicMock()
        u.provider_user_id = "5511999"
        with pytest.raises(ProviderError, match="not started"):
            await a.send_template(u, TemplateMessage(name="x", language="en_US"))

    async def test_send_template_uses_catalog(self):
        catalog = WhatsAppTemplateCatalog.from_mapping({
            "templates": [{
                "name": "appt", "language": "pt_BR", "category": "utility",
                "body_params": [{"name": "n"}, {"name": "t"}],
            }]
        })
        a = self._adapter_with_catalog(catalog)
        # Stub api_client without going through start()
        a._client = MagicMock()
        a._client.send_template = AsyncMock(return_value="wamid.OK")
        u = MagicMock()
        u.provider_user_id = "5511999"
        msg_id = await a.send_template(
            u,
            TemplateMessage(name="appt", language="pt_BR", body_params=("Maria", "10:00")),
        )
        assert msg_id == "wamid.OK"
        call_kwargs = a._client.send_template.call_args
        # api_client.send_template signature: (to, name, language, components=...)
        assert call_kwargs.args[0] == "5511999"
        assert call_kwargs.args[1] == "appt"
        assert call_kwargs.args[2] == "pt_BR"
        components = call_kwargs.kwargs["components"]
        assert components[0]["type"] == "body"
        assert components[0]["parameters"][0]["text"] == "Maria"

    async def test_send_template_unknown_template_no_components(self):
        """Templates not in the catalog are sent with empty components.

        The Cloud API will respond with 132001 (template not found) — that's
        the operator's signal to register the template with Meta. The
        adapter does not pretend the template exists locally.
        """
        a = self._adapter_with_catalog(WhatsAppTemplateCatalog.empty())
        a._client = MagicMock()
        a._client.send_template = AsyncMock(return_value="wamid.OK")
        u = MagicMock()
        u.provider_user_id = "5511999"
        await a.send_template(
            u, TemplateMessage(name="ghost", language="en_US")
        )
        assert a._client.send_template.call_args.kwargs["components"] == []

    async def test_send_template_param_mismatch_raises(self):
        catalog = WhatsAppTemplateCatalog.from_mapping({
            "templates": [{
                "name": "appt", "language": "pt_BR", "category": "utility",
                "body_params": [{"name": "n"}],
            }]
        })
        a = self._adapter_with_catalog(catalog)
        a._client = MagicMock()
        a._client.send_template = AsyncMock(return_value="wamid.OK")
        u = MagicMock()
        u.provider_user_id = "5511999"
        with pytest.raises(ProviderError, match="parameter mismatch"):
            await a.send_template(
                u,
                TemplateMessage(
                    name="appt", language="pt_BR", body_params=("a", "b"),
                ),
            )

    async def test_send_template_rejects_non_template(self):
        a = self._adapter_with_catalog(WhatsAppTemplateCatalog.empty())
        a._client = MagicMock()
        u = MagicMock()
        with pytest.raises(ProviderError, match="TemplateMessage"):
            await a.send_template(u, "not-a-template")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TestFormatterInteractive (Phase 2)
# ---------------------------------------------------------------------------

class TestFormatterInteractive:
    """WhatsAppFormatter.render_interactive — Cloud API payload shape."""

    def test_button_row_basic(self):
        f = WhatsAppFormatter()
        row = InteractiveButtonRow(buttons=(
            InteractiveButton(label="Sim", callback_data="yes"),
            InteractiveButton(label="Não", callback_data="no"),
        ))
        out = f.render_interactive(row, body_text="Confirma?")
        assert out["type"] == "button"
        assert out["body"]["text"] == "Confirma?"
        assert len(out["action"]["buttons"]) == 2
        assert out["action"]["buttons"][0]["reply"]["id"] == "yes"
        assert out["action"]["buttons"][0]["reply"]["title"] == "Sim"

    def test_button_row_truncates_long_label(self):
        f = WhatsAppFormatter()
        # WhatsApp caps button title at 20 chars
        row = InteractiveButtonRow(buttons=(
            InteractiveButton(
                label="Confirmar a opção desejada agora",
                callback_data="x",
            ),
        ))
        out = f.render_interactive(row)
        assert len(out["action"]["buttons"][0]["reply"]["title"]) <= 20

    def test_button_row_caps_at_three_buttons(self):
        # WhatsApp Reply Buttons max is 3 — InteractiveButtonRow accepts up
        # to 5 (Discord-friendly limit), so the formatter must trim.
        f = WhatsAppFormatter()
        row = InteractiveButtonRow(buttons=tuple(
            InteractiveButton(label=f"B{i}", callback_data=f"id{i}")
            for i in range(5)
        ))
        out = f.render_interactive(row)
        assert len(out["action"]["buttons"]) == 3

    def test_button_row_url_only_buttons_dropped(self):
        # WhatsApp Reply Buttons require callback_data; URL buttons are not
        # supported in this surface — they must be dropped silently.
        f = WhatsAppFormatter()
        row = InteractiveButtonRow(buttons=(
            InteractiveButton(label="Doc", url="https://docs.example.com"),
            InteractiveButton(label="OK", callback_data="ok"),
        ))
        out = f.render_interactive(row)
        ids = [b["reply"]["id"] for b in out["action"]["buttons"]]
        assert ids == ["ok"]

    def test_list_basic(self):
        f = WhatsAppFormatter()
        lst = InteractiveList(
            button_label="Ver opções",
            sections=(
                InteractiveListSection(
                    title="Categorias",
                    items=(
                        InteractiveButton(label="Suporte", callback_data="sup"),
                        InteractiveButton(label="Vendas", callback_data="ven"),
                    ),
                ),
            ),
        )
        out = f.render_interactive(lst, body_text="Como podemos ajudar?")
        assert out["type"] == "list"
        assert out["action"]["button"] == "Ver opções"
        assert len(out["action"]["sections"]) == 1
        assert out["action"]["sections"][0]["rows"][0]["id"] == "sup"

    def test_list_section_with_no_callback_items_skipped(self):
        f = WhatsAppFormatter()
        lst = InteractiveList(
            button_label="Op",
            sections=(
                InteractiveListSection(
                    title="Empty",
                    items=(InteractiveButton(label="X", url="https://e"),),
                ),
                InteractiveListSection(
                    title="Real",
                    items=(InteractiveButton(label="OK", callback_data="ok"),),
                ),
            ),
        )
        out = f.render_interactive(lst)
        assert len(out["action"]["sections"]) == 1  # empty section dropped
        assert out["action"]["sections"][0]["title"] == "Real"

    def test_list_no_usable_rows_raises(self):
        f = WhatsAppFormatter()
        lst = InteractiveList(
            button_label="Op",
            sections=(
                InteractiveListSection(
                    title="Empty",
                    items=(InteractiveButton(label="X", url="https://e"),),
                ),
            ),
        )
        with pytest.raises(ValueError, match="no usable rows"):
            f.render_interactive(lst)

    def test_unsupported_kind_raises(self):
        f = WhatsAppFormatter()
        with pytest.raises(ValueError):
            f.render_interactive("not-a-control", body_text="x")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TestAdapterSendInteractive (Phase 2)
# ---------------------------------------------------------------------------

class TestAdapterSendInteractive:
    async def test_send_button_row(self):
        from deilebot._testing import make_channel as _mk
        a = _adapter()
        a._client = MagicMock()
        a._client._post = AsyncMock(return_value="wamid.IK")
        ch = _mk(provider="whatsapp", provider_channel_id="100:5511999")
        row = InteractiveButtonRow(buttons=(
            InteractiveButton(label="OK", callback_data="ok"),
        ))
        msg_id = await a.send_interactive(ch, row, body_text="?")
        assert msg_id == "wamid.IK"
        sent_payload = a._client._post.call_args.args[0]
        assert sent_payload["type"] == "interactive"
        assert sent_payload["interactive"]["type"] == "button"
        assert sent_payload["to"] == "5511999"

    async def test_send_interactive_unsupported_kind(self):
        from deilebot._testing import make_channel as _mk
        from deilebot.foundation.interactive import QuickReplies, QuickReply
        a = _adapter()
        a._client = MagicMock()
        ch = _mk(provider="whatsapp", provider_channel_id="100:5511999")
        controls = QuickReplies(options=(QuickReply(label="x", payload="x"),))
        with pytest.raises(CapabilityNotSupported):
            await a.send_interactive(ch, controls)

    async def test_send_interactive_no_client_raises(self):
        from deilebot._testing import make_channel as _mk
        a = _adapter()
        ch = _mk(provider="whatsapp", provider_channel_id="100:5511999")
        row = InteractiveButtonRow(buttons=(
            InteractiveButton(label="OK", callback_data="ok"),
        ))
        with pytest.raises(ProviderError, match="not started"):
            await a.send_interactive(ch, row)
