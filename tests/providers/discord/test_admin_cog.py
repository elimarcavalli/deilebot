"""Smoke tests for AdminCog/EventsCog construction (no live discord)."""

from __future__ import annotations


class TestAdminCogShape:
    def test_admin_cog_attaches_subcommands(self):
        """Verify AdminCog has the expected hybrid commands declared."""
        from deilebot.providers.discord.cogs.admin_cog import AdminCog

        # AdminCog declares hybrid_command(s); these end up as class attrs
        names = set()
        for attr_name in dir(AdminCog):
            attr = getattr(AdminCog, attr_name)
            if hasattr(attr, "name") and not attr_name.startswith("_"):
                names.add(getattr(attr, "name", None))
        assert "dlq" in names or "metrics" in names or "audit" in names


class TestEventsCogShape:
    def test_events_cog_construct(self):
        from deilebot.providers.discord.cogs.events_cog import EventsCog

        cog = EventsCog.__new__(EventsCog)
        cog.bot = None
        cog.runtime = None
        cog.adapter = None
        # Should expose listener methods
        assert hasattr(cog, "on_member_join")
        assert hasattr(cog, "on_thread_create")


class TestAdapterCogWiring:
    """Verify adapter.start() registers AdminCog and EventsCog.

    The on_ready closure is not directly invocable without a live Discord
    client, so we inspect the source of DiscordAdapter.start to assert the
    wiring is present. Regressions (e.g. someone removing add_cog) will fail.
    """

    def test_admin_cog_is_wired(self):
        import inspect

        from deilebot.providers.discord.adapter import DiscordAdapter

        src = inspect.getsource(DiscordAdapter.start)
        assert "AdminCog" in src
        assert "add_cog(AdminCog(" in src

    def test_events_cog_is_wired(self):
        import inspect

        from deilebot.providers.discord.adapter import DiscordAdapter

        src = inspect.getsource(DiscordAdapter.start)
        assert "EventsCog" in src
        assert "add_cog(EventsCog(" in src
