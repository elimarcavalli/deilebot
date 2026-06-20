"""Tests for TranscriptionService chunking — issue #30 + issue #49.

Covers AC-1..AC-8 via the public transcribe() entry-point.
Issue #49 adds: temporal-split integration (AC#2) and language propagation (AC#3).
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import math
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deilebot.foundation.envelope import Attachment, AttachmentKind
from deilebot.foundation.settings import TranscriptionSettings
from deilebot.foundation.transcription import TranscriptionError, TranscriptionService
from deilebot.foundation.transcription_budget import TranscriptionBudgetTracker

_FAKE_AUDIO = b"\x4f\x67\x67\x53" + b"\x00" * 60
_AV_AVAILABLE = importlib.util.find_spec("av") is not None


def _byte_split(audio_bytes: bytes, chunk_seconds: int, mime: str, num_chunks: int) -> list:
    """Byte-split stub for _split_audio_by_time used in unit tests (no av needed).

    Returns exactly ``num_chunks`` fake chunks so tests that verify chunk-count
    behavior work without PyAV installed.
    """
    if num_chunks <= 1:
        return [audio_bytes]
    size = max(1, len(audio_bytes) // num_chunks)
    chunks = [audio_bytes[i * size:(i + 1) * size] for i in range(num_chunks - 1)]
    chunks.append(audio_bytes[(num_chunks - 1) * size:] or audio_bytes[:1])
    return chunks


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def budget(tmp_path: Path) -> TranscriptionBudgetTracker:
    tracker = TranscriptionBudgetTracker(tmp_path / "budget.sqlite")
    yield tracker
    await tracker.close()


@pytest.fixture(autouse=True)
def _patch_split_audio(request):
    """Patch _split_audio_by_time for all tests that don't need real PyAV.

    Tests marked with ``pytest.mark.needs_av`` are excluded — they import av
    directly and verify real decodability (AC#2 from issue #49).
    """
    if request.node.get_closest_marker("needs_av"):
        yield
        return
    with patch.object(
        TranscriptionService,
        "_split_audio_by_time",
        side_effect=_byte_split,
    ):
        yield


def _make_svc(
    budget,
    *,
    max_duration: int = 120,
    chunk_seconds: int = 60,
    chunk_on_partial_failure: str = "fail",
    chunk_max_concurrency: int = 3,
    chunk_timeout_seconds: int = 90,
) -> TranscriptionService:
    settings = TranscriptionSettings(
        enabled=True,
        max_duration_seconds=max_duration,
        chunk_seconds=chunk_seconds,
        chunk_on_partial_failure=chunk_on_partial_failure,
        chunk_max_concurrency=chunk_max_concurrency,
        chunk_timeout_seconds=chunk_timeout_seconds,
    )
    return TranscriptionService(settings=settings, budget=budget)


def _att() -> Attachment:
    return Attachment(
        kind=AttachmentKind.AUDIO,
        url="http://fake/audio.ogg",
        mime="audio/ogg",
        filename="audio.ogg",
    )


# ── AC-1: chunk count ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "duration_s,chunk_s,expected_chunks",
    [
        (150, 60, 3),  # ceil(150/60)=3
        (120, 60, 2),  # exact boundary: no empty residual
    ],
)
async def test_ac1_chunk_count(budget, duration_s, chunk_s, expected_chunks):
    """AC-1: correct number of chunks for known durations."""
    svc = _make_svc(budget, max_duration=60, chunk_seconds=chunk_s)
    calls = []

    async def fake_chunk(chunk_bytes, index, filename, mime, **kw):
        calls.append(index)
        return f"t{index}"

    with patch.object(svc, "_download_audio", new=AsyncMock(return_value=_FAKE_AUDIO)):
        with patch.object(svc, "_estimate_duration_seconds", return_value=float(duration_s)):
            with patch.object(svc, "_transcribe_chunk", new=AsyncMock(side_effect=fake_chunk)):
                await svc.transcribe(_att())

    assert len(calls) == expected_chunks, (
        f"duration={duration_s}s chunk={chunk_s}s → expected {expected_chunks} chunks, got {len(calls)}"
    )
    assert sorted(calls) == list(range(expected_chunks))


# ── AC-2: temporal order ──────────────────────────────────────────────────────


async def test_ac2_temporal_order(budget):
    """AC-2: output preserves chunk order even when tasks resolve in reverse."""
    svc = _make_svc(budget, max_duration=60, chunk_seconds=60)
    texts = ["um", "dois", "três"]
    # Resolve in reverse: chunk 2 fastest, chunk 0 slowest
    delays = [0.05, 0.03, 0.01]

    async def fake_chunk(chunk_bytes, index, filename, mime, **kw):
        await asyncio.sleep(delays[index])
        return texts[index]

    with patch.object(svc, "_download_audio", new=AsyncMock(return_value=_FAKE_AUDIO)):
        with patch.object(svc, "_estimate_duration_seconds", return_value=180.0):
            with patch.object(svc, "_transcribe_chunk", new=AsyncMock(side_effect=fake_chunk)):
                result = await svc.transcribe(_att())

    assert result == "um dois três", f"order broken: {result!r}"


# ── AC-3: partial failure modes ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("fail", None),       # raises — no transcript
        ("skip", "um três"),  # middle chunk omitted
        ("best_effort", "um [transcrição indisponível] três"),
    ],
)
async def test_ac3_partial_failure(budget, mode, expected):
    """AC-3: chunk_on_partial_failure behaves correctly for all 3 modes."""
    svc = _make_svc(budget, max_duration=60, chunk_seconds=60, chunk_on_partial_failure=mode)
    chunk_texts = ["um", None, "três"]  # None → fail

    async def fake_chunk(chunk_bytes, index, filename, mime, **kw):
        if chunk_texts[index] is None:
            raise RuntimeError("forced failure")
        return chunk_texts[index]

    ctx = patch.object(svc, "_download_audio", new=AsyncMock(return_value=_FAKE_AUDIO))
    dur = patch.object(svc, "_estimate_duration_seconds", return_value=180.0)
    chunk_patch = patch.object(svc, "_transcribe_chunk", new=AsyncMock(side_effect=fake_chunk))

    with ctx, dur, chunk_patch:
        if mode == "fail":
            with pytest.raises(TranscriptionError):
                await svc.transcribe(_att())
        else:
            result = await svc.transcribe(_att())
            assert result == expected, f"mode={mode!r}: expected {expected!r}, got {result!r}"


# ── AC-4: concurrency limit ───────────────────────────────────────────────────


async def test_ac4_concurrency_peak(budget):
    """AC-4: peak concurrency == chunk_max_concurrency (≤ limit, ≥ limit = saturated)."""
    concurrency = 2
    svc = _make_svc(
        budget,
        max_duration=10,
        chunk_seconds=10,
        chunk_max_concurrency=concurrency,
    )

    counter = 0
    peak = 0
    lock = asyncio.Lock()

    async def fake_chunk(chunk_bytes, index, filename, mime, **kw):
        nonlocal counter, peak
        async with lock:
            counter += 1
            if counter > peak:
                peak = counter
        await asyncio.sleep(0.02)  # hold slot briefly so siblings can saturate
        async with lock:
            counter -= 1
        return f"t{index}"

    # 5 chunks of 10s each → 50s total, max_duration=10 → all 5 go to chunking
    with patch.object(svc, "_download_audio", new=AsyncMock(return_value=_FAKE_AUDIO)):
        with patch.object(svc, "_estimate_duration_seconds", return_value=50.0):
            with patch.object(svc, "_transcribe_chunk", new=AsyncMock(side_effect=fake_chunk)):
                await svc.transcribe(_att())

    assert peak <= concurrency, f"peak concurrency {peak} exceeded limit {concurrency}"
    assert peak >= concurrency, f"peak concurrency {peak} never saturated limit {concurrency}"


# ── AC-5: single-shot non-regression ─────────────────────────────────────────


async def test_ac5_single_shot_no_chunking(budget):
    """AC-5: audio <= max_duration_seconds uses single-shot path; _transcribe_chunk never called."""
    svc = _make_svc(budget, max_duration=120)
    chunk_called = []

    async def fake_chunk(*_a, **_kw):
        chunk_called.append(True)
        return "should not be reached"

    with patch.object(svc, "_download_audio", new=AsyncMock(return_value=_FAKE_AUDIO)):
        with patch.object(svc, "_estimate_duration_seconds", return_value=119.0):
            with patch.object(svc, "_transcribe_chunk", new=AsyncMock(side_effect=fake_chunk)):
                with patch.object(svc, "_call_whisper", new=AsyncMock(return_value="direct")):
                    result = await svc.transcribe(_att())

    assert result == "direct"
    assert not chunk_called, "_transcribe_chunk was invoked on single-shot path"


# ── AC-6: overlap locked at 0 ─────────────────────────────────────────────────


def test_ac6_overlap_gt0_rejected_at_boot():
    """AC-6: chunk_overlap_seconds > 0 raises ValueError at settings construction."""
    with pytest.raises(ValueError, match="chunk_overlap_seconds"):
        TranscriptionSettings(chunk_overlap_seconds=1)


def test_ac6_overlap_zero_accepted():
    """AC-6: chunk_overlap_seconds=0 is the valid default."""
    s = TranscriptionSettings(chunk_overlap_seconds=0)
    assert s.chunk_overlap_seconds == 0


# ── AC-7: timeout per chunk ───────────────────────────────────────────────────


async def test_ac7_chunk_timeout_skip(budget):
    """AC-7: chunk exceeding chunk_timeout_seconds is treated as failed; mode=skip omits it."""
    svc = _make_svc(
        budget,
        max_duration=60,
        chunk_seconds=60,
        chunk_on_partial_failure="skip",
        chunk_timeout_seconds=1,
    )
    # 3 chunks: index 1 times out
    async def fake_chunk(chunk_bytes, index, filename, mime, **kw):
        if index == 1:
            await asyncio.sleep(5)  # exceeds chunk_timeout_seconds=1
        return f"t{index}"

    with patch.object(svc, "_download_audio", new=AsyncMock(return_value=_FAKE_AUDIO)):
        with patch.object(svc, "_estimate_duration_seconds", return_value=180.0):
            with patch.object(svc, "_transcribe_chunk", new=AsyncMock(side_effect=fake_chunk)):
                result = await svc.transcribe(_att())

    assert "t1" not in result, "timed-out chunk should be omitted in skip mode"
    assert "t0" in result and "t2" in result, "non-timed chunks should be present"


async def test_ac7_transcribe_returns_despite_timeout(budget):
    """AC-7: transcribe() returns (doesn't block forever) when a chunk times out."""
    svc = _make_svc(
        budget,
        max_duration=60,
        chunk_seconds=60,
        chunk_on_partial_failure="skip",
        chunk_timeout_seconds=1,
    )

    async def fake_chunk(chunk_bytes, index, filename, mime, **kw):
        if index == 1:
            await asyncio.sleep(60)  # would block forever without timeout guard
        return f"t{index}"

    with patch.object(svc, "_download_audio", new=AsyncMock(return_value=_FAKE_AUDIO)):
        with patch.object(svc, "_estimate_duration_seconds", return_value=180.0):
            with patch.object(svc, "_transcribe_chunk", new=AsyncMock(side_effect=fake_chunk)):
                # Must complete in well under 60s — asyncio.wait_for enforces the 1s limit
                result = await asyncio.wait_for(svc.transcribe(_att()), timeout=5.0)

    assert "t1" not in result
    assert "t0" in result and "t2" in result


# ── AC-8: structured log ──────────────────────────────────────────────────────


async def test_ac8_structured_log(budget):
    """AC-8: log emitted with chunk_count, completed, failed, mode for 3 chunks / 1 failure / skip."""
    import logging

    svc = _make_svc(budget, max_duration=60, chunk_seconds=60, chunk_on_partial_failure="skip")
    chunk_texts = ["alfa", None, "gama"]

    captured: list = []

    class _Cap(logging.Handler):
        def emit(self, record):
            captured.append(record)

    logger = logging.getLogger("deilebot.transcription")
    handler = _Cap()
    handler.setLevel(logging.DEBUG)
    prev_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)

    async def fake_chunk(chunk_bytes, index, filename, mime, **kw):
        if chunk_texts[index] is None:
            raise RuntimeError("injected failure")
        return chunk_texts[index]

    try:
        with patch.object(svc, "_download_audio", new=AsyncMock(return_value=_FAKE_AUDIO)):
            with patch.object(svc, "_estimate_duration_seconds", return_value=180.0):
                with patch.object(svc, "_transcribe_chunk", new=AsyncMock(side_effect=fake_chunk)):
                    await svc.transcribe(_att())
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)

    records = [r for r in captured if getattr(r, "chunk_count", None) is not None]
    assert records, "no structured log record with chunk_count found"
    rec = records[0]
    assert rec.chunk_count == 3, f"chunk_count={rec.chunk_count}"
    assert rec.completed == 2, f"completed={rec.completed}"
    assert rec.failed == 1, f"failed={rec.failed}"
    assert rec.mode == "skip", f"mode={rec.mode}"

# ── Issue #49 AC#2: temporal chunks decodable via PyAV ───────────────────────


@pytest.mark.needs_av
@pytest.mark.skipif(not _AV_AVAILABLE, reason="av not installed")
def test_i49_ac2_pyav_chunks_decodable(tmp_path):
    """AC#2 (issue #49): _split_audio_by_time produces chunks each decodable via PyAV.

    Generates a minimal real WAV/OGG file with duration > max_duration_seconds,
    splits it into ≥2 chunks, and asserts each chunk yields ≥1 audio frame.
    Skipped if PyAV is not installed (CI without av wheel).
    """
    import struct
    import wave
    import av

    # Build a minimal WAV: 3 seconds mono 8000Hz 16-bit = 48000 samples
    wav_path = tmp_path / "test.wav"
    with wave.open(str(wav_path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        # 3 seconds of silence
        wf.writeframes(b"\x00" * 2 * 8000 * 3)
    wav_bytes = wav_path.read_bytes()

    # chunk_seconds=1 → ceil(3/1)=3 chunks; each should be independently decodable
    chunks = TranscriptionService._split_audio_by_time(wav_bytes, chunk_seconds=1, mime="audio/wav", num_chunks=3)

    assert len(chunks) >= 2, f"Expected ≥2 chunks, got {len(chunks)}"
    for idx, chunk in enumerate(chunks):
        assert chunk, f"chunk {idx} is empty"
        try:
            container = av.open(io.BytesIO(chunk))
            frames = list(container.decode(audio=0))
            container.close()
            assert frames, f"chunk {idx} yielded 0 audio frames — header invalid or no audio"
        except Exception as e:
            raise AssertionError(f"chunk {idx} failed to decode via PyAV: {e}") from e



# ── Issue #49 AC#3: language propagated end-to-end (real call-site) ──────────


async def test_i49_ac3_language_propagated_through_chunking(budget):
    """AC#3 (issue #49): transcribe(language='pt') propagates to every _call_whisper chunk.

    Drives the REAL call-path transcribe→_transcribe_chunked→_transcribe_chunk→_call_whisper.
    Unit-isolating _transcribe_chunk is insufficient (Goodhart / GC #596).
    """
    svc = _make_svc(budget, max_duration=60, chunk_seconds=60)
    whisper_calls = []

    async def spy_whisper(audio_bytes, filename, mime, *, language=None):
        whisper_calls.append({"language": language})
        return "texto"

    with patch.object(svc, "_download_audio", new=AsyncMock(return_value=_FAKE_AUDIO)):
        with patch.object(svc, "_estimate_duration_seconds", return_value=180.0):
            with patch.object(svc, "_call_whisper", new=AsyncMock(side_effect=spy_whisper)):
                await svc.transcribe(_att(), language="pt")

    assert whisper_calls, "_call_whisper was never called — chunked path not exercised"
    assert len(whisper_calls) >= 2, (
        f"Expected ≥2 chunk calls (chunked path), got {len(whisper_calls)}"
    )
    for i, call in enumerate(whisper_calls):
        assert call["language"] == "pt", (
            f"chunk {i}: expected language='pt', got {call['language']!r} — "
            "language not propagated through transcribe→_transcribe_chunked→_transcribe_chunk→_call_whisper"
        )
