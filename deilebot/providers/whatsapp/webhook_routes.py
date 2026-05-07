"""aiohttp route handlers for WhatsApp webhook — GET handshake + POST dispatch."""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from deilebot.providers.whatsapp.adapter import WhatsAppAdapter

logger = logging.getLogger("deilebot.whatsapp.webhook")

_APP_KEY = "whatsapp_adapter"


def _json_error(code: str, message: str, *, status: int = 400) -> web.Response:
    return web.json_response({"error": code, "message": message}, status=status)


def _verify_signature(body: bytes, sig_header: str, secret: str) -> bool:
    """Verify X-Hub-Signature-256 header (sha256=<hex>) against body + app_secret.

    Meta sends this on every POST so the receiver can confirm the request
    genuinely came from the WhatsApp platform and was not spoofed.
    """
    if not sig_header.startswith("sha256="):
        return False
    expected = hmac.new(key=secret.encode(), msg=body, digestmod=hashlib.sha256).hexdigest()
    received = sig_header[7:]
    return hmac.compare_digest(expected, received)


async def _get_verify(request: web.Request) -> web.Response:
    """Meta webhook verification handshake (GET with hub.* query params)."""
    adapter: WhatsAppAdapter = request.app[_APP_KEY]
    mode = request.rel_url.query.get("hub.mode", "")
    token = request.rel_url.query.get("hub.verify_token", "")
    challenge = request.rel_url.query.get("hub.challenge", "")

    if mode != "subscribe":
        return _json_error("BAD_REQUEST", "hub.mode must be 'subscribe'")

    expected = adapter.settings.verify_token.get_secret_value()
    if not expected:
        logger.error("whatsapp webhook: verify_token is not configured")
        return _json_error("FORBIDDEN", "verify_token not configured", status=403)

    if not hmac.compare_digest(token.encode(), expected.encode()):
        logger.warning("whatsapp webhook verify_token mismatch")
        return _json_error("FORBIDDEN", "invalid verify_token", status=403)

    return web.Response(text=challenge)


async def _post_events(request: web.Request) -> web.Response:
    """Receive WhatsApp Cloud API event payload and dispatch to the adapter.

    Returns 200 even for status="ignored" — Meta requires a 200 within 20 s
    for all POST deliveries or it retries with exponential back-off.
    """
    adapter: WhatsAppAdapter = request.app[_APP_KEY]

    body = await request.read()

    app_secret_obj = adapter.settings.app_secret
    if app_secret_obj is not None:
        secret = app_secret_obj.get_secret_value()
        if secret:
            sig_header = request.headers.get("X-Hub-Signature-256", "")
            if not _verify_signature(body, sig_header, secret):
                logger.warning("whatsapp webhook X-Hub-Signature-256 mismatch")
                return _json_error("FORBIDDEN", "invalid signature", status=403)

    try:
        import json
        payload = json.loads(body)
    except Exception:
        return _json_error("BAD_REQUEST", "invalid JSON body")

    try:
        result = await adapter.handle_webhook(payload)
    except Exception:
        logger.exception("whatsapp handle_webhook raised")
        return _json_error("INTERNAL_ERROR", "webhook dispatch failed", status=500)

    return web.json_response(result)


def register_routes(
    app: web.Application,
    adapter: WhatsAppAdapter,
    *,
    path: str = "/webhook/whatsapp",
) -> None:
    """Mount GET + POST WhatsApp webhook handlers on an existing aiohttp app.

    Call this once after creating the aiohttp Application and before starting
    the server. The adapter reference is stored in ``app[_APP_KEY]`` so handlers
    can reach it without globals.
    """
    app[_APP_KEY] = adapter
    app.router.add_get(path, _get_verify)
    app.router.add_post(path, _post_events)
