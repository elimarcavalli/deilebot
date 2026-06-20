"""Transcription observability metrics — AC-1 through AC-5.

AC-1: 3 métricas registradas (nomes/labels/tipos corretos)
AC-2: fiação via handle() — delta ok/error/rejected
AC-3: re-seed do gauge após restart (lê SQLite)
AC-4: rollover mensal (mês M não aparece no gauge do mês M+1)
AC-5: boundaries do histogram definidos p/ transcrição (topo ≥ max_duration_seconds)
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from deilebot.foundation.envelope import (
    Attachment,
    AttachmentKind,
    BotUser,
    Channel,
    ChannelScope,
    MessageEnvelope,
)
from deilebot.foundation.metrics import MetricsCollector
from deilebot.foundation.pipeline import IngressPipeline
from deilebot.foundation.settings import TranscriptionSettings
from deilebot.foundation.transcription import (
    TranscriptionError,
    TranscriptionRejectedError,
    TranscriptionService,
)
from deilebot.foundation.transcription_budget import TranscriptionBudgetTracker


# ── fixtures ──────────────────────────────────────────────────────────────────

_FIXED_TS = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
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


def _audio_envelope() -> MessageEnvelope:
    return MessageEnvelope(
        message_id="msg1",
        channel=_CHANNEL,
        author=_BOT_USER,
        sent_at=_FIXED_TS,
        text="",
        attachments=(
            Attachment(
                kind=AttachmentKind.AUDIO,
                url="http://cdn.example.com/audio.ogg",
                mime="audio/ogg",
                filename="voice.ogg",
            ),
        ),
    )


def _mock_intent(should_respond: bool = True):
    d = MagicMock()
    d.should_respond = should_respond
    d.reason = "test"
    return d


def _mock_permission(allowed: bool = True):
    d = MagicMock()
    d.allowed = allowed
    d.reason = "ok"
    return d


def _build_pipeline(*, transcription_service=None) -> tuple[IngressPipeline, MetricsCollector]:
    bridge = MagicMock()

    async def _capture_invoke(inv):
        try:
            from deile.common.markup_ast import MarkupAST
            markup = MarkupAST.from_plain("ok")
        except Exception:
            markup = None
        from deilebot.foundation.agent_bridge import AgentResponse
        return AgentResponse(text="ok", markup=markup)

    bridge.invoke = _capture_invoke

    identity = MagicMock()
    identity.resolve = AsyncMock(return_value=_BOT_USER)

    permissions = MagicMock()
    permissions.check = AsyncMock(return_value=_mock_permission())
    permissions.is_owner = AsyncMock(return_value=False)

    rate_limit = MagicMock()
    rate_limit.acquire_inbound = AsyncMock()

    store = MagicMock()
    store.upsert_user = AsyncMock()
    store.upsert_channel = AsyncMock()
    store.record_inbound = AsyncMock()
    store.get_recent_messages = AsyncMock(return_value=[])

    intent = MagicMock()
    intent.decide = AsyncMock(return_value=_mock_intent())

    capability_catalog = MagicMock()
    capability_catalog.snapshot = AsyncMock(return_value=MagicMock())
    capability_catalog.render_for_system_prompt = MagicMock(return_value="")

    persona_selector = MagicMock()
    persona_selector.resolve = AsyncMock(return_value="developer")

    audit = MagicMock()
    audit.log = AsyncMock()

    event_bus = MagicMock()
    event_bus.publish = AsyncMock()

    metrics = MetricsCollector()

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
    return pipeline, metrics


def _mock_svc(*, transcribe_result=None, transcribe_error=None, used_minutes: float = 0.0):
    svc = MagicMock(spec=TranscriptionService)
    svc._settings = TranscriptionSettings(enabled=True, max_duration_seconds=120, max_minutes_per_month=60)
    if transcribe_error is not None:
        svc.transcribe = AsyncMock(side_effect=transcribe_error)
    else:
        svc.transcribe = AsyncMock(return_value=transcribe_result or "hello")
    svc.get_used_minutes = AsyncMock(return_value=used_minutes)
    return svc


# ── AC-1: métricas registradas (nomes/labels/tipos) ──────────────────────────

async def test_ac1_counter_name_and_label_ok():
    """AC-1: bot_transcription_total{status=ok} é um counter registrado."""
    svc = _mock_svc(transcribe_result="transcript", used_minutes=0.5)
    pipeline, metrics = _build_pipeline(transcription_service=svc)
    with MagicMock() as _patch:
        await pipeline.handle(_audio_envelope(), MagicMock(self_user_id="bot1", name="discord"))

    snap = metrics.snapshot()
    assert "bot_transcription_total" in snap["counters"], "counter não registrado"
    series = {s["labels"].get("status"): s["value"] for s in snap["counters"]["bot_transcription_total"]}
    assert series.get("ok") == 1, f"status=ok esperado=1, got {series}"


async def test_ac1_histogram_name():
    """AC-1: bot_transcription_seconds é um histogram registrado."""
    svc = _mock_svc(transcribe_result="transcript", used_minutes=1.0)
    pipeline, metrics = _build_pipeline(transcription_service=svc)
    await pipeline.handle(_audio_envelope(), MagicMock(self_user_id="bot1", name="discord"))

    snap = metrics.snapshot()
    assert "bot_transcription_seconds" in snap["histograms"], "histogram não registrado"
    hist = snap["histograms"]["bot_transcription_seconds"]
    assert len(hist) == 1 and hist[0]["count"] == 1


async def test_ac1_gauge_name():
    """AC-1: bot_transcription_minutes_consumed_month é um gauge registrado."""
    svc = _mock_svc(transcribe_result="transcript", used_minutes=3.7)
    pipeline, metrics = _build_pipeline(transcription_service=svc)
    await pipeline.handle(_audio_envelope(), MagicMock(self_user_id="bot1", name="discord"))

    snap = metrics.snapshot()
    assert "bot_transcription_minutes_consumed_month" in snap["gauges"], "gauge não registrado"
    val = snap["gauges"]["bot_transcription_minutes_consumed_month"][0]["value"]
    assert abs(val - 3.7) < 0.01, f"gauge esperado=3.7, got {val}"


# ── AC-2: fiação via handle() — delta ok/error/rejected ──────────────────────

async def test_ac2_handle_ok_increments_counter_and_gauge():
    """AC-2: após handle() com STT ok, counter +1 e gauge = minutos usados."""
    svc = _mock_svc(transcribe_result="olá mundo", used_minutes=5.0)
    pipeline, metrics = _build_pipeline(transcription_service=svc)

    await pipeline.handle(_audio_envelope(), MagicMock(self_user_id="bot1", name="discord"))

    snap = metrics.snapshot()
    series = {s["labels"].get("status"): s["value"] for s in snap["counters"]["bot_transcription_total"]}
    assert series.get("ok") == 1
    gauge_val = snap["gauges"]["bot_transcription_minutes_consumed_month"][0]["value"]
    assert abs(gauge_val - 5.0) < 0.01


async def test_ac2_handle_error_increments_error_counter():
    """AC-2: STT error → status=error counter +1, sem ok."""
    svc = _mock_svc(transcribe_error=TranscriptionError("network fail"))
    pipeline, metrics = _build_pipeline(transcription_service=svc)

    await pipeline.handle(_audio_envelope(), MagicMock(self_user_id="bot1", name="discord"))

    snap = metrics.snapshot()
    series = {s["labels"].get("status"): s["value"] for s in snap["counters"]["bot_transcription_total"]}
    assert series.get("error") == 1
    assert "ok" not in series


async def test_ac2_handle_rejected_increments_rejected_counter():
    """AC-2: budget rejected → status=rejected counter +1, sem ok."""
    svc = _mock_svc(transcribe_error=TranscriptionRejectedError("teto atingido"))
    pipeline, metrics = _build_pipeline(transcription_service=svc)

    await pipeline.handle(_audio_envelope(), MagicMock(self_user_id="bot1", name="discord"))

    snap = metrics.snapshot()
    series = {s["labels"].get("status"): s["value"] for s in snap["counters"]["bot_transcription_total"]}
    assert series.get("rejected") == 1
    assert "ok" not in series
    assert "error" not in series


# ── AC-3: re-seed do gauge após restart (lê SQLite) ──────────────────────────

async def test_ac3_seed_transcription_metrics_reads_sqlite():
    """AC-3: seed_transcription_metrics() lê SQLite e define o gauge."""
    svc = _mock_svc(used_minutes=42.5)
    pipeline, metrics = _build_pipeline(transcription_service=svc)

    await pipeline.seed_transcription_metrics()

    snap = metrics.snapshot()
    assert "bot_transcription_minutes_consumed_month" in snap["gauges"]
    val = snap["gauges"]["bot_transcription_minutes_consumed_month"][0]["value"]
    assert abs(val - 42.5) < 0.01, f"gauge após seed esperado=42.5, got {val}"


async def test_ac3_seed_called_once_on_first_handle():
    """AC-3: handle() invoca seed na primeira chamada, não na segunda."""
    svc = _mock_svc(transcribe_result="t", used_minutes=10.0)
    pipeline, metrics = _build_pipeline(transcription_service=svc)

    env = _audio_envelope()
    adapter = MagicMock(self_user_id="bot1", name="discord")
    await pipeline.handle(env, adapter)
    await pipeline.handle(env, adapter)

    # get_used_minutes chamado: 1x seed + 2x após ok = 3
    assert svc.get_used_minutes.call_count == 3


async def test_ac3_real_sqlite_seed():
    """AC-3: com SQLite real, gauge é semeado com N minutos do mês corrente."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "budget.db"
        settings = TranscriptionSettings(enabled=True, max_duration_seconds=120, max_minutes_per_month=60)
        budget = TranscriptionBudgetTracker(db_path)

        month_key = TranscriptionBudgetTracker._month_key()
        db = await budget._get_db()
        await db.execute(
            "INSERT INTO transcription_budget (month_key, used_minutes) VALUES (?, ?)",
            (month_key, 5.0),
        )
        await db.commit()

        real_svc = TranscriptionService(settings=settings, budget=budget)
        svc_wrapper = MagicMock(spec=TranscriptionService)
        svc_wrapper._settings = settings
        svc_wrapper.get_used_minutes = real_svc.get_used_minutes

        pipeline, metrics = _build_pipeline(transcription_service=svc_wrapper)
        await pipeline.seed_transcription_metrics()

        snap = metrics.snapshot()
        val = snap["gauges"]["bot_transcription_minutes_consumed_month"][0]["value"]
        assert abs(val - 5.0) < 0.01, f"gauge após seed real esperado=5.0, got {val}"
        await budget.close()


# ── AC-4: rollover mensal — mês M não aparece no mês M+1 ──────────────────────

async def test_ac4_gauge_rollover_reads_current_month_only():
    """AC-4: gauge lê mês corrente via SQLite; minutos de outro mês não aparecem."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "budget.db"
        settings = TranscriptionSettings(enabled=True, max_duration_seconds=120, max_minutes_per_month=60)
        budget = TranscriptionBudgetTracker(db_path)

        db = await budget._get_db()
        await db.execute(
            "INSERT INTO transcription_budget (month_key, used_minutes) VALUES (?, ?)",
            ("2024-01", 30.0),
        )
        await db.execute(
            "INSERT INTO transcription_budget (month_key, used_minutes) VALUES (?, ?)",
            (TranscriptionBudgetTracker._month_key(), 3.0),
        )
        await db.commit()

        real_svc = TranscriptionService(settings=settings, budget=budget)
        svc_wrapper = MagicMock(spec=TranscriptionService)
        svc_wrapper._settings = settings
        svc_wrapper.get_used_minutes = real_svc.get_used_minutes

        pipeline, metrics = _build_pipeline(transcription_service=svc_wrapper)
        await pipeline.seed_transcription_metrics()

        snap = metrics.snapshot()
        val = snap["gauges"]["bot_transcription_minutes_consumed_month"][0]["value"]
        assert abs(val - 3.0) < 0.01, f"gauge deve refletir só mês corrente=3.0, got {val}"
        await budget.close()


# ── AC-5: buckets do histogram definidos p/ transcrição ──────────────────────

async def test_ac5_histogram_has_transcription_buckets():
    """AC-5: bot_transcription_seconds usa boundaries cobrindo até ≥ 300s."""
    _, metrics = _build_pipeline()
    metrics.observe("bot_transcription_seconds", {}, 60.0)

    snap = metrics.snapshot()
    hist = snap["histograms"]["bot_transcription_seconds"][0]
    buckets = hist["buckets"]

    bucket_boundaries = [float(k) for k in buckets.keys()]
    assert max(bucket_boundaries) >= 300.0, f"topo deve ser ≥ 300s, boundaries={bucket_boundaries}"
    assert any(b >= 60.0 for b in bucket_boundaries), "60s deve ter ao menos um bucket cobrindo"
    assert max(bucket_boundaries) > 30.0, "buckets default de 30s não devem ser usados para transcrição"


async def test_ac5_buckets_configured_on_pipeline_init():
    """AC-5: histogram é configurado no __init__ do pipeline, independente de handle()."""
    svc = _mock_svc()
    pipeline, metrics = _build_pipeline(transcription_service=svc)

    assert "bot_transcription_seconds" in metrics._histogram_buckets
    buckets = metrics._histogram_buckets["bot_transcription_seconds"]
    assert max(buckets) >= 300.0
    assert 1.0 in buckets, "bucket mínimo de 1s esperado"
