"""WhatsApp media upload + reference flow.

High-level facade over the low-level media methods in WhatsAppApiClient.
Use this module when you need to upload local bytes before sending a media
message, or when you need to fetch the raw bytes of an inbound attachment.

Usage::

    media = WhatsAppMedia(api_client)
    media_id = await media.upload(content, "image/jpeg")
    url = await media.get_url(media_id)
    raw = await media.download(url)
"""

from __future__ import annotations

from deilebot.providers.whatsapp.api_client import WhatsAppApiClient


class WhatsAppMedia:
    """Facade for WhatsApp Cloud API media operations."""

    def __init__(self, client: WhatsAppApiClient) -> None:
        self._client = client

    async def upload(self, content: bytes, mime_type: str) -> str:
        """Upload bytes; returns the media_id to use in outbound messages."""
        return await self._client.upload_media(content, mime_type)

    async def get_url(self, media_id: str) -> str:
        """Resolve a media_id to its short-lived download URL."""
        return await self._client.get_media_url(media_id)

    async def download(self, media_url: str) -> bytes:
        """Download raw bytes from a short-lived WhatsApp media URL."""
        return await self._client.download_media_bytes(media_url)
