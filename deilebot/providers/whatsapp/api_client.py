"""WhatsApp Cloud API HTTP client."""

from __future__ import annotations

from typing import Any, Mapping

from deilebot.foundation.exceptions import ProviderError


def _http_status(exc: Exception) -> str:
    """Extract the HTTP status code from an httpx exception without leaking token."""
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return str(code) if code is not None else "?"


class WhatsAppApiClient:
    """Minimal HTTP wrapper around graph.facebook.com endpoints.

    Lifecycle: call ``close()`` (or use ``async with``) when done to release
    the underlying httpx connection pool. The adapter calls ``close()`` in
    ``stop()``; direct users of the client should use ``async with client``.
    """

    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        api_version: str = "v22.0",
    ):
        self._token = access_token
        self._phone_id = phone_number_id
        self._version = api_version
        self._client: Any = None

    async def __aenter__(self) -> WhatsAppApiClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    @property
    def base_url(self) -> str:
        return f"https://graph.facebook.com/{self._version}/{self._phone_id}/messages"

    @property
    def _media_url(self) -> str:
        return f"https://graph.facebook.com/{self._version}/{self._phone_id}/media"

    async def _get_client(self) -> Any:
        if self._client is None:
            try:
                import httpx
            except ImportError as e:
                raise ProviderError("httpx not installed (extras=whatsapp)", context={}) from e
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def send_text(self, to: str, text: str) -> str:
        return await self._post({
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text},
        })

    async def send_template(
        self,
        to: str,
        name: str,
        language: str,
        components: list = None,
    ) -> str:
        return await self._post({
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": name,
                "language": {"code": language},
                "components": components or [],
            },
        })

    async def upload_media(self, content: bytes, mime_type: str) -> str:
        """Upload raw bytes; returns media_id. Raises ProviderError on failure."""
        client = await self._get_client()
        try:
            r = await client.post(
                self._media_url,
                headers={"Authorization": f"Bearer {self._token}"},
                files={"file": ("upload", content, mime_type)},
                data={"messaging_product": "whatsapp"},
            )
            r.raise_for_status()
            media_id = r.json().get("id")
            if not media_id:
                raise ProviderError(
                    "whatsapp media upload: missing 'id' in response",
                    context={"response_body": r.text[:200]},
                )
            return str(media_id)
        except ProviderError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ProviderError(
                f"whatsapp media upload failed: HTTP {_http_status(e)}",
                context={"status_code": _http_status(e)},
            ) from e

    async def get_media_url(self, media_id: str) -> str:
        """Resolve a media_id to its temporary download URL."""
        client = await self._get_client()
        try:
            r = await client.get(
                f"https://graph.facebook.com/{self._version}/{media_id}",
                headers={"Authorization": f"Bearer {self._token}"},
            )
            r.raise_for_status()
            url = r.json().get("url")
            if not url:
                raise ProviderError(
                    "whatsapp get_media_url: missing 'url' in response",
                    context={"media_id": media_id},
                )
            return str(url)
        except ProviderError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ProviderError(
                f"whatsapp get_media_url failed: HTTP {_http_status(e)}",
                context={"status_code": _http_status(e)},
            ) from e

    async def download_media_bytes(self, media_url: str) -> bytes:
        """Download raw bytes from a temporary WhatsApp media URL."""
        client = await self._get_client()
        try:
            r = await client.get(
                media_url,
                headers={"Authorization": f"Bearer {self._token}"},
            )
            r.raise_for_status()
            return r.content
        except Exception as e:  # noqa: BLE001
            raise ProviderError(
                f"whatsapp download_media_bytes failed: HTTP {_http_status(e)}",
                context={"status_code": _http_status(e)},
            ) from e

    async def _post(self, payload: Mapping[str, Any]) -> str:
        client = await self._get_client()
        try:
            r = await client.post(
                self.base_url,
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
            )
            r.raise_for_status()
            data = r.json()
            msg_id = (data.get("messages") or [{}])[0].get("id")
            if not msg_id:
                raise ProviderError(
                    "whatsapp send: missing message 'id' in response",
                    context={"response_body": str(data)[:200]},
                )
            return str(msg_id)
        except ProviderError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ProviderError(
                f"whatsapp send failed: HTTP {_http_status(e)}",
                context={"status_code": _http_status(e)},
            ) from e

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
