#!/usr/bin/env python3
"""AC-8 — Whisper local (faster-whisper) accuracy benchmark. Runs OUTSIDE CI.

Usage:
    LOCAL_MODEL_PATH=/path/to/ctranslate2/model \
    python3 tests/fixtures/transcription/benchmark_local.py [--device cpu] [--compute_type int8]

WER threshold: ≤25% (acurácia ≥75%). More lenient than openai (≤20%) — smaller model on CPU.
Clips in tests/fixtures/transcription/clips/<lang>_<NN>.<ext> with matching .ref.txt.
"""
from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLIPS_DIR = HERE / "clips"
MIN_CLIPS = 5
MAX_WER = 0.25  # AC-8: WER ≤25% for local engine


def _normalize(text: str) -> list[str]:
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text, flags=re.UNICODE)
    return text.split()


def word_error_rate(ref: list[str], hyp: list[str]) -> float:
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


def _transcribe_clip(model, clip_path: Path) -> str:
    audio_bytes = clip_path.read_bytes()
    segments, _ = model.transcribe(io.BytesIO(audio_bytes), beam_size=5)
    return " ".join(seg.text.strip() for seg in segments).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Local Whisper benchmark (AC-8)")
    parser.add_argument("--model_path", default=None, help="CTranslate2 model directory")
    parser.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    parser.add_argument("--compute_type", default="int8")
    args = parser.parse_args()

    import os
    model_path = args.model_path or os.environ.get("LOCAL_MODEL_PATH")
    if not model_path:
        print("ERROR: set --model_path or LOCAL_MODEL_PATH env var", file=sys.stderr)
        return 1

    model_dir = Path(model_path)
    if not model_dir.exists():
        print(f"ERROR: model path not found: {model_dir}", file=sys.stderr)
        return 1

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("ERROR: faster-whisper not installed", file=sys.stderr)
        return 1

    print(f"Loading model from {model_dir} (device={args.device}, compute_type={args.compute_type})...")
    model = WhisperModel(
        str(model_dir),
        device=args.device,
        compute_type=args.compute_type,
        local_files_only=True,
    )

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
        print(f"ERROR: found only {len(pairs)} clip(ref) pairs; need ≥{MIN_CLIPS}", file=sys.stderr)
        return 1

    print(f"\n{'clip':<30} {'WER':>6}  {'acurácia':>8}")
    print("-" * 50)

    wers: list[float] = []
    for clip_path, ref_path in pairs:
        ref_text = ref_path.read_text(encoding="utf-8").strip()
        ref_words = _normalize(ref_text)
        try:
            hyp_text = _transcribe_clip(model, clip_path)
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
    sys.exit(main())
