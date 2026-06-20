"""AC-3b: DiscordNormalizer captures guild.preferred_locale into envelope.raw['guild_locale'].

- Message with guild.preferred_locale="pt-BR" → envelope carries raw["guild_locale"]="pt-BR"
- DM (no guild) → raw["guild_locale"] is absent / None
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from deilebot.foundation.envelope import ChannelScope
from deilebot.providers.discord.normalizer import DiscordNormalizer


def _user(uid=111, name="alice", bot=False):
    return SimpleNamespace(id=uid, name=name, display_name=name, bot=bot)


def _make_channel(cls_name: str, cid: int, name=None, parent=None):
    cls = type(cls_name, (), {})
    obj = cls()
    obj.id = cid
    obj.name = name
    obj.parent = parent
    return obj


def _msg_with_guild(guild, channel=None):
    return SimpleNamespace(
        id=999,
        content="oi",
        channel=channel or _make_channel("TextChannel", 1, name="geral"),
        author=_user(),
        attachments=[],
        reference=None,
        mentions=[],
        created_at=datetime.now(timezone.utc),
        guild=guild,
    )


class TestNormalizerLocale:
    def test_guild_locale_ptbr_captured_in_raw(self):
        """Message with guild.preferred_locale='pt-BR' → raw['guild_locale']='pt-BR'."""
        guild = SimpleNamespace(id=1234, preferred_locale="pt-BR")
        env = DiscordNormalizer().to_envelope(_msg_with_guild(guild))
        assert env.raw.get("guild_locale") == "pt-BR", (
            f"Expected 'pt-BR', got {env.raw.get('guild_locale')!r}"
        )

    def test_guild_locale_en_us_captured_in_raw(self):
        """Message with guild.preferred_locale='en-US' → raw['guild_locale']='en-US'."""
        guild = SimpleNamespace(id=5678, preferred_locale="en-US")
        env = DiscordNormalizer().to_envelope(_msg_with_guild(guild))
        assert env.raw.get("guild_locale") == "en-US"

    def test_guild_locale_de_de_captured_in_raw(self):
        """Message with guild.preferred_locale='de-DE' → raw['guild_locale']='de-DE'."""
        guild = SimpleNamespace(id=9999, preferred_locale="de-DE")
        env = DiscordNormalizer().to_envelope(_msg_with_guild(guild))
        assert env.raw.get("guild_locale") == "de-DE"

    def test_dm_no_guild_locale_absent(self):
        """DM (guild=None) → raw['guild_locale'] is None (absent)."""
        ch = _make_channel("DMChannel", 5)
        env = DiscordNormalizer().to_envelope(_msg_with_guild(None, channel=ch))
        assert env.raw.get("guild_locale") is None, (
            f"Expected None for DM, got {env.raw.get('guild_locale')!r}"
        )

    def test_dm_scope_is_dm(self):
        """DM channel → scope=DM, guild_locale absent."""
        ch = _make_channel("DMChannel", 5)
        env = DiscordNormalizer().to_envelope(_msg_with_guild(None, channel=ch))
        assert env.channel.scope == ChannelScope.DM
        assert env.raw.get("guild_locale") is None

    def test_guild_locale_preserved_in_raw_mapping(self):
        """Verify guild_locale is accessible via env.raw (a Mapping)."""
        guild = SimpleNamespace(id=42, preferred_locale="pt-BR")
        env = DiscordNormalizer().to_envelope(_msg_with_guild(guild))
        # Must be accessible as a mapping key
        assert "guild_locale" in env.raw
        assert env.raw["guild_locale"] == "pt-BR"
