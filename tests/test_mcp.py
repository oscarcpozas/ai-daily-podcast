from __future__ import annotations

import httpx2
import pytest
from mcp import types

from daily_podcast.aixy import AixyToolError, AixyTools
from daily_podcast.aixy.mcp import EXECUTE, SEARCH
from daily_podcast.config import Settings


def reply(text: str, *, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=error)


class ScriptedSession:
    """Answers each call with the next scripted reply; a reply that is an exception is raised."""

    def __init__(self, *replies: object) -> None:
        self.replies = list(replies)
        self.calls: list[str] = []

    async def call_tool(self, name: str, arguments: dict) -> types.CallToolResult:
        self.calls.append(name)
        item = self.replies.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item  # type: ignore[return-value]


def tools_with(session: ScriptedSession) -> AixyTools:
    tools = AixyTools(Settings(), session=session)  # type: ignore[arg-type]
    tools.retry_delays = (0, 0, 0)
    return tools


async def test_discovery_is_retried_while_aixy_reports_a_transient_state():
    session = ScriptedSession(
        reply("Discovery capacity is busy", error=True),
        reply("Discovery is awaiting authorized reconciliation", error=True),
        reply('{"tools": []}'),
    )
    out = await tools_with(session).call_helper(SEARCH, {"query": "x"})
    assert not out.is_error
    assert session.calls == [SEARCH] * 3


async def test_discovery_gives_up_after_the_last_delay():
    busy = reply("Discovery capacity is busy", error=True)
    session = ScriptedSession(busy, busy, busy, busy)
    out = await tools_with(session).call_helper(SEARCH, {"query": "x"})
    assert out.is_error
    assert len(session.calls) == 4  # first attempt + 3 retries


async def test_execution_is_retried_when_the_gateway_refused_to_process_it():
    """ "Processing capacity is unavailable" means nothing ran; retrying cannot pay twice."""
    session = ScriptedSession(
        reply("MCP processing capacity is unavailable", error=True),
        reply("ok"),
    )
    tools = tools_with(session)
    tools.execute_retry_delays = (0, 0, 0)
    out = await tools.call_helper(EXECUTE, {"name": "a.b", "arguments": {}})
    assert not out.is_error
    assert session.calls == [EXECUTE, EXECUTE]


async def test_execution_gives_up_after_its_own_delay_ladder():
    refusal = reply("MCP processing capacity is unavailable", error=True)
    session = ScriptedSession(refusal, refusal, refusal, refusal)
    tools = tools_with(session)
    tools.execute_retry_delays = (0, 0, 0)
    out = await tools.call_helper(EXECUTE, {"name": "a.b", "arguments": {}})
    assert out.is_error
    assert len(session.calls) == 4  # first attempt + 3 retries


async def test_execution_is_never_retried_even_on_a_transient_looking_error():
    """EXECUTE can spend credits: a retry could pay twice."""
    session = ScriptedSession(reply("Discovery capacity is busy", error=True), reply("ok"))
    out = await tools_with(session).call_helper(EXECUTE, {"name": "a.b", "arguments": {}})
    assert out.is_error
    assert session.calls == [EXECUTE]


async def test_a_non_transient_error_is_returned_at_once():
    session = ScriptedSession(reply("Unknown provider", error=True), reply("ok"))
    out = await tools_with(session).call_helper(SEARCH, {"query": "x"})
    assert out.is_error
    assert session.calls == [SEARCH]


@pytest.mark.parametrize(
    "failure",
    [
        httpx2.ConnectError("connection refused"),
        ExceptionGroup("taskgroup", [httpx2.ConnectError("unreachable")]),
        OSError("network is down"),
    ],
)
async def test_transport_failures_become_a_clean_aixy_error(failure):
    tools = tools_with(ScriptedSession(failure))
    with pytest.raises(AixyToolError, match=EXECUTE):
        await tools.call_helper(EXECUTE, {"name": "a.b", "arguments": {}})
