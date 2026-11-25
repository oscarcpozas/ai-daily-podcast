"""The agent loop with the gateway helpers: what the model may run, and what it gets back."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from daily_podcast.aixy import LocalTool, run_agent
from daily_podcast.aixy.agent import gateway_tools
from daily_podcast.aixy.capabilities import INTERACTIVE
from daily_podcast.aixy.mcp import EXECUTE, SEARCH
from tests.test_capabilities import GatewayFake


def call(name: str, arguments: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"call-{name}",
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments or {})),
    )


class ScriptedLLM:
    """Replays one assistant turn per request and keeps what the model was shown."""

    def __init__(self, *turns: SimpleNamespace) -> None:
        self.turns = list(turns)
        self.messages: list[list[dict[str, Any]]] = []
        self.specs: list[list[str]] = []

    async def chat(self, messages, *, tools=None, max_tokens=None):
        self.messages.append([dict(message) for message in messages])
        self.specs.append([tool["function"]["name"] for tool in tools or []])
        return SimpleNamespace(content="", tool_calls=[self.turns.pop(0)])


async def _echo(arguments: dict[str, Any]) -> dict[str, Any]:
    return arguments


def submit_tool() -> LocalTool:
    return LocalTool(
        "submit",
        "Hand over the result.",
        {"type": "object", "properties": {}},
        _echo,
        terminal=True,
    )


async def test_a_refused_action_goes_back_to_the_model_and_the_scope_holds():
    tools = GatewayFake()
    llm = ScriptedLLM(
        call(EXECUTE, {"name": "slack.post_message", "arguments": {"text": "hi"}}),
        call(EXECUTE, {"name": "exa.get_contents", "arguments": {"urls": ["https://example.com"]}}),
        call("submit", {"ok": True}),
    )
    result = await run_agent(
        llm,
        system="read",
        user="go",
        tools=[
            *gateway_tools(tools, provider={"exa"}, max_chars=200, refuse=INTERACTIVE),
            submit_tool(),
        ],
    )

    assert result == {"ok": True}
    # Only the in-scope action reached Aixy; Slack never did.
    assert tools.calls == [
        (EXECUTE, {"name": "exa.get_contents", "arguments": {"urls": ["https://example.com"]}})
    ]
    feedback = [m["content"] for m in llm.messages[1] if m["role"] == "tool"]
    assert feedback and feedback[0].startswith("ERROR:")


async def test_the_agent_still_sees_discovery_next_to_its_terminal_tool():
    llm = ScriptedLLM(call("submit", {"ok": True}))
    await run_agent(
        llm,
        system="read",
        user="go",
        tools=[*gateway_tools(GatewayFake(), provider={"exa"}, max_chars=200), submit_tool()],
    )
    assert llm.specs[0] == [SEARCH, EXECUTE, "submit"]
