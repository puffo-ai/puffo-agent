"""The connector tool registry is EXACTLY the known tools — no more, no less.

Each connector's own test checks its tools register and forward; this one owns
the cross-connector invariant the per-connector subset checks cannot see: a tool
accidentally registered (or silently dropped) from the shared registry. Adding a
connector means adding its names here on purpose — that is the coverage.
"""

from types import SimpleNamespace as NS

from puffo_agent.mcp.core_connector_tools import register_connector_tools

EXPECTED = {
    "slack_send", "slack_search", "slack_read", "slack_list_channels",
    "discord_whoami", "discord_guilds", "discord_guild_member", "discord_connections",
    "notion_search", "notion_read_page", "notion_create_page", "notion_update_page",
}


def _register(rpc_client):
    registered = {}

    class FakeMcp:
        def tool(self):
            def decorate(fn):
                registered[fn.__name__] = fn
                return fn
            return decorate

    register_connector_tools(FakeMcp(), NS(rpc_client=rpc_client))
    return registered


def test_no_link_registers_nothing():
    assert _register(None) == {}


def test_the_registry_is_exactly_the_known_connector_tools():
    async def slack_send(**kw):
        return {}

    async def connector(provider, op, **kw):
        return {}

    registered = set(_register(NS(slack_send=slack_send, connector=connector)))
    assert registered == EXPECTED
    # Positive control — the equality has teeth: a stray extra tool, or a
    # dropped one, would break it (the self-referential-coverage blind spot a
    # subset check leaves open).
    assert registered | {"ghost_tool"} != EXPECTED
    assert registered - {"slack_send"} != EXPECTED
