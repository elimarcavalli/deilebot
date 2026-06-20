#!/usr/bin/env python3
"""Benchmark RTF (Real-Time Factor) do engine local faster-whisper — issue #35 AC-S1/AC-S4.

# FORA DO CI — NÃO DETERMINÍSTICO — DEPENDENTE DE HARDWARE
# Rodar local/manual; não entra no CI (saída varia por CPU/GPU e modelo).

Computa p50/p95 de RTF (real-time factor = tempo_processamento / duração_áudio) por device.

Uso:
    LOCAL_MODEL_PATH=/path/to/ctranslate2-model python3 scripts/bench_transcription_local.py
    LOCAL_MODEL_PATH=/path/to/model python3 scripts/bench_transcription_local.py --device cuda

Clipe de referência do SLO:
    Usa os fixtures nomeados de tests/fixtures/transcription/clips/ (de #23 AC-2).
    Cada clipe deve ter duração, formato e sample-rate documentados no README dos fixtures.

Saída: p50/p95 RTF por device.
"""
from __future__ import annotations

import argparse
import io
import os
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
CLIPS_DIR = REPO_ROOT / "tests" / "fixtures" / "transcription" / "clips"
MIN_CLIPS = 5


def _transcribe_rtf(model, clip_path: Path) -> tuple[float, float]:
    """Return (duration_audio_s, processing_s) for a clip."""
    audio_bytes = clip_path.read_bytes()

    # Estimate duration via mutagen; fall back to size/bitrate
    duration_s: float
    try:
        import mutagen  # type: ignore[import]
        f = mutagen.File(io.BytesIO(audio_bytes))
        if f is not None and f.info is not None:
            duration_s = float(f.info.length)
        else:
            raise ValueError("mutagen returned None")
    except Exception:
        # 128 kbps fallback
        duration_s = len(audio_bytes) * 8 / 128_000

    t0 = time.monotonic()
    segments, _ = model.transcribe(io.BytesIO(audio_bytes), beam_size=5)
    _ = list(segments)  # consume iterator
    processing_s = time.monotonic() - t0

    return duration_s, processing_s


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_v = sorted(values)
    idx = (len(sorted_v) - 1) * p / 100
    lo = int(idx)
    hi = min(lo + 1, len(sorted_v) - 1)
    return sorted_v[lo] + (sorted_v[hi] - sorted_v[lo]) * (idx - lo)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="RTF benchmark do engine local (AC-S1/AC-S4 — fora do CI)"
    )
    parser.add_argument("--model_path", default=None, help="Diretório CTranslate2")
    parser.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    parser.add_argument("--compute_type", default="int8")
    parser.add_argument(
        "--local_timeout_seconds",
        type=int,
        default=60,
        help="Valor de local_timeout_seconds configurado (para validar desigualdade AC-S2)",
    )
    parser.add_argument(
        "--max_duration_seconds",
        type=int,
        default=120,
        help="Valor de max_duration_seconds configurado (para validar desigualdade AC-S2)",
    )
    args = parser.parse_args()

    model_path = args.model_path or os.environ.get("LOCAL_MODEL_PATH")
    if not model_path:
        print("ERROR: defina --model_path ou LOCAL_MODEL_PATH", file=sys.stderr)
        return 1

    model_dir = Path(model_path)
    if not model_dir.exists():
        print(f"ERROR: model path não existe: {model_dir}", file=sys.stderr)
        return 1

    try:
        from faster_whisper import WhisperModel  # type: ignore[import]
    except ImportError:
        print("ERROR: faster-whisper não instalado", file=sys.stderr)
        return 1

    if not CLIPS_DIR.exists():
        print(f"ERROR: diretório de clips não existe: {CLIPS_DIR}", file=sys.stderr)
        print("Adicione ≥5 clipes — ver tests/fixtures/transcription/README.md", file=sys.stderr)
        return 1

    clips = sorted(
        c for c in CLIPS_DIR.iterdir()
        if c.suffix not in (".txt", ".md") and not c.name.startswith(".")
    )
    if len(clips) < MIN_CLIPS:
        print(f"ERROR: apenas {len(clips)} clips; precisam de ≥{MIN_CLIPS}", file=sys.stderr)
        return 1

    print(
        f"Carregando modelo de {model_dir} "
        f"(device={args.device}, compute_type={args.compute_type})..."
    )
    model = WhisperModel(
        str(model_dir),
        device=args.device,
        compute_type=args.compute_type,
        local_files_only=True,
    )

    print(f"\n{'clip':<35} {'dur_s':>6} {'proc_s':>7} {'RTF':>6}")
    print("-" * 60)

    rtf_values: list[float] = []
    for clip_path in clips:
        try:
            dur_s, proc_s = _transcribe_rtf(model, clip_path)
        except Exception as e:
            print(f"{clip_path.name:<35} ERROR: {e}")
            continue
        rtf = proc_s / dur_s if dur_s > 0 else 0.0
        rtf_values.append(rtf)
        print(f"{clip_path.name:<35} {dur_s:>6.1f} {proc_s:>7.3f} {rtf:>6.3f}")

    if not rtf_values:
        print("\nERROR: nenhum clip transcrito com sucesso")
        return 1

    p50 = _percentile(rtf_values, 50)
    p95 = _percentile(rtf_values, 95)

    print("-" * 60)
    print(f"{'p50 RTF':<35} {p50:>6.3f}")
    print(f"{'p95 RTF':<35} {p95:>6.3f}")
    print()

    # AC-S2: validar desigualdade derivada p95_RTF × max_duration_seconds ≤ local_timeout_seconds
    # AC-S3: computar duração máxima recomendada = floor(local_timeout_seconds / p95_RTF)
    timeout_s = args.local_timeout_seconds
    max_dur = args.max_duration_seconds
    derived_timeout_needed = p95 * max_dur

    print(f"=== Validação SLO (AC-S2) ===")
    print(f"  p95_RTF × max_duration_seconds = {p95:.3f} × {max_dur}s = {derived_timeout_needed:.1f}s")
    print(f"  local_timeout_seconds configurado = {timeout_s}s")

    if derived_timeout_needed <= timeout_s:
        print(f"  STATUS: OK — desigualdade satisfeita ({derived_timeout_needed:.1f}s ≤ {timeout_s}s)")
        slo_ok = True
    else:
        recomendado = int(derived_timeout_needed) + 1
        print(
            f"  STATUS: FALHA — desigualdade NÃO satisfeita ({derived_timeout_needed:.1f}s > {timeout_s}s)"
        )
        print(f"  RECOMENDAÇÃO: aumente local_timeout_seconds para ≥{recomendado}s")
        print(f"               ou reduza max_duration_seconds para ≤{int(timeout_s / p95)}s")
        slo_ok = False

    if p95 > 0:
        max_recomendado = int(timeout_s / p95)
        print()
        print(f"=== Mapeamento SLO → config (AC-S3) ===")
        print(f"  device={args.device}: duração máxima recomendada = floor({timeout_s} / {p95:.3f}) = {max_recomendado}s")
        print(f"  (equivale a max_duration_seconds ≤ {max_recomendado}s com local_timeout_seconds={timeout_s}s)")

    return 0 if slo_ok else 1


if __name__ == "__main__":
    sys.exit(main())
