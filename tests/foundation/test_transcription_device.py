"""Tests for issue #35: GPU toggle + device logging (AC-G1, AC-G2).

AC-G1: campo `device` emitido no log estruturado ao carregar o modelo.
AC-G2: local_cuda_fallback=fail levanta CUDAUnavailableError; =cpu faz fallback e emite WARN.

CPU-only — roda em CI sem GPU (CUDA é mockada).
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from deilebot.foundation.settings import TranscriptionSettings
from deilebot.foundation.transcription import (
    CUDAUnavailableError,
    TranscriptionError,
    _LocalWhisperBackend,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def model_dir(tmp_path: Path) -> Path:
    """Minimal valid CTranslate2 model directory."""
    d = tmp_path / "whisper_model"
    d.mkdir()
    (d / "model.bin").write_bytes(b"\x00" * 16)
    (d / "config.json").write_text("{}")
    return d


def _settings(device: str = "cpu", fallback: str = "fail") -> TranscriptionSettings:
    return TranscriptionSettings(
        engine="local",
        enabled=True,
        local_model_path=None,  # overridden per test
        local_device=device,  # type: ignore[arg-type]
        local_cuda_fallback=fallback,  # type: ignore[arg-type]
    )


def _make_fake_whisper():
    """Return a fake faster_whisper module with a capturing WhisperModel."""
    fake_fw = MagicMock()
    captured: dict = {}

    class CapturingModel:
        def __init__(self, path, **kwargs):
            captured["path"] = path
            captured.update(kwargs)

    fake_fw.WhisperModel = CapturingModel
    return fake_fw, captured


# ---------------------------------------------------------------------------
# AC-G1: campo `device` no log ao carregar o modelo
# ---------------------------------------------------------------------------


def test_acg1_device_cpu_logged_on_model_load(model_dir, caplog):
    """device=cpu emitido no log ao carregar o modelo."""
    settings = _settings(device="cpu")
    settings.local_model_path = model_dir

    fake_fw, _ = _make_fake_whisper()
    old = sys.modules.get("faster_whisper")
    sys.modules["faster_whisper"] = fake_fw
    try:
        with caplog.at_level(logging.INFO, logger="deilebot.transcription.local"):
            backend = _LocalWhisperBackend(settings)
    finally:
        if old is None:
            sys.modules.pop("faster_whisper", None)
        else:
            sys.modules["faster_whisper"] = old

    device_records = [
        r for r in caplog.records
        if getattr(r, "device", None) == "cpu"
    ]
    assert device_records, (
        f"Nenhum log com device=cpu encontrado. Records: "
        f"{[(r.getMessage(), {k: getattr(r, k, None) for k in ('device',)}) for r in caplog.records]}"
    )


def test_acg1_device_field_value_matches_configured_device(model_dir, caplog):
    """O valor do campo `device` no log deve refletir o device efetivamente usado."""
    settings = _settings(device="cpu")
    settings.local_model_path = model_dir

    fake_fw, _ = _make_fake_whisper()
    old = sys.modules.get("faster_whisper")
    sys.modules["faster_whisper"] = fake_fw
    try:
        with caplog.at_level(logging.INFO, logger="deilebot.transcription.local"):
            backend = _LocalWhisperBackend(settings)
    finally:
        if old is None:
            sys.modules.pop("faster_whisper", None)
        else:
            sys.modules["faster_whisper"] = old

    # When cpu is configured, device field must be "cpu"
    device_records = [r for r in caplog.records if hasattr(r, "device")]
    assert device_records, "Nenhum log com campo device encontrado"
    assert device_records[0].device == "cpu"


def test_acg1_device_cuda_logged_when_cuda_available(model_dir, caplog):
    """Com local_device=cuda e CUDA disponível, device=cuda no log."""
    settings = _settings(device="cuda", fallback="fail")
    settings.local_model_path = model_dir

    fake_fw, _ = _make_fake_whisper()
    old = sys.modules.get("faster_whisper")
    sys.modules["faster_whisper"] = fake_fw
    try:
        with caplog.at_level(logging.INFO, logger="deilebot.transcription.local"):
            # Mock CUDA as available
            with patch.object(_LocalWhisperBackend, "_is_cuda_available", return_value=True):
                backend = _LocalWhisperBackend(settings)
    finally:
        if old is None:
            sys.modules.pop("faster_whisper", None)
        else:
            sys.modules["faster_whisper"] = old

    device_records = [r for r in caplog.records if getattr(r, "device", None) == "cuda"]
    assert device_records, (
        f"Nenhum log com device=cuda. Records: {[(r.getMessage(), vars(r)) for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# AC-G2: local_cuda_fallback modes with CUDA unavailable
# ---------------------------------------------------------------------------


def test_acg2_cuda_unavailable_fallback_fail_raises(model_dir):
    """local_cuda_fallback=fail (default) levanta CUDAUnavailableError quando CUDA indisponível."""
    settings = _settings(device="cuda", fallback="fail")
    settings.local_model_path = model_dir

    fake_fw, _ = _make_fake_whisper()
    old = sys.modules.get("faster_whisper")
    sys.modules["faster_whisper"] = fake_fw
    try:
        with patch.object(_LocalWhisperBackend, "_is_cuda_available", return_value=False):
            with pytest.raises(CUDAUnavailableError, match="CUDA indisponível"):
                _LocalWhisperBackend(settings)
    finally:
        if old is None:
            sys.modules.pop("faster_whisper", None)
        else:
            sys.modules["faster_whisper"] = old


def test_acg2_cuda_unavailable_fallback_cpu_continues(model_dir, caplog):
    """local_cuda_fallback=cpu faz fallback silencioso para CPU e emite WARN com device_fallback."""
    settings = _settings(device="cuda", fallback="cpu")
    settings.local_model_path = model_dir

    fake_fw, captured = _make_fake_whisper()
    old = sys.modules.get("faster_whisper")
    sys.modules["faster_whisper"] = fake_fw
    try:
        with caplog.at_level(logging.WARNING, logger="deilebot.transcription.local"):
            with patch.object(_LocalWhisperBackend, "_is_cuda_available", return_value=False):
                backend = _LocalWhisperBackend(settings)
    finally:
        if old is None:
            sys.modules.pop("faster_whisper", None)
        else:
            sys.modules["faster_whisper"] = old

    # WhisperModel must have been called with device=cpu (fallback)
    assert captured.get("device") == "cpu", (
        f"WhisperModel deve ser chamado com device=cpu após fallback, got: {captured}"
    )
    # WARN log with device_fallback field
    warn_records = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING
        and getattr(r, "device_fallback", None) == "cuda->cpu"
    ]
    assert warn_records, (
        f"Nenhum WARN com device_fallback=cuda->cpu. Records: "
        f"{[(r.getMessage(), {k: getattr(r, k, None) for k in ('device_fallback',)}) for r in caplog.records]}"
    )


def test_acg2_cuda_unavailable_fallback_cpu_effective_device_is_cpu(model_dir):
    """Após fallback para CPU, effective_device deve ser 'cpu'."""
    settings = _settings(device="cuda", fallback="cpu")
    settings.local_model_path = model_dir

    fake_fw, _ = _make_fake_whisper()
    old = sys.modules.get("faster_whisper")
    sys.modules["faster_whisper"] = fake_fw
    try:
        with patch.object(_LocalWhisperBackend, "_is_cuda_available", return_value=False):
            backend = _LocalWhisperBackend(settings)
    finally:
        if old is None:
            sys.modules.pop("faster_whisper", None)
        else:
            sys.modules["faster_whisper"] = old

    assert backend._effective_device == "cpu"


def test_acg2_fallback_fail_is_default():
    """local_cuda_fallback default deve ser 'fail'."""
    s = TranscriptionSettings()
    assert s.local_cuda_fallback == "fail"


def test_acg2_fallback_cpu_accepted():
    """local_cuda_fallback='cpu' é valor válido."""
    s = TranscriptionSettings(local_cuda_fallback="cpu")
    assert s.local_cuda_fallback == "cpu"


def test_acg2_invalid_fallback_raises():
    """Valor inválido de local_cuda_fallback levanta ValidationError."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        TranscriptionSettings(local_cuda_fallback="ignore")  # type: ignore[arg-type]


def test_acg2_cpu_device_skips_cuda_check(model_dir):
    """Com local_device=cpu, _is_cuda_available não é chamado."""
    settings = _settings(device="cpu", fallback="fail")
    settings.local_model_path = model_dir

    fake_fw, _ = _make_fake_whisper()
    old = sys.modules.get("faster_whisper")
    sys.modules["faster_whisper"] = fake_fw
    try:
        with patch.object(
            _LocalWhisperBackend, "_is_cuda_available", return_value=False
        ) as mock_check:
            backend = _LocalWhisperBackend(settings)
    finally:
        if old is None:
            sys.modules.pop("faster_whisper", None)
        else:
            sys.modules["faster_whisper"] = old

    mock_check.assert_not_called()
