"""TranscriptionService — STT via OpenAI Whisper or local faster-whisper."""
from __future__ import annotations

import asyncio
import io
import os
import time
from pathlib import Path
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


class _LocalWhisperBackend:
    """faster-whisper backend for local/offline transcription."""

    def __init__(self, settings: TranscriptionSettings) -> None:
        model_path = settings.local_model_path
        if model_path is None:
            raise TranscriptionError(
                "engine: local requer local_model_path configurado"
            )
        model_path = Path(model_path)
        if not model_path.exists():
            raise TranscriptionError(
                f"local_model_path não existe: {model_path}"
            )
        if not model_path.is_dir():
            raise TranscriptionError(
                f"local_model_path deve ser um diretório CTranslate2: {model_path}"
            )
        # Validate the directory contains expected CTranslate2 files
        model_bin = model_path / "model.bin"
        if not model_bin.exists():
            raise TranscriptionError(
                f"local_model_path não contém model.bin — diretório CTranslate2 inválido: {model_path}"
            )
        self._model_path = model_path
        self._device = settings.local_device
        self._compute_type = settings.local_compute_type
        self._timeout_s = settings.local_timeout_seconds
        self._model = None
        self._logger = get_logger("transcription.local")
        self._load_model()

    def _load_model(self) -> None:
        # Import here so tests can mock before instantiation
        try:
            from faster_whisper import WhisperModel  # type: ignore[import]
        except ImportError as e:
            raise TranscriptionError(
                f"faster-whisper não instalado: {e}"
            ) from e

        # local_files_only=True enforced via str path (no HuggingFace download)
        self._model = WhisperModel(
            str(self._model_path),
            device=self._device,
            compute_type=self._compute_type,
            local_files_only=True,
        )

    def _transcribe_sync(self, audio_bytes: bytes) -> str:
        segments, _ = self._model.transcribe(
            io.BytesIO(audio_bytes),
            beam_size=5,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()

    async def transcribe(self, audio_bytes: bytes) -> str:
        logger = self._logger
        t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self._transcribe_sync, audio_bytes),
                timeout=self._timeout_s,
            )
        except asyncio.TimeoutError as e:
            duration_ms = int((time.monotonic() - t0) * 1000)
            logger.warning(
                "local transcription timeout",
                extra={"engine": "local", "duration_ms": duration_ms, "outcome": "timeout"},
            )
            raise TranscriptionError(
                f"transcrição local excedeu timeout de {self._timeout_s}s"
            ) from e
        except Exception as e:
            duration_ms = int((time.monotonic() - t0) * 1000)
            logger.warning(
                "local transcription error",
                extra={"engine": "local", "duration_ms": duration_ms, "outcome": "error"},
                exc_info=True,
            )
            raise TranscriptionError(
                f"faster-whisper error: {type(e).__name__}: {e}"
            ) from e

        duration_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "local transcription ok",
            extra={"engine": "local", "duration_ms": duration_ms, "outcome": "ok"},
        )
        return result


class TranscriptionService:
    def __init__(
        self,
        settings: TranscriptionSettings,
        budget: TranscriptionBudgetTracker,
    ):
        self._settings = settings
        self._budget = budget
        self._logger = get_logger("transcription")
        self._local_backend: Optional[_LocalWhisperBackend] = None
        if settings.engine == "local":
            # Fail fast on init — not silently at first audio
            self._local_backend = _LocalWhisperBackend(settings)

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
        self, audio_bytes: bytes, filename: str, mime: str
    ) -> str:
        api_key = self._get_api_key()
        if not api_key:
            raise TranscriptionError("DEILE_BOT_TRANSCRIPTION_API_KEY não configurado")
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=api_key, timeout=_WHISPER_TIMEOUT_S)
        result = await client.audio.transcriptions.create(
            model="whisper-1",
            file=(filename, io.BytesIO(audio_bytes), mime),
        )
        return result.text

    async def transcribe(self, att) -> str:
        """Download att.url, enforce limits, call STT backend. Returns transcript text.

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

        if self._settings.engine == "local":
            assert self._local_backend is not None
            try:
                return await self._local_backend.transcribe(audio_bytes)
            except TranscriptionError:
                raise
            except Exception as e:
                raise TranscriptionError(
                    f"local backend error: {type(e).__name__}: {e}"
                ) from e

        filename = att.filename or "audio.ogg"
        mime = att.mime or "audio/ogg"
        try:
            return await self._call_whisper(audio_bytes, filename, mime)
        except TranscriptionError:
            raise
        except Exception as e:
            raise TranscriptionError(
                f"Whisper API error: {type(e).__name__}: {e}"
            ) from e
