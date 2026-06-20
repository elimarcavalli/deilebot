#!/usr/bin/env python3
"""AC-2 — Whisper accuracy benchmark. Runs OUTSIDE CI (API paga).

Usage:
    DEILE_BOT_TRANSCRIPTION_API_KEY=sk-... python3 tests/fixtures/transcription/benchmark.py

Requires: openai, pathlib (stdlib).
Clips in tests/fixtures/transcription/clips/<lang>_<NN>.<ext> with matching .ref.txt.
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLIPS_DIR = HERE / "clips"
MIN_CLIPS = 5
MAX_WER = 0.20


def _normalize(text: str) -> list[str]:
    """Lowercase, remove punctuation, split into words."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text, flags=re.UNICODE)
    return text.split()


def word_error_rate(ref: list[str], hyp: list[str]) -> float:
    """Levenshtein WER: insertions + deletions + substitutions / len(ref)."""
    if not ref:
        return 0.0 if not hyp else 1.0
    n, m = len(ref), len(hyp)
    dp = list(range(m + 1))
    for i in range(1, n + 1):
        prev = dp[:]
        dp[0] = i
        for j in range(1, m + 1):
            if ref[i - 1] == hyp[j - 1]:
                dp[j] = prev[j - 1]
            else:
                dp[j] = 1 + min(prev[j], dp[j - 1], prev[j - 1])
    return dp[m] / n


async def _transcribe_clip(api_key: str, clip_path: Path) -> str:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, timeout=120.0)
    audio_bytes = clip_path.read_bytes()
    result = await client.audio.transcriptions.create(
        model="whisper-1",
        file=(clip_path.name, io.BytesIO(audio_bytes), "audio/ogg"),
    )
    return result.text


async def main() -> int:
    api_key = os.environ.get("DEILE_BOT_TRANSCRIPTION_API_KEY")
    if not api_key:
        print("ERROR: DEILE_BOT_TRANSCRIPTION_API_KEY not set", file=sys.stderr)
        return 1

    if not CLIPS_DIR.exists():
        print(f"ERROR: clips directory not found: {CLIPS_DIR}", file=sys.stderr)
        print("Add ≥5 audio clips and .ref.txt files — see README.md", file=sys.stderr)
        return 1

    pairs: list[tuple[Path, Path]] = []
    for clip in sorted(CLIPS_DIR.glob("*")):
        if clip.suffix in (".txt", ".md") or clip.name.startswith("."):
            continue
        ref = clip.with_suffix(".ref.txt")
        if ref.exists():
            pairs.append((clip, ref))

    if len(pairs) < MIN_CLIPS:
        print(
            f"ERROR: found only {len(pairs)} clip(ref) pairs; need ≥{MIN_CLIPS}",
            file=sys.stderr,
        )
        return 1

    print(f"{'clip':<30} {'WER':>6}  {'acurácia':>8}")
    print("-" * 50)

    wers: list[float] = []
    for clip_path, ref_path in pairs:
        ref_text = ref_path.read_text(encoding="utf-8").strip()
        ref_words = _normalize(ref_text)
        try:
            hyp_text = await _transcribe_clip(api_key, clip_path)
        except Exception as e:
            print(f"{clip_path.name:<30} ERROR: {e}")
            continue
        hyp_words = _normalize(hyp_text)
        wer = word_error_rate(ref_words, hyp_words)
        wers.append(wer)
        acc = (1 - wer) * 100
        print(f"{clip_path.name:<30} {wer*100:>5.1f}%  {acc:>7.1f}%")

    if not wers:
        print("\nERROR: no clips transcribed successfully")
        return 1

    avg_wer = sum(wers) / len(wers)
    avg_acc = (1 - avg_wer) * 100
    print("-" * 50)
    print(f"{'MÉDIA':<30} {avg_wer*100:>5.1f}%  {avg_acc:>7.1f}%")
    print()
    if avg_wer <= MAX_WER:
        print(f"RESULTADO: APROVADO (WER {avg_wer*100:.1f}% ≤ {MAX_WER*100:.0f}%)")
        return 0
    else:
        print(f"RESULTADO: REPROVADO (WER {avg_wer*100:.1f}% > {MAX_WER*100:.0f}%)")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
