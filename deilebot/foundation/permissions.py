"""PermissionGate — owner/allowlist/blocklist enforcement."""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping, NamedTuple

from deilebot.foundation.envelope import BotUser, ChannelScope
from deilebot.foundation.identity import IdentityResolver
from deilebot.foundation.settings import BotSettings


class Action(str, Enum):
    READ_MESSAGE = "READ_MESSAGE"
    INVOKE_AGENT = "INVOKE_AGENT"
    SEND_DM = "SEND_DM"
    EXECUTE_TOOL = "EXECUTE_TOOL"
    ADMIN_COMMAND = "ADMIN_COMMAND"
    DEBUG_COMMAND = "DEBUG_COMMAND"


class PermissionDecision(NamedTuple):
    allowed: bool
    reason: str


class PermissionGate:
    """Decide if a user is allowed to perform an Action.

    Order: blocklist > owners > per_action > allowlist_invoke_agent.
    """

    def __init__(self, settings: BotSettings, identity: IdentityResolver):
        self._settings = settings
        self._identity = identity

    @property
    def perms(self):
        return self._settings.permissions

    def _user_matches(self, user: BotUser, ids: list) -> bool:
        """True if any identifier the user goes by appears in ``ids``.

        We try both forms because they answer different questions:
        ``bot_user_id`` is the internal ULID (random per first encounter,
        wiped when the conversation DB is rebuilt); ``<provider>:<id>``
        is stable for the lifetime of the upstream account, and is the
        form operators put in ``owners:``/``allowlist:`` so the YAML
        survives a fresh deployment.
        """
        return (
            user.bot_user_id in ids
            or f"{user.provider}:{user.provider_user_id}" in ids
        )

    def _is_in(self, user_or_id, ids: list) -> bool:
        if isinstance(user_or_id, BotUser):
            return self._user_matches(user_or_id, ids)
        return user_or_id in ids

    def _is_wildcard(self, ids: list) -> bool:
        return "*" in ids

    async def check(
        self,
        user: BotUser,
        action: Action,
        *,
        scope: ChannelScope = ChannelScope.DM,
        context: Mapping[str, Any] = {},
    ) -> PermissionDecision:
        perms = self.perms
        # 1. blocklist
        if self._is_in(user, perms.blocklist):
            return PermissionDecision(False, "blocklist")
        # 2. owner override
        if self._is_in(user, perms.owners):
            return PermissionDecision(True, "owner")
        # 3. per-action rule
        rule = perms.per_action.get(action.value)
        if rule is not None:
            if rule.mode == "owner_only":
                return PermissionDecision(False, "owner_only")
            if rule.mode == "allowlist":
                if self._is_in(user, rule.list) or self._is_wildcard(rule.list):
                    return PermissionDecision(True, "allowlist")
                return PermissionDecision(False, "not_in_allowlist")
            if rule.mode == "wildcard":
                return PermissionDecision(True, "wildcard")
        # 4. global default for INVOKE_AGENT
        if action == Action.INVOKE_AGENT:
            allowlist = perms.allowlist_invoke_agent
            if self._is_wildcard(allowlist) or self._is_in(user, allowlist):
                return PermissionDecision(True, "allowlist_default")
            return PermissionDecision(False, "not_in_invoke_allowlist")
        # 5. fallback for non-explicit actions: deny
        return PermissionDecision(False, "no_rule")

    async def is_owner(self, user: BotUser) -> bool:
        return self._is_in(user, self.perms.owners) and not self._is_in(
            user, self.perms.blocklist
        )
