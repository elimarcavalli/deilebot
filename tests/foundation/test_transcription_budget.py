"""AC-5 — monthly budget: teto, incremento atômico, recusa antes da API."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from deilebot.foundation.transcription_budget import TranscriptionBudgetTracker


@pytest.fixture
async def budget(tmp_path: Path) -> TranscriptionBudgetTracker:
    tracker = TranscriptionBudgetTracker(tmp_path / "budget.sqlite")
    yield tracker
    await tracker.close()


async def test_first_reservation_succeeds(budget: TranscriptionBudgetTracker):
    ok = await budget.try_reserve(60.0, max_minutes=60.0, month_key="2099-01")
    assert ok is True


async def test_exact_limit_allowed(budget: TranscriptionBudgetTracker):
    # Use exactly max_minutes — should succeed
    ok = await budget.try_reserve(3600.0, max_minutes=60.0, month_key="2099-02")
    assert ok is True


async def test_exceeding_limit_rejected(budget: TranscriptionBudgetTracker):
    month = "2099-03"
    # Fill up to the limit
    ok1 = await budget.try_reserve(3600.0, max_minutes=60.0, month_key=month)
    assert ok1 is True
    # (N+1)th minute must be rejected before any API call
    ok2 = await budget.try_reserve(60.0, max_minutes=60.0, month_key=month)
    assert ok2 is False


async def test_partial_fill_then_reject(budget: TranscriptionBudgetTracker):
    month = "2099-04"
    # 59 minutes reserved
    ok = await budget.try_reserve(59 * 60.0, max_minutes=60.0, month_key=month)
    assert ok is True
    # 1 more minute (brings total to 60) — should still be allowed
    ok2 = await budget.try_reserve(60.0, max_minutes=60.0, month_key=month)
    assert ok2 is True
    # now over budget
    ok3 = await budget.try_reserve(1.0, max_minutes=60.0, month_key=month)
    assert ok3 is False


async def test_get_used_minutes(budget: TranscriptionBudgetTracker):
    month = "2099-05"
    assert await budget.get_used_minutes(month) == 0.0
    await budget.try_reserve(120.0, max_minutes=60.0, month_key=month)
    used = await budget.get_used_minutes(month)
    assert abs(used - 2.0) < 0.001  # 120s = 2 minutes


async def test_atomic_concurrent_reservations(budget: TranscriptionBudgetTracker):
    """Concurrent try_reserve calls must not both succeed past the limit."""
    month = "2099-06"
    max_minutes = 1.0  # 60 seconds total

    results = await asyncio.gather(
        budget.try_reserve(55.0, max_minutes=max_minutes, month_key=month),
        budget.try_reserve(55.0, max_minutes=max_minutes, month_key=month),
    )
    # Only one of the two should succeed (55s + 55s = 110s > 60s)
    assert sum(results) == 1


async def test_different_months_independent(budget: TranscriptionBudgetTracker):
    ok1 = await budget.try_reserve(3600.0, max_minutes=60.0, month_key="2099-07")
    ok2 = await budget.try_reserve(3600.0, max_minutes=60.0, month_key="2099-08")
    assert ok1 is True
    assert ok2 is True
