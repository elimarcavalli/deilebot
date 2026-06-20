"""IngressPipeline + EgressPipeline.

Ingress: adapter -> identity -> permissions -> rate_limit -> store -> intent ->
         persona -> capability_catalog -> bridge.invoke -> egress.

Egress: render markup -> split -> send -> persist -> audit; on failure -> DLQ.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from tenacity import (AsyncRetrying, retry_if_exception_type,
                      stop_after_attempt, wait_exponential)

from deilebot.foundation.agent_bridge import (AgentBridge, AgentInvocation,
                                              AgentResponse)
from deilebot.foundation.agent_meta import AgentMetaProvider
from deilebot.foundation.audit import AuditEventType, BotAuditLogger
from deilebot.foundation.capabilities import CapabilityCatalog
from deilebot.foundation.conversation_store import (ConversationStore,
                                                    StoredMessage)
from deilebot.foundation.dlq import DeadLetterQueue
from deilebot.foundation.envelope import (BotUser, Channel, MessageEnvelope,
                                          TemplateMessage)
from deilebot.foundation.event_bus import BotEventBus, BotEventType
from deilebot.foundation.exceptions import (AgentInvocationError,
                                            ProviderError, RateLimited)
from deilebot.foundation.identity import IdentityResolver
from deilebot.foundation.intent import IntentClassifier
from deilebot.foundation.logging import get_logger
from deilebot.foundation.metrics import MetricsCollector, timer
from deilebot.foundation.output_formatter import OutputFormatter
from deilebot.foundation.permissions import Action, PermissionGate
from deilebot.foundation.persona_selector import PersonaSelector
from deilebot.foundation.rate_limit import RateLimiter
from deilebot.foundation.settings import get_bot_settings, resolve_language
from deilebot.foundation.transcription import TranscriptionRejectedError


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    base_seconds: float = 0.2
    max_seconds: float = 5.0


# Auto-route: owner messages that are cluster/pipeline questions are answered by
# the deile-monitor (the only pod that can see the cluster), not the embedded
# agent. Conservative gate — requires BOTH a cluster term AND a question/imperative
# hint — so it never hijacks ordinary chat that merely mentions "pod" in passing.
_CLUSTER_TERM_RE = re.compile(
    r"\b(cluster|k8s|kubernetes|kubectl|namespace|pipeline|pods?|"
    r"monitor(?:amento)?|deploy(?:ment)?|oauth|claude-worker|deile-worker|"
    r"deilebot|anomalias?|vigia|tick|reaper|backlog|worktree)\b",
    re.IGNORECASE,
)
_CLUSTER_QUESTION_HINT_RE = re.compile(
    r"\?|^\s*(como|qual|quais|quanto|quantos|quantas|est[áa]|t[áa]|tem|h[áa]|"
    r"cad[êe]|o que|por ?que|status|checa|verifica|olha|mostra|lista|me diz|"
    r"diz|ver|how|what|why|is\b|are\b|show|list|check)\b",
    re.IGNORECASE,
)
_AUTOROUTE_POLL_INTERVAL_S = 2.0
_AUTOROUTE_POLL_TIMEOUT_S = 170.0


def _is_cluster_question(text: str) -> bool:
    """True iff *text* looks like a cluster/pipeline question (conservative).

    Requires a cluster term AND a question/imperative hint; bounded length so a
    long paste that merely contains "pod" never triggers."""
    t = (text or "").strip()
    if not t or len(t) > 600:
        return False
    return bool(_CLUSTER_TERM_RE.search(t) and _CLUSTER_QUESTION_HINT_RE.search(t))


def _build_transcript_echo(transcript: str, max_chars: int) -> str:
    """Return the echo prefix for the reply when echo_transcript is enabled."""
    if len(transcript) > max_chars:
        text = transcript[:max_chars] + "…"
    else:
        text = transcript
    return f'🎙️ Transcrevi seu áudio: "{text}"\n'


def render_history_for_worker(
    history: List[StoredMessage],
    *,
    exclude_message_id: Optional[str] = None,
    limit: int = 20,
    max_chars: int = 8000,
) -> str:
    """Render recent channel messages as a compact text block for the worker.

    The deile-worker runs one-shot (a fresh session per dispatch), so when
    the bot delegates a follow-up ("agora adiciona um teste pra aquele
    arquivo") the worker needs the prior turns as context. ``history``
    arrives newest-first (as ``ConversationStore.get_recent_messages``
    returns it); this emits the most recent ``limit`` messages
    oldest-first, labelled by direction, with per-message and total
    length caps. The message identified by ``exclude_message_id`` (the
    current inbound, already captured verbatim as the brief) is dropped.
    """
    msgs = [
        m
        for m in history
        if not exclude_message_id or m.provider_message_id != exclude_message_id
    ]
    msgs = list(reversed(msgs[:limit]))  # newest-first slice → oldest-first
    lines: List[str] = []
    for m in msgs:
        who = "user" if m.direction == "inbound" else "deile"
        text = " ".join((m.text or "").split())
        if len(text) > 400:
            text = text[:399] + "…"
        lines.append(f"[{who}] {text}")
    block = "\n".join(lines)
    return block[-max_chars:] if len(block) > max_chars else block


class EgressPipeline:
    """Render → split → send via adapter → persist → audit; DLQ on fatal fail."""

    def __init__(
        self,
        formatters: Dict[str, OutputFormatter],
        rate_limit: RateLimiter,
        store: ConversationStore,
        audit: BotAuditLogger,
        event_bus: BotEventBus,
        metrics: MetricsCollector,
        dlq: DeadLetterQueue,
        retry_policy: Optional[RetryPolicy] = None,
        default_formatter: Optional[OutputFormatter] = None,
    ):
        self._formatters = formatters
        self._rate_limit = rate_limit
        self._store = store
        self._audit = audit
        self._event_bus = event_bus
        self._metrics = metrics
        self._dlq = dlq
        self._policy = retry_policy or RetryPolicy()
        self._logger = get_logger("egress")
        self._default_formatter = default_formatter

    def _formatter_for(self, provider: str) -> OutputFormatter:
        f = self._formatters.get(provider)
        if f is not None:
            return f
        if self._default_formatter is not None:
            return self._default_formatter
        from deilebot.foundation.output_formatter import PlainTextFormatter

        return PlainTextFormatter()

    async def send_response(
        self,
        adapter,
        env: MessageEnvelope,
        response: AgentResponse,
        persona: str,
        *,
        target_bot_user_id: Optional[str] = None,
        transcript_echo: Optional[str] = None,
    ) -> None:
        formatter = self._formatter_for(adapter.name)
        rendered = formatter.render(response.markup) if response.markup else response.text
        chunks = formatter.split(rendered) or [rendered]
        last_msg_id: Optional[str] = env.message_id
        target_id = target_bot_user_id or env.author.bot_user_id
        for i, chunk in enumerate(chunks):
            if i == 0 and transcript_echo:
                chunk = transcript_echo + chunk
            try:
                msg_id = await self._send_with_retry(
                    adapter, env.channel, chunk, reply_to=last_msg_id if i == 0 else None
                )
                await self._store.record_outbound(
                    provider=adapter.name,
                    channel=env.channel,
                    provider_message_id=msg_id,
                    bot_user_id=target_id,
                    text=chunk,
                    reply_to=last_msg_id,
                    sent_at=datetime.now(timezone.utc),
                )
                self._metrics.inc(
                    "bot_outbound_total", {"provider": adapter.name, "status": "ok"}
                )
                self._metrics.observe(
                    "bot_outbound_chars",
                    {"provider": adapter.name},
                    float(len(chunk)),
                )
                await self._audit.log(
                    AuditEventType.OUTBOUND_SENT,
                    user=env.author,
                    channel=env.channel,
                    message_id=msg_id,
                    payload={"persona": persona, "chars": len(chunk), "chunk_index": i},
                )
                await self._event_bus.publish(
                    BotEventType.OUTBOUND_SENT,
                    {"provider": adapter.name, "channel": env.channel.provider_channel_id},
                )
                last_msg_id = msg_id
            except Exception as e:  # noqa: BLE001
                self._metrics.inc(
                    "bot_outbound_total", {"provider": adapter.name, "status": "fail"}
                )
                await self._audit.log(
                    AuditEventType.OUTBOUND_FAILED,
                    user=env.author,
                    channel=env.channel,
                    payload={"error": str(e)[:200]},
                )
                await self._dlq.enqueue(
                    adapter.name,
                    {
                        "channel_id": env.channel.provider_channel_id,
                        "channel_provider": env.channel.provider,
                        "channel_scope": env.channel.scope.value,
                        "channel_name": env.channel.name,
                        "channel_parent_id": env.channel.parent_channel_id,
                        "text": chunk,
                        "reply_to": last_msg_id,
                        "bot_user_id": target_id,
                    },
                    error=f"{type(e).__name__}: {e}",
                    attempts=self._policy.max_attempts,
                )
                self._metrics.inc(
                    "bot_dlq_enqueued_total", {"provider": adapter.name}
                )
                await self._event_bus.publish(
                    BotEventType.DLQ_ENQUEUED, {"provider": adapter.name}
                )
                return

    async def _send_with_retry(
        self,
        adapter,
        channel: Channel,
        text: str,
        reply_to: Optional[str],
    ) -> str:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._policy.max_attempts),
            wait=wait_exponential(
                multiplier=self._policy.base_seconds, max=self._policy.max_seconds
            ),
            retry=retry_if_exception_type(ProviderError),
            reraise=True,
        ):
            with attempt:
                return await adapter.send_message(channel, text, reply_to=reply_to)
        raise ProviderError("retry exhausted")  # pragma: no cover

    async def send_template(
        self,
        adapter,
        user: BotUser,
        channel: Channel,
        template: TemplateMessage,
        *,
        category: str = "utility",
    ) -> str:
        """Send a template message via the adapter.

        Templates are the only outbound shape allowed outside the 24h
        WhatsApp conversation window. The caller is responsible for
        deciding *when* to use a template (typically by checking
        ``ConversationStore.get_window``); this method just executes
        the send, persists, and increments the per-category metric so
        the operator can see how cost-bearing categories drift.
        """
        if not hasattr(adapter, "send_template"):
            raise ProviderError(
                f"adapter {adapter.name!r} does not implement send_template",
                context={"provider": adapter.name},
            )
        try:
            msg_id = await adapter.send_template(user, template)
        except Exception as e:  # noqa: BLE001
            self._metrics.inc(
                "bot_outbound_total",
                {"provider": adapter.name, "status": "fail"},
            )
            self._metrics.inc(
                "bot_whatsapp_conversations_total",
                {"category": category, "status": "fail"},
            )
            await self._audit.log(
                AuditEventType.OUTBOUND_FAILED,
                user=user,
                channel=channel,
                payload={
                    "error": str(e)[:200],
                    "kind": "template",
                    "template": template.name,
                    "language": template.language,
                    "category": category,
                },
            )
            raise
        await self._store.record_outbound(
            provider=adapter.name,
            channel=channel,
            provider_message_id=msg_id,
            bot_user_id=user.bot_user_id,
            text=f"[template:{template.name}]",
            reply_to=None,
            sent_at=datetime.now(timezone.utc),
        )
        self._metrics.inc(
            "bot_outbound_total", {"provider": adapter.name, "status": "ok"}
        )
        self._metrics.inc(
            "bot_whatsapp_conversations_total",
            {"category": category, "status": "ok"},
        )
        await self._audit.log(
            AuditEventType.OUTBOUND_SENT,
            user=user,
            channel=channel,
            message_id=msg_id,
            payload={
                "kind": "template",
                "template": template.name,
                "language": template.language,
                "category": category,
            },
        )
        await self._event_bus.publish(
            BotEventType.OUTBOUND_SENT,
            {
                "provider": adapter.name,
                "channel": channel.provider_channel_id,
                "kind": "template",
            },
        )
        return msg_id

    async def send_fallback(
        self,
        adapter,
        channel: Channel,
        reply_to: Optional[str],
        reason: str,
    ) -> None:
        msg = f"⚠️ não foi possível processar agora ({reason})."
        try:
            await adapter.send_message(channel, msg, reply_to=reply_to)
        except Exception:
            self._logger.warning("fallback send also failed", exc_info=True)


class IngressPipeline:
    """Single entry point: adapter delivers an envelope; pipeline routes it."""

    def __init__(
        self,
        identity: IdentityResolver,
        permissions: PermissionGate,
        rate_limit: RateLimiter,
        store: ConversationStore,
        intent: IntentClassifier,
        bridge: AgentBridge,
        capability_catalog: CapabilityCatalog,
        persona_selector: PersonaSelector,
        audit: BotAuditLogger,
        event_bus: BotEventBus,
        metrics: MetricsCollector,
        egress: EgressPipeline,
        agent_meta: AgentMetaProvider,
        transcription_service=None,
    ):
        self.identity = identity
        self.permissions = permissions
        self.rate_limit = rate_limit
        self.store = store
        self.intent = intent
        self.bridge = bridge
        self.capability_catalog = capability_catalog
        self.persona_selector = persona_selector
        self.audit = audit
        self.event_bus = event_bus
        self.metrics = metrics
        self.egress = egress
        self.agent_meta = agent_meta
        self._transcription_service = transcription_service
        self._logger = get_logger("ingress")
        self._transcription_metrics_seeded = False
        # Configure transcription histogram with boundaries suited to STT latency.
        # topo=300s covers max_duration_seconds (default 120s) with headroom.
        self.metrics.configure_histogram(
            "bot_transcription_seconds",
            (1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
        )

    async def seed_transcription_metrics(self) -> None:
        """Re-seed bot_transcription_minutes_consumed_month from SQLite.

        Gauges are in-memory and reset on pod restart; the SQLite counter
        persists. Call this once at startup (or before first handle()) so the
        gauge reflects the real month total immediately after restart.
        """
        if self._transcription_service is None:
            return
        used = await self._transcription_service.get_used_minutes()
        self.metrics.gauge("bot_transcription_minutes_consumed_month", {}, used)
        self._transcription_metrics_seeded = True

    def _load_monitor_client(self):
        """Lazy import of ``(MonitorClient, MonitorClientError)`` from the deile
        package. Lazy because ``deile`` is heavy and the client ships from the
        deile repo (merges first, see spec §8). Returns ``None`` when the package
        is unavailable so the caller falls through to the embedded agent."""
        try:
            from deile.infrastructure.deile_monitor_client import (
                MonitorClient, MonitorClientError)
        except Exception:  # noqa: BLE001 — not installed → normal flow
            return None
        return MonitorClient, MonitorClientError

    async def _send_monitor_text(self, adapter, env: MessageEnvelope,
                                 persona: str, text: str) -> None:
        """Deliver *text* via the egress (chunking + persistence + audit)."""
        response = AgentResponse(text=text, markup=None)
        await self.egress.send_response(adapter, env, response, persona)

    async def _try_route_to_monitor(self, env: MessageEnvelope, adapter,
                                    persona: str) -> bool:
        """Ask the deile-monitor and reply. Returns ``True`` when handled
        (answered or surfaced an error), ``False`` to fall through to the
        embedded agent (monitor not wired in this deployment)."""
        loaded = self._load_monitor_client()
        if loaded is None:
            return False
        MonitorClient, MonitorClientError = loaded
        try:
            client = MonitorClient()
        except MonitorClientError as exc:
            if exc.code in ("MONITOR_AUTH_MISSING", "MONITOR_TRANSPORT_MISSING"):
                return False  # not wired → normal flow, never drop the message
            await self._send_monitor_text(
                adapter, env, persona, f"⚠️ monitor indisponível (`{exc.code}`).")
            return True
        self.metrics.inc("bot_monitor_autoroute_total", {"result": "started"})
        # Acknowledge (best-effort; skip synthetic slash ids).
        if not str(env.message_id or "").startswith("slash-"):
            try:
                await adapter.react(env.channel, env.message_id, "🛰️")
            except Exception:  # noqa: BLE001 — reaction is cosmetic
                pass
        try:
            request_id = await client.ask(env.text)
        except MonitorClientError as exc:
            await self._send_monitor_text(
                adapter, env, persona,
                f"⚠️ não consegui perguntar ao monitor (`{exc.code}`).")
            self.metrics.inc("bot_monitor_autoroute_total", {"result": "error"})
            return True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _AUTOROUTE_POLL_TIMEOUT_S
        result: Dict = {}
        while loop.time() < deadline:
            try:
                result = await client.get_ask_result(request_id)
            except MonitorClientError as exc:
                await self._send_monitor_text(
                    adapter, env, persona,
                    f"⚠️ erro ao buscar a resposta (`{exc.code}`).")
                self.metrics.inc("bot_monitor_autoroute_total", {"result": "error"})
                return True
            if result.get("status") != "running":
                break
            await asyncio.sleep(_AUTOROUTE_POLL_INTERVAL_S)
        else:
            await self._send_monitor_text(
                adapter, env, persona,
                "⏱️ o monitor demorou demais. Tente `/monitor ask` de novo.")
            self.metrics.inc("bot_monitor_autoroute_total", {"result": "timeout"})
            return True
        if result.get("status") == "done":
            await self._send_monitor_text(
                adapter, env, persona,
                str(result.get("answer") or "(resposta vazia)"))
            self.metrics.inc("bot_monitor_autoroute_total", {"result": "done"})
        else:
            await self._send_monitor_text(
                adapter, env, persona,
                f"⚠️ o monitor não respondeu: {str(result.get('error') or 'erro')[:1500]}")
            self.metrics.inc("bot_monitor_autoroute_total", {"result": "error"})
        return True

    async def handle(self, env: MessageEnvelope, adapter) -> None:
        if not self._transcription_metrics_seeded:
            await self.seed_transcription_metrics()
        provider = env.channel.provider
        scope = env.channel.scope.value
        # 1. inbound audit + metric
        self.metrics.inc("bot_inbound_total", {"provider": provider, "scope": scope})
        await self.audit.log(
            AuditEventType.INBOUND_RECEIVED,
            user=env.author,
            channel=env.channel,
            message_id=env.message_id,
            payload={"text_chars": len(env.text)},
        )
        # 2. resolve identity (refresh display_name)
        user = await self.identity.resolve(
            env.author.provider,
            env.author.provider_user_id,
            env.author.display_name,
            is_bot=env.author.is_bot,
        )
        # 3. permission gate
        decision = await self.permissions.check(
            user, Action.INVOKE_AGENT, scope=env.channel.scope
        )
        if not decision.allowed:
            self.metrics.inc(
                "bot_permission_denied_total",
                {"provider": provider, "action": "INVOKE_AGENT"},
            )
            await self.audit.log(
                AuditEventType.PERMISSION_DENIED,
                user=user,
                channel=env.channel,
                message_id=env.message_id,
                payload={"reason": decision.reason},
            )
            await self.event_bus.publish(
                BotEventType.PERMISSION_DENIED,
                {"provider": provider, "reason": decision.reason},
            )
            return
        # 4. rate limit
        try:
            await self.rate_limit.acquire_inbound(user)
        except RateLimited as e:
            reason = e.context.get("reason", "unknown")
            self.metrics.inc(
                "bot_rate_limited_total", {"provider": provider, "reason": reason}
            )
            await self.audit.log(
                AuditEventType.RATE_LIMITED,
                user=user,
                channel=env.channel,
                payload={"reason": reason},
            )
            return
        # 5. persist (use resolved user.bot_user_id, not env.author's stale one)
        await self.store.upsert_user(user)
        await self.store.upsert_channel(env.channel)
        await self.store.record_inbound(env, bot_user_id_override=user.bot_user_id)
        # 6. recent history
        history = await self.store.get_recent_messages(provider, env.channel, limit=40)
        # 7. intent classifier
        decision_intent = await self.intent.decide(env, history, adapter.self_user_id)
        self.metrics.inc(
            "bot_should_respond_total",
            {
                "provider": provider,
                "decision": "true" if decision_intent.should_respond else "false",
                "classifier": type(self.intent).__name__,
            },
        )
        await self.audit.log(
            AuditEventType.SHOULD_RESPOND_DECIDED,
            user=user,
            channel=env.channel,
            message_id=env.message_id,
            payload={
                "decision": decision_intent.should_respond,
                "reason": decision_intent.reason,
            },
        )
        if not decision_intent.should_respond:
            return
        # 8. owner / persona
        is_owner = await self.permissions.is_owner(user)
        persona = await self.persona_selector.resolve(env, user, is_owner)
        # 8.5 Auto-route owner cluster/pipeline questions to the deile-monitor —
        # the only pod with cluster visibility. Falls through to the embedded
        # agent when the monitor is not wired (messages are never dropped).
        if is_owner and _is_cluster_question(env.text):
            if await self._try_route_to_monitor(env, adapter, persona):
                return
        # 9. capability snapshot + extra prompt
        snap = await self.capability_catalog.snapshot(adapter, self.agent_meta)
        extra = self.capability_catalog.render_for_system_prompt(snap)
        # Eagerly download image attachments and base64-encode them in the
        # bot process. Discord CDN URLs since 2024 carry expiring signatures
        # (`?ex=…&is=…&hm=…`), so passing the URL to DEILE risks 403 by the
        # time DEILE goes to download. The bot is the connected client and
        # already has access — do the IO once here.
        # Cap: 4 MiB per image (≈5.4 MiB base64) to avoid bloating the LLM
        # context. Larger attachments fall back to URL-only and DEILE gets
        # to decide whether to try the (possibly expired) URL.
        att_summaries = await self._materialize_attachments(
            env.attachments,
            transcription_service=self._transcription_service,
            env=env,
        )
        # Append a <bot_context> block so the LLM (not just the tools) can
        # see channel/owner info and — critically — inbound attachments.
        # Without this the agent sees the attachments only via tool ctx,
        # which it never inspects unless prompted.
        ctx_lines = [
            "<bot_context>",
            f"provider: {provider}",
            f"channel_scope: {scope}",
            f"channel_id: {env.channel.provider_channel_id}",
            f"channel_name: {env.channel.name or '-'}",
            # The user-message-id is the snowflake of the message the user
            # JUST sent (the inbound this turn handles). It's the right
            # value for "react/edit/pin to my last message" or for the
            # worker to react 🔧/✅ on the user's message during dispatch.
            f"user_message_id: {env.message_id}",
            f"user_id: {user.provider_user_id}",
            f"author_display_name: {user.display_name or '-'}",
            f"is_owner: {is_owner}",
            f"persona: {persona}",
        ]
        if att_summaries:
            ctx_lines.append("attachments:")
            for s in att_summaries:
                if s.get("transcript"):
                    marker = "transcribed"
                elif s.get("data_base64"):
                    marker = "base64_inline"
                else:
                    marker = "url_only"
                ctx_lines.append(
                    f"  - kind={s['kind']} mime={s.get('mime') or '?'} "
                    f"filename={s.get('filename') or '?'} "
                    f"size_bytes={s.get('size_bytes') or 0} delivery={marker}"
                )
                if s.get("data_base64"):
                    # Don't dump the full base64 into the system prompt — it
                    # would dominate context. The agent reads it from
                    # bot_context.attachments[*].data_base64 (kwarg).
                    ctx_lines.append(
                        f"    data_base64: <{len(s['data_base64'])} chars in bot_context.attachments[].data_base64>"
                    )
                if s.get("url"):
                    ctx_lines.append(f"    url: {s['url']}")
                if s.get("download_error"):
                    ctx_lines.append(f"    download_error: {s['download_error']}")
                if s.get("transcript"):
                    ctx_lines.append(f"    transcript: {s['transcript'][:300]}")
                if s.get("skip_reason"):
                    ctx_lines.append(f"    skip_reason: {s['skip_reason']}")
        ctx_lines.append("</bot_context>")
        extra = extra + "\n\n" + "\n".join(ctx_lines)
        _full_settings = get_bot_settings()
        bot_settings = _full_settings.foundation
        # 10. agent invoke — prefix any audio transcripts to inbound_text
        transcripts = [s["transcript"] for s in att_summaries if s.get("transcript")]
        transcript_echo: Optional[str] = None
        if transcripts:
            joined = " ".join(transcripts)
            inbound_text = f"[áudio transcrito] {joined}\n{env.text}".strip()
            t_cfg = _full_settings.transcription
            if t_cfg.echo_transcript:
                transcript_echo = _build_transcript_echo(joined, t_cfg.echo_max_chars)
        else:
            inbound_text = env.text
        inv = AgentInvocation(
            bot_user_id=user.bot_user_id,
            persona=persona,
            forced_model=bot_settings.forced_model,
            inbound_text=inbound_text,
            default_model=bot_settings.default_model,
            inbound_attachments=env.attachments,
            history=history,
            capabilities=snap,
            extra_system_prompt=extra,
            channel=env.channel,
            bot_context={
                "provider": provider,
                "channel_scope": scope,
                "channel_id": env.channel.provider_channel_id,
                "channel_name": env.channel.name,
                "user_message_id": env.message_id,
                "user_id": user.provider_user_id,
                "author_display_name": user.display_name,
                "is_owner": is_owner,
                "persona": persona,
                # Surface inbound attachments so the agent can call
                # vision_describe_image (or similar) on URLs without
                # the agent having to ask. Discord CDN URLs are public,
                # no auth required to download.
                "attachments": att_summaries,
                # Recent channel turns, pre-rendered. dispatch_deile_task
                # forwards this to the worker so a bot-mediated follow-up
                # has context (the worker itself is one-shot). The /deile
                # passthrough never goes through here, so it stays
                # historyless by construction.
                "recent_history": render_history_for_worker(
                    history, exclude_message_id=env.message_id
                ),
            },
            timeout_seconds=bot_settings.agent_invocation_timeout_seconds,
        )
        self.metrics.inc(
            "bot_agent_invocations_total",
            {"provider": provider, "persona": persona, "status": "started"},
        )
        await self.audit.log(
            AuditEventType.AGENT_INVOKED,
            user=user,
            channel=env.channel,
            message_id=env.message_id,
            payload={"persona": persona, "extra_chars": len(extra)},
        )
        # UX premium: react 🔧 imediatamente para sinalizar "recebi"
        # (best-effort; cooldown não-fatal). E typing keep-alive em loop
        # enquanto o agent processa, pra não morrer em 10s.
        #
        # Slash-command inbound (e.g. /deile prompt:X) traz um message_id
        # sintético (não existe no Discord) — pular as reactions evita
        # ruído UNKNOWN_MESSAGE no log e tentativas inúteis.
        _is_slash = (
            str(env.message_id or "").startswith("slash-")
            or str((env.raw or {}).get("source", "")).startswith("slash:")
        )
        if not _is_slash:
            try:
                await adapter.react(env.channel, env.message_id, "🔧")
            except Exception:
                self._logger.debug("inbound react ignored", exc_info=True)

        async def _typing_keepalive(stop_evt: "asyncio.Event") -> None:
            while not stop_evt.is_set():
                try:
                    await adapter.send_typing(env.channel)
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(stop_evt.wait(), timeout=7.0)
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    return

        import asyncio
        _stop = asyncio.Event()
        _typing_task = asyncio.create_task(_typing_keepalive(_stop))
        try:
            with timer(self.metrics, "bot_agent_invocation_seconds", {"provider": provider}):
                response = await self.bridge.invoke(inv)
        except AgentInvocationError as e:
            _stop.set()
            try:
                await _typing_task
            except Exception:
                pass
            self.metrics.inc(
                "bot_agent_invocations_total",
                {"provider": provider, "persona": persona, "status": "fail"},
            )
            await self.audit.log(
                AuditEventType.AGENT_FAILED,
                user=user,
                channel=env.channel,
                message_id=env.message_id,
                payload={"error": str(e)[:200], "type": type(e).__name__},
            )
            # UX: react ❌ pra sinalizar erro + fallback expressivo com tipo do erro
            if not _is_slash:
                try:
                    await adapter.react(env.channel, env.message_id, "❌")
                except Exception:
                    pass
            err_type = type(e).__name__
            if "Timeout" in err_type:
                reason = "timeout — pedido demorou demais; tente algo menor"
            else:
                reason = f"{err_type}: {str(e)[:200]}"
            await self.egress.send_fallback(
                adapter, env.channel, env.message_id, reason=reason
            )
            return
        finally:
            _stop.set()
            try:
                if not _typing_task.done():
                    _typing_task.cancel()
            except Exception:
                pass

        # UX: react ✅ no envelope inbound depois do sucesso
        if not _is_slash:
            try:
                await adapter.react(env.channel, env.message_id, "✅")
            except Exception:
                self._logger.debug("success react ignored", exc_info=True)
        await self.audit.log(
            AuditEventType.AGENT_RESPONDED,
            user=user,
            channel=env.channel,
            message_id=env.message_id,
            payload={
                "elapsed_ms": response.elapsed_ms,
                "model": response.model_used,
                "chars": len(str(response.text)),
            },
        )
        self.metrics.inc(
            "bot_agent_invocations_total",
            {"provider": provider, "persona": persona, "status": "ok"},
        )
        # 11. egress
        await self.egress.send_response(
            adapter, env, response, persona, target_bot_user_id=user.bot_user_id,
            transcript_echo=transcript_echo
        )

    # ------------------------------------------------------------------
    # attachment materialization
    # ------------------------------------------------------------------

    # Per-image cap: 4 MiB raw → ~5.4 MiB base64. Anything larger falls
    # back to URL-only (the agent decides whether to call vision_describe_image
    # with the URL — Gemini handles up to 20 MB inline anyway, but we don't
    # want a 20 MB blob in our LLM context).
    _IMAGE_INLINE_BYTES_LIMIT = 4 * 1024 * 1024
    _IMAGE_DOWNLOAD_TIMEOUT_S = 12.0

    async def _materialize_attachments(
        self, attachments, *, transcription_service=None, env=None
    ) -> list:
        """Build the bot_context.attachments list.

        For image kind, eagerly fetch + base64-encode (so DEILE doesn't
        have to re-download from a possibly-expired Discord CDN URL).
        For audio kind with transcription enabled, download + STT via Whisper.
        For non-image/audio kinds (or failed/oversize), keep URL-only.
        Errors are recorded inline as `download_error`/`skip_reason`.
        """
        import asyncio
        import base64 as _b64

        try:
            import httpx  # noqa: F401  (httpx is already in deps)
        except ImportError:
            httpx = None  # type: ignore[assignment]

        # Resolve Whisper language once for all audio attachments in this request.
        _transcription_language: Optional[str] = None
        if transcription_service is not None:
            _lang_setting = getattr(
                getattr(transcription_service, "_settings", None), "language", "auto"
            )
            _guild_locale: Optional[str] = None
            if env is not None:
                _guild_locale = (
                    getattr(env.channel, "locale", None)
                    or (env.raw or {}).get("guild_locale")
                )
            _transcription_language = resolve_language(_lang_setting, _guild_locale)

        results: list = []

        async def _fetch_one(att):
            kind_str = att.kind.value if hasattr(att.kind, "value") else str(att.kind)
            entry: dict = {
                "kind": kind_str,
                "url": att.url,
                "mime": att.mime,
                "filename": att.filename,
                "size_bytes": att.size_bytes,
            }
            # Audio: attempt STT when transcription_service is wired and enabled
            if kind_str.upper() == "AUDIO" and transcription_service is not None:
                if transcription_service._settings.enabled and att.url:
                    import time as _time
                    _metrics = getattr(self, "metrics", None)
                    _t0 = _time.monotonic()
                    try:
                        transcript = await transcription_service.transcribe(
                            att, language=_transcription_language
                        )
                        entry["transcript"] = transcript
                        if _metrics is not None:
                            _elapsed = _time.monotonic() - _t0
                            _metrics.observe("bot_transcription_seconds", {}, _elapsed)
                            _metrics.inc("bot_transcription_total", {"status": "ok"})
                            _used = await transcription_service.get_used_minutes()
                            _metrics.gauge("bot_transcription_minutes_consumed_month", {}, _used)
                    except TranscriptionRejectedError as exc:
                        if _metrics is not None:
                            _metrics.inc("bot_transcription_total", {"status": "rejected"})
                        entry["skip_reason"] = f"STT: {exc}"
                        self._logger.warning(
                            "transcription rejected (budget) for %s: %s", att.url, exc
                        )
                    except Exception as exc:
                        if _metrics is not None:
                            _metrics.inc("bot_transcription_total", {"status": "error"})
                        entry["skip_reason"] = f"STT: {exc}"
                        self._logger.warning(
                            "transcription failed for %s: %s", att.url, exc
                        )
                return entry
            # Only image-kind attachments get inlined; other kinds keep
            # the URL so a future tool can decide.
            if kind_str.upper() != "IMAGE" or not att.url:
                return entry
            if att.size_bytes and att.size_bytes > self._IMAGE_INLINE_BYTES_LIMIT:
                entry["download_error"] = (
                    f"too large to inline ({att.size_bytes} > {self._IMAGE_INLINE_BYTES_LIMIT})"
                )
                return entry
            if httpx is None:
                entry["download_error"] = "httpx not installed"
                return entry
            try:
                import httpx as _httpx
                async with _httpx.AsyncClient(
                    timeout=self._IMAGE_DOWNLOAD_TIMEOUT_S, follow_redirects=True
                ) as cli:
                    async with cli.stream("GET", att.url) as resp:
                        if resp.status_code >= 400:
                            entry["download_error"] = f"upstream {resp.status_code}"
                            return entry
                        buf = bytearray()
                        async for chunk in resp.aiter_bytes():
                            buf.extend(chunk)
                            if len(buf) > self._IMAGE_INLINE_BYTES_LIMIT:
                                entry["download_error"] = "exceeded cap mid-download"
                                return entry
            except Exception as e:
                entry["download_error"] = f"{type(e).__name__}: {e}"
                return entry
            entry["data_base64"] = _b64.b64encode(bytes(buf)).decode("ascii")
            entry["size_bytes"] = entry.get("size_bytes") or len(buf)
            return entry

        results = await asyncio.gather(*[_fetch_one(a) for a in attachments])
        return list(results)

