"""The pipeline names no provider: discovery decides which tools a stage may run."""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp import types

from daily_podcast.aixy.agent import ToolInputError, gateway_tools
from daily_podcast.aixy.capabilities import (
    INTERACTIVE,
    is_interactive,
    resolve_action,
    web_actions,
    web_providers,
)
from daily_podcast.aixy.mcp import EXECUTE, SEARCH, Action, AixyToolError, ToolOutput
from daily_podcast.pipeline.gather import web_scope


class GatewayFake:
    """The slice of AixyTools the gateway helpers and the capability helpers use."""

    def __init__(
        self,
        actions: list[Action] | None = None,
        *,
        described: tuple[str, ...] = (),
    ) -> None:
        self.actions = actions or []
        self.described = set(described)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def helper(self, name: str) -> types.Tool:
        return types.Tool(name=name, description=name, inputSchema={"type": "object"})

    async def call_helper(self, name: str, arguments: dict[str, Any]) -> ToolOutput:
        self.calls.append((name, arguments))
        catalog = [
            {"name": a.name, "description": a.description, "inputSchema": a.input_schema}
            for a in self.actions
        ]
        return ToolOutput(texts=[json.dumps({"tools": catalog})])

    async def search(
        self,
        query: str = "",
        *,
        provider: str | None = None,
        category: str | None = None,
        limit: int = 5,
    ) -> list[Action]:
        self.calls.append((SEARCH, {"query": query, "provider": provider, "category": category}))
        return self.actions

    async def describe(self, name: str) -> Action:
        if name in self.described:
            return Action(name)
        raise AixyToolError(f"{name} is not in the catalog")


# --- scoping what a stage may run -------------------------------------------------------------


async def test_actions_belonging_to_another_provider_are_refused():
    tools = GatewayFake()
    _, execute = gateway_tools(tools, provider={"tinyfish", "exa"}, max_chars=100)
    with pytest.raises(ToolInputError, match=r"got 'slack\.post_message'"):
        await execute.handler({"name": "slack.post_message", "arguments": {}})
    assert tools.calls == []


async def test_a_scoped_action_runs_through_aixy():
    tools = GatewayFake()
    _, execute = gateway_tools(tools, provider="exa", max_chars=100)
    await execute.handler({"name": "exa.get_contents", "arguments": {"urls": ["https://x"]}})
    assert tools.calls == [
        (EXECUTE, {"name": "exa.get_contents", "arguments": {"urls": ["https://x"]}})
    ]


async def test_interactive_automation_is_refused_even_inside_the_scope():
    tools = GatewayFake()
    _, execute = gateway_tools(tools, provider="tinyfish", max_chars=100, refuse=INTERACTIVE)
    with pytest.raises(ToolInputError, match="not allowed"):
        await execute.handler({"name": "tinyfish.run_web_automation", "arguments": {}})
    assert tools.calls == []


def test_an_unscoped_execute_tool_is_never_handed_to_the_model():
    with pytest.raises(ValueError, match="scope"):
        gateway_tools(GatewayFake(), provider=None, max_chars=100)


async def test_search_stays_inside_a_single_provider_and_opens_up_for_several():
    tools = GatewayFake()
    search_one, _ = gateway_tools(tools, provider="ElevenLabs", max_chars=100)
    await search_one.handler({"query": "voices"})
    assert tools.calls[-1] == (SEARCH, {"query": "voices", "provider": "elevenlabs"})

    search_many, _ = gateway_tools(tools, provider={"exa", "tinyfish"}, max_chars=100)
    await search_many.handler({"query": "web"})
    assert tools.calls[-1] == (SEARCH, {"query": "web"})


# --- discovering the capability ---------------------------------------------------------------


async def test_web_providers_come_from_discovery_not_from_a_constant():
    tools = GatewayFake(actions=[Action("exa.search"), Action("tinyfish.fetch_content")])
    assert await web_providers(tools) == frozenset({"exa", "tinyfish"})
    assert tools.calls[-1][1]["category"] == "web_search"


async def test_a_provider_that_only_offers_browser_automation_is_no_web_reader():
    tools = GatewayFake(actions=[Action("tinyfish.run_web_automation")])
    assert await web_providers(tools) == frozenset()
    assert is_interactive("tinyfish.run_web_automation")
    assert not is_interactive("exa.get_contents")


async def test_web_actions_are_reported_for_the_doctor():
    tools = GatewayFake(actions=[Action("exa.search")])
    assert [a.name for a in await web_actions(tools)] == ["exa.search"]


async def test_the_web_scope_is_discovered_or_pinned():
    tools = GatewayFake(actions=[Action("exa.search")])
    assert await web_scope(tools) == frozenset({"exa"})
    assert await web_scope(tools, "Tavily") == frozenset({"tavily"})


async def test_a_missing_web_connector_says_how_to_fix_it():
    with pytest.raises(AixyToolError, match="AIXY_WEB_PROVIDER"):
        await web_scope(GatewayFake())


# --- resolving an action the code knows by name -----------------------------------------------


async def test_resolve_action_prefers_the_known_name():
    tools = GatewayFake(described=("elevenlabs.creative_list_voices",))
    name = await resolve_action(
        tools, ("elevenlabs.creative_list_voices",), query="voices", provider="elevenlabs"
    )
    assert name == "elevenlabs.creative_list_voices"
    assert not any(call[0] == SEARCH for call in tools.calls)


async def test_resolve_action_falls_back_to_what_the_connector_exposes():
    tools = GatewayFake(actions=[Action("other.generate"), Action("elevenlabs.text_to_speech")])
    name = await resolve_action(
        tools, ("elevenlabs.creative_generate_speech",), query="speech", provider="elevenlabs"
    )
    assert name == "elevenlabs.text_to_speech"


async def test_resolve_action_fails_with_an_actionable_message():
    with pytest.raises(AixyToolError, match="no action"):
        await resolve_action(GatewayFake(), ("a.b",), query="speech")
