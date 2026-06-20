"""TranscriptionService — STT via OpenAI Whisper for audio attachments."""
from __future__ import annotations

import io
import os
from typing import Optional

from deilebot.foundation.logging import get_logger
from deilebot.foundation.settings import TranscriptionSettings
from deilebot.foundation.transcription_budget import TranscriptionBudgetTracker

_AUDIO_DOWNLOAD_TIMEOUT_S = 30.0
_WHISPER_TIMEOUT_S = 60.0
# Conservative bitrate fallback for duration estimation when mutagen is absent
_ASSUMED_BITRATE_BITS_PER_S = 128_000


class TranscriptionError(Exception):
    """Raised for any STT failure; caught by the pipeline, never propagated."""


class TranscriptionService:
    def __init__(
        self,
        settings: TranscriptionSettings,
        budget: TranscriptionBudgetTracker,
    ):
        self._settings = settings
        self._budget = budget
        self._logger = get_logger("transcription")

    def _get_api_key(self) -> Optional[str]:
        return os.environ.get("DEILE_BOT_TRANSCRIPTION_API_KEY")

    async def _download_audio(self, url: str) -> bytes:
        import httpx

        try:
            async with httpx.AsyncClient(
                timeout=_AUDIO_DOWNLOAD_TIMEOUT_S, follow_redirects=True
            ) as cli:
                resp = await cli.get(url)
        except httpx.TimeoutException as e:
            raise TranscriptionError(f"download timeout: {e}") from e
        except Exception as e:
            raise TranscriptionError(f"download error: {type(e).__name__}: {e}") from e
        if resp.status_code >= 400:
            raise TranscriptionError(f"download failed: HTTP {resp.status_code}")
        return resp.content

    def _estimate_duration_seconds(self, audio_bytes: bytes, mime: Optional[str]) -> float:
        """Estimate duration via mutagen metadata; falls back to size/bitrate."""
        try:
            import mutagen  # type: ignore[import]

            f = mutagen.File(io.BytesIO(audio_bytes))
            if f is not None and f.info is not None:
                return float(f.info.length)
        except Exception:
            pass
        return len(audio_bytes) * 8 / _ASSUMED_BITRATE_BITS_PER_S

    async def _call_whisper(
        self, audio_bytes: bytes, filename: str, mime: str, *, language: Optional[str] = None
    ) -> str:
        api_key = self._get_api_key()
        if not api_key:
            raise TranscriptionError("DEILE_BOT_TRANSCRIPTION_API_KEY não configurado")
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=api_key, timeout=_WHISPER_TIMEOUT_S)
        kwargs: dict = dict(
            model="whisper-1",
            file=(filename, io.BytesIO(audio_bytes), mime),
        )
        if language is not None:
            kwargs["language"] = language
        result = await client.audio.transcriptions.create(**kwargs)
        return result.text

    async def transcribe(self, att, *, language: Optional[str] = None) -> str:
        """Download att.url, enforce limits, call Whisper. Returns transcript text.

        ``language`` is an ISO-639-1 code (e.g. "pt", "en") or None for Whisper
        auto-detection. Resolve it via ``settings.resolve_language`` before calling.

        Raises TranscriptionError on any failure — never swallows silently.
        """
        if not att.url:
            raise TranscriptionError("attachment has no URL")

        audio_bytes = await self._download_audio(att.url)

        duration_s = self._estimate_duration_seconds(audio_bytes, att.mime)
        if duration_s > self._settings.max_duration_seconds:
            raise TranscriptionError(
                f"áudio muito longo ({duration_s:.0f}s > {self._settings.max_duration_seconds}s max)"
            )

        allowed = await self._budget.try_reserve(
            duration_s, self._settings.max_minutes_per_month
        )
        if not allowed:
            raise TranscriptionError(
                f"teto mensal de transcrição atingido ({self._settings.max_minutes_per_month} min)"
            )

        filename = att.filename or "audio.ogg"
        mime = att.mime or "audio/ogg"
        try:
            return await self._call_whisper(audio_bytes, filename, mime, language=language)
        except TranscriptionError:
            raise
        except Exception as e:
            raise TranscriptionError(
                f"Whisper API error: {type(e).__name__}: {e}"
            ) from e
