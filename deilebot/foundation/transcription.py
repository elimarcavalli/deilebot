"""TranscriptionService — STT via OpenAI Whisper for audio attachments."""
from __future__ import annotations

import asyncio
import io
import math
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

    async def _transcribe_chunk(
        self, chunk_bytes: bytes, index: int, filename: str, mime: str
    ) -> str:
        """Transcribe a single chunk. Isolated for mocking in tests."""
        return await self._call_whisper(chunk_bytes, filename, mime)

    async def _transcribe_chunked(
        self, audio_bytes: bytes, duration_s: float, filename: str, mime: str
    ) -> str:
        """Split audio proportionally and transcribe chunks in parallel."""
        chunk_seconds = self._settings.chunk_seconds
        num_chunks = math.ceil(duration_s / chunk_seconds)

        total_bytes = len(audio_bytes)
        base_size = total_bytes // num_chunks
        chunks = []
        for i in range(num_chunks):
            start = i * base_size
            end = start + base_size if i < num_chunks - 1 else total_bytes
            chunks.append(audio_bytes[start:end])

        sem = asyncio.Semaphore(self._settings.chunk_max_concurrency)
        timeout_s = float(self._settings.chunk_timeout_seconds)
        mode = self._settings.chunk_on_partial_failure

        async def _run_one(idx: int, chunk: bytes):
            async with sem:
                try:
                    return await asyncio.wait_for(
                        self._transcribe_chunk(chunk, idx, filename, mime),
                        timeout=timeout_s,
                    )
                except Exception as exc:
                    return exc

        tasks = [asyncio.create_task(_run_one(i, c)) for i, c in enumerate(chunks)]
        raw = await asyncio.gather(*tasks)

        texts = []
        completed = 0
        failed = 0

        for i, result in enumerate(raw):
            if isinstance(result, Exception):
                failed += 1
                if mode == "fail":
                    raise TranscriptionError(
                        f"chunk {i} falhou ({type(result).__name__}: {result})"
                    )
                elif mode == "skip":
                    pass
                else:  # best_effort
                    texts.append("[transcrição indisponível]")
            else:
                completed += 1
                texts.append(result)

        self._logger.info(
            "chunked transcription complete",
            extra={
                "chunk_count": num_chunks,
                "completed": completed,
                "failed": failed,
                "mode": mode,
            },
        )

        return " ".join(texts)

    async def transcribe(self, att, *, language: Optional[str] = None) -> str:
        """Download att.url, enforce limits, call Whisper. Returns transcript text.

        ``language`` is an ISO-639-1 code (e.g. "pt", "en") or None for Whisper
        auto-detection. Resolve it via ``settings.resolve_language`` before calling.

        For audio with duration > max_duration_seconds, splits into chunks and
        transcribes in parallel (the chunked path currently relies on Whisper
        auto-detection per chunk). Raises TranscriptionError on any failure —
        never swallows silently.
        """
        if not att.url:
            raise TranscriptionError("attachment has no URL")

        audio_bytes = await self._download_audio(att.url)

        duration_s = self._estimate_duration_seconds(audio_bytes, att.mime)

        allowed = await self._budget.try_reserve(
            duration_s, self._settings.max_minutes_per_month
        )
        if not allowed:
            raise TranscriptionError(
                f"teto mensal de transcrição atingido ({self._settings.max_minutes_per_month} min)"
            )

        filename = att.filename or "audio.ogg"
        mime = att.mime or "audio/ogg"

        if duration_s > self._settings.max_duration_seconds:
            try:
                return await self._transcribe_chunked(audio_bytes, duration_s, filename, mime)
            except TranscriptionError:
                raise
            except Exception as e:
                raise TranscriptionError(
                    f"chunked transcription error: {type(e).__name__}: {e}"
                ) from e

        try:
            return await self._call_whisper(audio_bytes, filename, mime, language=language)
        except TranscriptionError:
            raise
        except Exception as e:
            raise TranscriptionError(
                f"Whisper API error: {type(e).__name__}: {e}"
            ) from e
