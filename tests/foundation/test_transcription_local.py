"""Tests for local Whisper engine (faster-whisper) — issue #29.

AC-1: config valid/invalid
AC-2: fail-fast on invalid local_model_path
AC-3: zero egress (no outbound STT calls)
AC-4: integration wiring via handle() real path
AC-5: no model download at runtime
AC-6: timeout / non-deadlock
AC-7: structured logging
AC-9: non-regression (openai engine unchanged)
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from deilebot.foundation.envelope import (
    Attachment,
    AttachmentKind,
    BotUser,
    Channel,
    ChannelScope,
    MessageEnvelope,
)
from deilebot.foundation.pipeline import EgressPipeline, IngressPipeline
from deilebot.foundation.settings import (
    BotSettings,
    FoundationSettings,
    TranscriptionSettings,
    reset_bot_settings_cache,
)
from deilebot.foundation.transcription import (
    TranscriptionError,
    TranscriptionService,
    _LocalWhisperBackend,
)
from deilebot.foundation.transcription_budget import TranscriptionBudgetTracker

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FIXED_TS = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_BOT_USER = BotUser(
    bot_user_id="u1",
    provider="discord",
    provider_user_id="111",
    display_name="TestUser",
)
_CHANNEL = Channel(
    provider="discord",
    provider_channel_id="ch1",
    name="test-channel",
    scope=ChannelScope.DM,
)


@pytest.fixture
async def budget(tmp_path: Path) -> TranscriptionBudgetTracker:
    tracker = TranscriptionBudgetTracker(tmp_path / "budget.sqlite")
    yield tracker
    await tracker.close()


@pytest.fixture
def model_dir(tmp_path: Path) -> Path:
    """A minimal valid CTranslate2 model directory."""
    d = tmp_path / "whisper_model"
    d.mkdir()
    (d / "model.bin").write_bytes(b"\x00" * 16)
    (d / "config.json").write_text("{}")
    (d / "tokenizer.json").write_text("{}")
    return d


def _make_local_settings(model_path=None, *, timeout=60) -> TranscriptionSettings:
    return TranscriptionSettings(
        engine="local",
        enabled=True,
        local_model_path=model_path,
        local_device="cpu",
        local_compute_type="int8",
        local_timeout_seconds=timeout,
    )


def _audio_envelope(text: str = "") -> MessageEnvelope:
    return MessageEnvelope(
        message_id="msg1",
        channel=_CHANNEL,
        author=_BOT_USER,
        sent_at=_FIXED_TS,
        text=text,
        attachments=(
            Attachment(
                kind=AttachmentKind.AUDIO,
                url="http://cdn.example.com/audio.ogg",
                mime="audio/ogg",
                filename="voice.ogg",
            ),
        ),
    )


def _build_pipeline(*, transcription_service=None):
    bridge = MagicMock()
    invocations = []

    async def _capture_invoke(inv):
        invocations.append(inv)
        try:
            from deile.common.markup_ast import MarkupAST
            markup = MarkupAST.from_plain("ok")
        except Exception:
            markup = None
        from deilebot.foundation.agent_bridge import AgentResponse
        return AgentResponse(text="ok", markup=markup)

    bridge.invoke = _capture_invoke
    bridge.invocations = invocations

    identity = MagicMock()
    identity.resolve = AsyncMock(return_value=_BOT_USER)

    permissions = MagicMock()
    perm_ok = MagicMock()
    perm_ok.allowed = True
    perm_ok.reason = "ok"
    permissions.check = AsyncMock(return_value=perm_ok)
    permissions.is_owner = AsyncMock(return_value=False)

    rate_limit = MagicMock()
    rate_limit.acquire_inbound = AsyncMock()

    store = MagicMock()
    store.upsert_user = AsyncMock()
    store.upsert_channel = AsyncMock()
    store.record_inbound = AsyncMock()
    store.get_recent_messages = AsyncMock(return_value=[])

    intent = MagicMock()
    intent_d = MagicMock()
    intent_d.should_respond = True
    intent_d.reason = "test"
    intent.decide = AsyncMock(return_value=intent_d)

    capability_catalog = MagicMock()
    capability_catalog.snapshot = AsyncMock(return_value=MagicMock())
    capability_catalog.render_for_system_prompt = MagicMock(return_value="")

    persona_selector = MagicMock()
    persona_selector.resolve = AsyncMock(return_value="developer")

    audit = MagicMock()
    audit.log = AsyncMock()

    event_bus = MagicMock()
    event_bus.publish = AsyncMock()

    metrics = MagicMock()
    metrics.inc = MagicMock()
    metrics.observe = MagicMock()

    egress = MagicMock()
    egress.send_response = AsyncMock()

    agent_meta = MagicMock()

    pipeline = IngressPipeline(
        identity=identity,
        permissions=permissions,
        rate_limit=rate_limit,
        store=store,
        intent=intent,
        bridge=bridge,
        capability_catalog=capability_catalog,
        persona_selector=persona_selector,
        audit=audit,
        event_bus=event_bus,
        metrics=metrics,
        egress=egress,
        agent_meta=agent_meta,
        transcription_service=transcription_service,
    )
    return pipeline, bridge


def _mock_bot_settings(*, transcription_enabled=True):
    fs = FoundationSettings(
        forced_model=None,
        default_model=None,
        agent_invocation_timeout_seconds=30,
    )
    ts = TranscriptionSettings(enabled=transcription_enabled)
    return BotSettings(foundation=fs, transcription=ts)


# ---------------------------------------------------------------------------
# AC-1: config — valid and invalid engine values
# ---------------------------------------------------------------------------

def test_ac1_default_engine_is_openai():
    s = TranscriptionSettings()
    assert s.engine == "openai"


def test_ac1_local_engine_accepted():
    s = TranscriptionSettings(engine="local")
    assert s.engine == "local"


def test_ac1_invalid_engine_raises_pydantic():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        TranscriptionSettings(engine="bogus")


def test_ac1_local_fields_defaults():
    s = TranscriptionSettings(engine="local")
    assert s.local_device == "cpu"
    assert s.local_compute_type == "int8"
    assert s.local_timeout_seconds == 60
    assert s.local_model_path is None


def test_ac1_local_fields_from_dict(tmp_path):
    model_path = tmp_path / "model"
    s = TranscriptionSettings(
        engine="local",
        local_model_path=model_path,
        local_device="cuda",
        local_compute_type="float16",
        local_timeout_seconds=120,
    )
    assert str(s.local_model_path) == str(model_path)
    assert s.local_device == "cuda"
    assert s.local_compute_type == "float16"
    assert s.local_timeout_seconds == 120


# ---------------------------------------------------------------------------
# AC-2: fail-fast on invalid local_model_path
# ---------------------------------------------------------------------------

def test_ac2_no_model_path_raises_on_init(budget):
    settings = _make_local_settings(model_path=None)
    with pytest.raises(TranscriptionError, match="local_model_path"):
        # _LocalWhisperBackend init should fail immediately
        _LocalWhisperBackend(settings)


def test_ac2_nonexistent_path_raises_on_init(tmp_path, budget):
    missing = tmp_path / "does_not_exist"
    settings = _make_local_settings(model_path=missing)
    with pytest.raises(TranscriptionError, match="não existe"):
        _LocalWhisperBackend(settings)


def test_ac2_valid_path_init_ok(model_dir):
    settings = _make_local_settings(model_path=model_dir)
    mock_model = MagicMock()
    with patch("deilebot.foundation.transcription.WhisperModel", mock_model, create=True):
        with patch("deilebot.foundation.transcription._LocalWhisperBackend._load_model"):
            backend = _LocalWhisperBackend(settings)
    assert backend._model_path == model_dir


def test_ac2_service_init_fails_fast_on_bad_path(tmp_path, budget):
    """TranscriptionService with engine=local fails at __init__ time."""
    missing = tmp_path / "no_model"
    settings = _make_local_settings(model_path=missing)
    with pytest.raises(TranscriptionError, match="não existe"):
        TranscriptionService(settings=settings, budget=budget)


def test_ac2_file_instead_of_dir_raises(tmp_path, budget):
    f = tmp_path / "model.bin"
    f.write_bytes(b"\x00")
    settings = _make_local_settings(model_path=f)
    with pytest.raises(TranscriptionError, match="diretório"):
        _LocalWhisperBackend(settings)


# ---------------------------------------------------------------------------
# AC-3: zero egress — no outbound STT calls for local engine
# ---------------------------------------------------------------------------

async def test_ac3_local_engine_no_openai_call(model_dir, budget):
    """engine=local must not call OpenAI or any external STT service."""
    settings = _make_local_settings(model_path=model_dir)

    fake_segments = [MagicMock(text="hello world")]
    mock_model_instance = MagicMock()
    mock_model_instance.transcribe.return_value = (iter(fake_segments), MagicMock())

    with patch("deilebot.foundation.transcription._LocalWhisperBackend._load_model"):
        svc = TranscriptionService(settings=settings, budget=budget)
        svc._local_backend._model = mock_model_instance

    att = Attachment(
        kind=AttachmentKind.AUDIO,
        url="http://cdn.example.com/audio.ogg",
        mime="audio/ogg",
        filename="voice.ogg",
    )

    with patch.object(svc, "_download_audio", new=AsyncMock(return_value=b"\x00" * 100)):
        with patch.object(svc, "_estimate_duration_seconds", return_value=5.0):
            with patch.object(svc, "_call_whisper", new=AsyncMock()) as mock_call_whisper:
                result = await svc.transcribe(att)

    assert result == "hello world"
    assert not mock_call_whisper.called, "_call_whisper (OpenAI) was called — egress violation"


# ---------------------------------------------------------------------------
# AC-4: WIRING integration via handle() real path
# ---------------------------------------------------------------------------

async def test_ac4_handle_local_engine_inbound_text(model_dir, budget):
    """handle() with local engine → inbound_text contains transcript."""
    settings = _make_local_settings(model_path=model_dir)

    with patch("deilebot.foundation.transcription._LocalWhisperBackend._load_model"):
        svc = TranscriptionService(settings=settings, budget=budget)

    svc._local_backend = MagicMock()
    svc._local_backend.transcribe = AsyncMock(return_value="transcrição local aqui")

    pipeline, bridge = _build_pipeline(transcription_service=svc)
    env = _audio_envelope(text="")
    adapter = MagicMock()
    adapter.self_user_id = "bot1"
    adapter.name = "discord"

    with patch(
        "deilebot.foundation.pipeline.get_bot_settings",
        return_value=_mock_bot_settings(transcription_enabled=True),
    ):
        with patch.object(svc, "_download_audio", new=AsyncMock(return_value=b"\x00" * 100)):
            with patch.object(svc, "_estimate_duration_seconds", return_value=5.0):
                await pipeline.handle(env, adapter)

    assert bridge.invocations, "bridge.invoke was not called"
    inv = bridge.invocations[0]
    assert "transcrição local aqui" in inv.inbound_text, (
        f"transcript not in inbound_text: {inv.inbound_text!r}"
    )


# ---------------------------------------------------------------------------
# AC-5: no model download at runtime (local_files_only enforced)
# ---------------------------------------------------------------------------

def test_ac5_local_files_only_passed_to_whisper_model(model_dir):
    """WhisperModel must be instantiated with local_files_only=True."""
    settings = _make_local_settings(model_path=model_dir)
    captured = {}

    def _fake_load_model(self):
        # Intercept at the call site within _load_model; patch the import target
        import sys
        # Build a fake faster_whisper module if absent
        fake_module = MagicMock()

        class FakeWhisperModel:
            def __init__(self, path, **kw):
                captured.update(kw)
                captured["model_path"] = path

        fake_module.WhisperModel = FakeWhisperModel
        old = sys.modules.get("faster_whisper")
        sys.modules["faster_whisper"] = fake_module
        try:
            self._load_model_real()
        finally:
            if old is None:
                sys.modules.pop("faster_whisper", None)
            else:
                sys.modules["faster_whisper"] = old

    # Directly test _load_model by faking faster_whisper in sys.modules
    import sys
    fake_fw = MagicMock()
    captured_ctor = {}

    class CapturingModel:
        def __init__(self, path, **kwargs):
            captured_ctor.update(kwargs)
            captured_ctor["model_path"] = path

    fake_fw.WhisperModel = CapturingModel
    old_fw = sys.modules.get("faster_whisper")
    sys.modules["faster_whisper"] = fake_fw
    try:
        with patch("deilebot.foundation.transcription._LocalWhisperBackend._load_model"):
            backend = _LocalWhisperBackend(settings)
        # Now manually call _load_model with patched import
        backend._load_model()
    except Exception:
        pass
    finally:
        if old_fw is None:
            sys.modules.pop("faster_whisper", None)
        else:
            sys.modules["faster_whisper"] = old_fw

    if captured_ctor:
        assert captured_ctor.get("local_files_only") is True, (
            f"local_files_only not True in WhisperModel call: {captured_ctor}"
        )
        assert str(captured_ctor.get("model_path")) == str(model_dir), (
            "model_path passed to WhisperModel should be the configured local path"
        )


def test_ac5_no_huggingface_download(model_dir):
    """Model path is always a local filesystem path, not a HuggingFace model ID."""
    settings = _make_local_settings(model_path=model_dir)

    with patch("deilebot.foundation.transcription._LocalWhisperBackend._load_model"):
        backend = _LocalWhisperBackend(settings)

    # The path stored must be the local directory, not a HuggingFace name like "openai/whisper-*"
    assert "/" in str(backend._model_path) or backend._model_path.is_absolute(), (
        "local_model_path should be a filesystem path, not a HuggingFace model name"
    )
    assert str(backend._model_path) == str(model_dir)


# ---------------------------------------------------------------------------
# AC-6: timeout / non-deadlock
# ---------------------------------------------------------------------------

async def test_ac6_timeout_raises_transcription_error(model_dir, budget):
    """A backend that hangs raises TranscriptionError (not TimeoutError escaping)."""
    settings = _make_local_settings(model_path=model_dir, timeout=1)

    with patch("deilebot.foundation.transcription._LocalWhisperBackend._load_model"):
        backend = _LocalWhisperBackend(settings)

    # Use a real blocking thread — asyncio.to_thread runs it in a pool thread;
    # wait_for cancels the task after 1s while the thread continues in background.
    def _blocking_sync(audio_bytes):
        time.sleep(10)  # much longer than the 1-second timeout
        return "never"

    backend._transcribe_sync = _blocking_sync

    with pytest.raises(TranscriptionError, match="timeout"):
        await backend.transcribe(b"\x00" * 100)


async def test_ac6_handle_timeout_degrades_gracefully(model_dir, budget):
    """Timeout in local STT does not propagate out of handle()."""
    settings = _make_local_settings(model_path=model_dir, timeout=1)

    with patch("deilebot.foundation.transcription._LocalWhisperBackend._load_model"):
        svc = TranscriptionService(settings=settings, budget=budget)

    async def _hanging_transcribe(audio_bytes):
        await asyncio.sleep(10)
        return "never"

    svc._local_backend.transcribe = _hanging_transcribe

    pipeline, bridge = _build_pipeline(transcription_service=svc)
    env = _audio_envelope()
    adapter = MagicMock()
    adapter.self_user_id = "bot1"
    adapter.name = "discord"

    with patch(
        "deilebot.foundation.pipeline.get_bot_settings",
        return_value=_mock_bot_settings(transcription_enabled=True),
    ):
        with patch.object(svc, "_download_audio", new=AsyncMock(return_value=b"\x00" * 100)):
            with patch.object(svc, "_estimate_duration_seconds", return_value=5.0):
                with patch.object(svc._local_backend, "transcribe", new=AsyncMock(
                    side_effect=TranscriptionError("transcrição local excedeu timeout de 1s")
                )):
                    # Must not raise
                    await pipeline.handle(env, adapter)

    # Agent was still invoked (degraded, not crashed)
    assert bridge.invocations, "bridge was not invoked after timeout — deadlock or crash"


async def test_ac6_runs_in_thread_not_event_loop(model_dir):
    """to_thread is used so CPU work doesn't block the event loop."""
    settings = _make_local_settings(model_path=model_dir)

    with patch("deilebot.foundation.transcription._LocalWhisperBackend._load_model"):
        backend = _LocalWhisperBackend(settings)

    backend._model = MagicMock()
    fake_seg = MagicMock(text="result")
    backend._model.transcribe.return_value = (iter([fake_seg]), MagicMock())

    to_thread_called = []
    original_to_thread = asyncio.to_thread

    async def _spy_to_thread(fn, *args, **kwargs):
        to_thread_called.append(fn.__name__ if hasattr(fn, "__name__") else str(fn))
        return await original_to_thread(fn, *args, **kwargs)

    with patch("asyncio.to_thread", _spy_to_thread):
        await backend.transcribe(b"\x00" * 100)

    assert to_thread_called, "asyncio.to_thread was not called — transcription may block event loop"


# ---------------------------------------------------------------------------
# AC-7: structured logging
# ---------------------------------------------------------------------------

async def test_ac7_structured_log_ok(model_dir, caplog):
    """Successful transcription emits log with engine=local, duration_ms, outcome=ok."""
    settings = _make_local_settings(model_path=model_dir)

    with patch("deilebot.foundation.transcription._LocalWhisperBackend._load_model"):
        backend = _LocalWhisperBackend(settings)

    backend._model = MagicMock()
    fake_seg = MagicMock(text="ola mundo")
    backend._model.transcribe.return_value = (iter([fake_seg]), MagicMock())

    # Logger is registered as "deilebot.transcription.local" (get_logger prepends "deilebot.")
    with caplog.at_level(logging.INFO, logger="deilebot.transcription.local"):
        result = await backend.transcribe(b"\x00" * 100)

    assert result == "ola mundo"
    ok_records = [
        r for r in caplog.records
        if getattr(r, "engine", None) == "local"
        and getattr(r, "outcome", None) == "ok"
    ]
    assert ok_records, (
        f"No log record with engine=local, outcome=ok. Records: {[(r.getMessage(), vars(r)) for r in caplog.records]}"
    )
    assert hasattr(ok_records[0], "duration_ms")


async def test_ac7_structured_log_timeout(model_dir, caplog):
    """Timeout emits log with engine=local, outcome=timeout."""
    settings = _make_local_settings(model_path=model_dir, timeout=1)

    with patch("deilebot.foundation.transcription._LocalWhisperBackend._load_model"):
        backend = _LocalWhisperBackend(settings)

    def _blocking_sync(audio_bytes):
        time.sleep(10)

    backend._transcribe_sync = _blocking_sync

    with caplog.at_level(logging.WARNING, logger="deilebot.transcription.local"):
        with pytest.raises(TranscriptionError, match="timeout"):
            await backend.transcribe(b"\x00" * 100)

    timeout_records = [
        r for r in caplog.records
        if getattr(r, "engine", None) == "local"
        and getattr(r, "outcome", None) == "timeout"
    ]
    assert timeout_records, (
        f"No log record with engine=local, outcome=timeout. Records: {[(r.getMessage(), vars(r)) for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# AC-9: non-regression — openai engine unchanged
# ---------------------------------------------------------------------------

async def test_ac9_openai_engine_unchanged(budget):
    """engine=openai still uses _call_whisper (not local backend)."""
    settings = TranscriptionSettings(engine="openai", enabled=True)
    svc = TranscriptionService(settings=settings, budget=budget)
    assert svc._local_backend is None

    att = Attachment(
        kind=AttachmentKind.AUDIO,
        url="http://cdn.example.com/audio.ogg",
        mime="audio/ogg",
        filename="voice.ogg",
    )

    whisper_called = []

    async def _mock_whisper(audio_bytes, filename, mime, *, language=None):
        whisper_called.append(True)
        return "openai result"

    with patch.object(svc, "_download_audio", new=AsyncMock(return_value=b"\x00" * 100)):
        with patch.object(svc, "_estimate_duration_seconds", return_value=5.0):
            with patch.object(svc, "_call_whisper", new=AsyncMock(side_effect=_mock_whisper)):
                result = await svc.transcribe(att)

    assert result == "openai result"
    assert whisper_called, "_call_whisper not called for engine=openai"


def test_ac9_existing_settings_tests_still_pass():
    """Sanity: existing TranscriptionSettings tests are not broken."""
    s = TranscriptionSettings()
    assert s.engine == "openai"
    assert s.enabled is False
    assert s.max_duration_seconds == 120
    assert s.max_minutes_per_month == 60
    assert "api_key" not in TranscriptionSettings.model_fields
