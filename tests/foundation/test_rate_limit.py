"""Tests for RateLimiter."""

from __future__ import annotations

import asyncio

import pytest

from deilebot._testing import make_user
from deilebot.foundation.exceptions import RateLimited
from deilebot.foundation.rate_limit import RateLimiter, TokenBucket
from deilebot.foundation.settings import BotSettings, FoundationSettings


def _settings(burst=3, refill=60, concurrent=8) -> BotSettings:
    return BotSettings(
        foundation=FoundationSettings(
            rate_limit_user_burst=burst,
            rate_limit_user_refill_per_minute=refill,
            rate_limit_global_concurrent=concurrent,
        )
    )


class TestTokenBucket:
    async def test_acquire_until_empty(self):
        b = TokenBucket(capacity=3, refill_per_minute=0)
        for _ in range(3):
            assert await b.try_acquire()
        assert not await b.try_acquire()

    async def test_refill(self):
        b = TokenBucket(capacity=2, refill_per_minute=6000)  # 100/sec
        await b.try_acquire()
        await b.try_acquire()
        await asyncio.sleep(0.05)  # > 5 tokens refilled
        assert await b.try_acquire()


class TestRateLimiter:
    async def test_burst_then_block(self):
        rl = RateLimiter(_settings(burst=3))
        u = make_user()
        for _ in range(3):
            await rl.acquire_inbound(u)
        with pytest.raises(RateLimited) as excinfo:
            await rl.acquire_inbound(u)
        assert excinfo.value.context["reason"] == "user_burst"

    async def test_distinct_users_independent(self):
        rl = RateLimiter(_settings(burst=2))
        a = make_user(bot_user_id="A", provider_user_id="a")
        b = make_user(bot_user_id="B", provider_user_id="b")
        await rl.acquire_inbound(a)
        await rl.acquire_inbound(a)
        await rl.acquire_inbound(b)
        with pytest.raises(RateLimited):
            await rl.acquire_inbound(a)
        await rl.acquire_inbound(b)

    async def test_outbound_separate_bucket(self):
        rl = RateLimiter(_settings(burst=2))
        u = make_user()
        # Inbound bucket separate from outbound
        await rl.acquire_inbound(u)
        await rl.acquire_inbound(u)
        await rl.acquire_outbound(u)
        await rl.acquire_outbound(u)
        with pytest.raises(RateLimited):
            await rl.acquire_outbound(u)

    async def test_stats_increments(self):
        rl = RateLimiter(_settings(burst=1))
        u = make_user()
        await rl.acquire_inbound(u)
        try:
            await rl.acquire_inbound(u)
        except RateLimited:
            pass
        assert rl.stats()["user_burst"] >= 1


class TestOutboundWeight:
    """RateLimiter.acquire_outbound(weight=N) — heavier ops cost more tokens.

    Used to model the WhatsApp pricing tiers: a marketing template costs
    more than a simple text reply, so it should consume a bigger slice of
    the operator's outbound budget per send.
    """

    async def test_default_weight_one(self):
        rl = RateLimiter(_settings(burst=2))
        u = make_user()
        # weight=1 default — two sends fit, third exceeds
        await rl.acquire_outbound(u)
        await rl.acquire_outbound(u)
        with pytest.raises(RateLimited):
            await rl.acquire_outbound(u)

    async def test_explicit_weight_consumes_multiple_tokens(self):
        rl = RateLimiter(_settings(burst=4))
        u = make_user()
        # weight=3 takes 3 of 4 tokens
        await rl.acquire_outbound(u, weight=3)
        # 1 token left — weight=2 fails
        with pytest.raises(RateLimited):
            await rl.acquire_outbound(u, weight=2)
        # weight=1 still fits
        await rl.acquire_outbound(u, weight=1)

    async def test_weight_zero_or_negative_rejected(self):
        rl = RateLimiter(_settings(burst=2))
        u = make_user()
        with pytest.raises(ValueError):
            await rl.acquire_outbound(u, weight=0)
        with pytest.raises(ValueError):
            await rl.acquire_outbound(u, weight=-1)

    async def test_weight_exceeds_capacity(self):
        rl = RateLimiter(_settings(burst=2))
        u = make_user()
        # weight bigger than capacity always fails — no infinite consumption
        with pytest.raises(RateLimited) as excinfo:
            await rl.acquire_outbound(u, weight=5)
        assert excinfo.value.context["weight"] == 5
