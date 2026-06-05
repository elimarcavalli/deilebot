"""Tests for the auto-route of cluster/pipeline questions to deile-monitor.

Two layers:
  * `_is_cluster_question` — the conservative gate (pure function).
  * IngressPipeline.handle — owner cluster questions go to the monitor (the
    embedded agent is NOT invoked); everything else flows normally; an unwired
    monitor falls through (messages are never dropped).
"""
from __future__ import annotations

import pytest

from deilebot._testing import (FakeAgentMetaProvider, FakeProviderAdapter,
                               make_channel, make_envelope)
from deilebot.foundation.audit import BotAuditLogger
from deilebot.foundation.capabilities import CapabilityCatalog
from deilebot.foundation.conversation_store import ConversationStore
from deilebot.foundation.dlq import DeadLetterQueue
from deilebot.foundation.envelope import ChannelScope
from deilebot.foundation.event_bus import BotEventBus
from deilebot.foundation.identity import IdentityResolver
from deilebot.foundation.intent import HeuristicIntentClassifier
from deilebot.foundation.metrics import MetricsCollector
from deilebot.foundation.output_formatter import PlainTextFormatter
from deilebot.foundation.permissions import PermissionGate
from deilebot.foundation.persona_selector import PersonaSelector
from deilebot.foundation.pipeline import (EgressPipeline, IngressPipeline,
                                          _is_cluster_question)
from deilebot.foundation.rate_limit import RateLimiter
from deilebot.foundation.settings import BotSettings, PermissionsSettings
# Reuse the canned bridge from the sibling pipeline test.
from tests.foundation.test_pipeline import FakeBridge

# --------------------------------------------------------------------------- #
# Gate (pure)
# --------------------------------------------------------------------------- #

CLUSTER_QS = [
    "como tá o cluster?",
    "qual o status do pipeline?",
    "os pods estão de pé?",
    "tem alguma anomalia no monitor?",
    "checa o oauth do claude-worker",
    "mostra o backlog",
    "how is the pipeline doing?",
    "what pods are running?",
]
NOT_CLUSTER_QS = [
    "oi DEILE, tudo bem?",
    "me ajuda a refatorar auth.py",
    "obrigado!",
    "escreve um poema sobre pods de ervilha",  # 'pod' sem hint de cluster real… mas tem 'pods'?
    "qual a capital da França?",
]


@pytest.mark.parametrize("q", CLUSTER_QS)
def test_is_cluster_question_true(q):
    assert _is_cluster_question(q) is True, q


@pytest.mark.parametrize("q", ["oi DEILE, tudo bem?", "me ajuda a refatorar auth.py",
                               "obrigado!", "qual a capital da França?", ""])
def test_is_cluster_question_false(q):
    assert _is_cluster_question(q) is False, q


def test_is_cluster_question_needs_both_term_and_hint():
    # term but no question/imperative hint → not routed
    assert _is_cluster_question("o pipeline roda no cluster k8s.") is False
    # hint but no cluster term → not routed
    assert _is_cluster_question("como você está?") is False


def test_is_cluster_question_bounded_length():
    assert _is_cluster_question("como tá o cluster? " + "x" * 1000) is False


# --------------------------------------------------------------------------- #
# Fake MonitorClient (no network)
# --------------------------------------------------------------------------- #


class _FakeError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        self.message = message
        super().__init__(message)


class _FakeClient:
    last = None

    def __init__(self, *, answer="tudo verde ✅", ask_error=None,
                 construct_error=None):
        if construct_error:
            raise _FakeError(construct_error, "boom")
        self.answer = answer
        self.ask_error = ask_error
        self.asked = None
        _FakeClient.last = self

    async def ask(self, question):
        self.asked = question
        if self.ask_error:
            raise _FakeError(self.ask_error, "ask failed")
        return "req-1"

    async def get_ask_result(self, request_id):
        return {"status": "done", "answer": self.answer}


def _loader(**kwargs):
    def _build():
        return _FakeClient(**kwargs)
    return lambda: (_build, _FakeError)


# --------------------------------------------------------------------------- #
# Pipeline harness (mirrors tests/foundation/test_pipeline._wire)
# --------------------------------------------------------------------------- #


@pytest.fixture
async def store(tmp_path):
    s = ConversationStore(tmp_path / "p.sqlite")
    await s.init()
    yield s
    await s.close()


def _wire(store, *, owners=()):
    settings = BotSettings(permissions=PermissionsSettings(owners=list(owners)))
    bridge = FakeBridge()
    adapter = FakeProviderAdapter()
    identity = IdentityResolver(store)
    perms = PermissionGate(settings, identity)
    rl = RateLimiter(settings)
    audit = BotAuditLogger(store)
    bus = BotEventBus()
    metrics = MetricsCollector()
    dlq = DeadLetterQueue(store, settings)
    egress = EgressPipeline(
        formatters={adapter.name: PlainTextFormatter()},
        rate_limit=rl, store=store, audit=audit, event_bus=bus,
        metrics=metrics, dlq=dlq,
    )
    pipeline = IngressPipeline(
        identity=identity, permissions=perms, rate_limit=rl, store=store,
        intent=HeuristicIntentClassifier(), bridge=bridge,
        capability_catalog=CapabilityCatalog(),
        persona_selector=PersonaSelector(settings, identity),
        audit=audit, event_bus=bus, metrics=metrics, egress=egress,
        agent_meta=FakeAgentMetaProvider(),
    )
    return pipeline, adapter, bridge


def _dm(text):
    return make_envelope(channel=make_channel(scope=ChannelScope.DM), text=text)


# --------------------------------------------------------------------------- #
# End-to-end routing
# --------------------------------------------------------------------------- #


async def test_owner_cluster_question_routes_to_monitor(store, monkeypatch):
    pipeline, adapter, bridge = _wire(store, owners=["fake:u-1"])
    monkeypatch.setattr(pipeline, "_load_monitor_client", _loader(answer="tudo verde ✅"))
    await pipeline.handle(_dm("como tá o cluster?"), adapter)
    assert _FakeClient.last.asked == "como tá o cluster?"
    assert bridge.invocations == [], "embedded agent must NOT be invoked when routed"
    assert adapter.inbox[-1]["text"] == "tudo verde ✅"


async def test_non_owner_cluster_question_uses_normal_flow(store, monkeypatch):
    pipeline, adapter, bridge = _wire(store, owners=[])  # u-1 is NOT an owner
    called = {"v": False}
    monkeypatch.setattr(pipeline, "_load_monitor_client",
                        lambda: (_ for _ in ()).throw(AssertionError("must not load")))
    # Make _is_cluster_question irrelevant: non-owner never reaches the gate's
    # monitor branch (is_owner is False), so the embedded agent answers.
    await pipeline.handle(_dm("como tá o cluster?"), adapter)
    assert len(bridge.invocations) == 1, "non-owner cluster q → embedded agent"
    assert called["v"] is False


async def test_owner_unrelated_message_uses_normal_flow(store, monkeypatch):
    pipeline, adapter, bridge = _wire(store, owners=["fake:u-1"])
    monkeypatch.setattr(pipeline, "_load_monitor_client",
                        lambda: (_ for _ in ()).throw(AssertionError("must not load")))
    await pipeline.handle(_dm("me ajuda a refatorar auth.py"), adapter)
    assert len(bridge.invocations) == 1, "non-cluster message → embedded agent"


async def test_unwired_monitor_falls_through_to_agent(store, monkeypatch):
    pipeline, adapter, bridge = _wire(store, owners=["fake:u-1"])
    # Monitor package not available → _load_monitor_client returns None.
    monkeypatch.setattr(pipeline, "_load_monitor_client", lambda: None)
    await pipeline.handle(_dm("como tá o cluster?"), adapter)
    assert len(bridge.invocations) == 1, "unwired monitor must NOT drop the message"


async def test_monitor_auth_missing_falls_through(store, monkeypatch):
    pipeline, adapter, bridge = _wire(store, owners=["fake:u-1"])
    monkeypatch.setattr(pipeline, "_load_monitor_client",
                        _loader(construct_error="MONITOR_AUTH_MISSING"))
    await pipeline.handle(_dm("como tá o cluster?"), adapter)
    assert len(bridge.invocations) == 1, "auth-missing monitor → normal flow"


async def test_monitor_ask_error_replies_and_skips_agent(store, monkeypatch):
    pipeline, adapter, bridge = _wire(store, owners=["fake:u-1"])
    monkeypatch.setattr(pipeline, "_load_monitor_client",
                        _loader(ask_error="MONITOR_UNREACHABLE"))
    await pipeline.handle(_dm("como tá o cluster?"), adapter)
    assert bridge.invocations == [], "a wired-but-failing monitor must not double-process"
    assert "MONITOR_UNREACHABLE" in adapter.inbox[-1]["text"]


class _RunningForeverClient:
    """ask() ok, but the result stays 'running' forever → exercises the timeout."""

    async def ask(self, question):
        return "req-1"

    async def get_ask_result(self, request_id):
        return {"status": "running"}


async def test_monitor_poll_timeout_replies_and_skips_agent(store, monkeypatch):
    import deilebot.foundation.pipeline as plmod
    # Trip the deadline almost immediately so the test is fast.
    monkeypatch.setattr(plmod, "_AUTOROUTE_POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(plmod, "_AUTOROUTE_POLL_INTERVAL_S", 0.01)
    pipeline, adapter, bridge = _wire(store, owners=["fake:u-1"])
    monkeypatch.setattr(pipeline, "_load_monitor_client",
                        lambda: (lambda: _RunningForeverClient(), _FakeError))
    await pipeline.handle(_dm("como tá o cluster?"), adapter)
    assert bridge.invocations == [], "timeout path must not fall through to the agent"
    assert "demorou" in adapter.inbox[-1]["text"].lower()
