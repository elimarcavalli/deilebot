"""deilebot_client — thin async HTTP client for the deilebot control-plane.

Public surface:

    from deilebot_client import (
        BotControlClient, BotControlSettings,
        BotClientError, BotClientAuthError, BotClientTimeoutError,
        BotClientRateLimited, BotClientUpstreamError, BotClientNotReady,
    )
    from deilebot_client.models import (
        ChannelPostRequest, ChannelPostResponse,
        DMSendRequest, DMSendResponse,
        ReactionAddRequest, ReactionAddResponse,
        ThreadStartRequest, ThreadStartResponse,
        MessagePinRequest, MessagePinResponse,
        RoleMentionRequest, RoleMentionResponse,
        UserProfileResponse, HealthResponse, ErrorEnvelope,
    )

The client wraps `httpx.AsyncClient` with retry on 5xx/timeouts (tenacity)
and raises typed errors. No logging of tokens or full bodies.
"""

from __future__ import annotations

from .client import BotControlClient, BotControlSettings
from .errors import (BotClientAuthError, BotClientError, BotClientNotReady,
                     BotClientRateLimited, BotClientTimeoutError,
                     BotClientUpstreamError)

__all__ = [
    "BotControlClient",
    "BotControlSettings",
    "BotClientError",
    "BotClientAuthError",
    "BotClientNotReady",
    "BotClientRateLimited",
    "BotClientTimeoutError",
    "BotClientUpstreamError",
]

__version__ = "0.2.0"
