"""A minimal tool-calling agent loop on top of Aixy inference and Aixy MCP tools."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from daily_podcast.aixy.mcp import EXECUTE, SEARCH, AixyToolError, AixyTools
from daily_podcast.errors import PodcastError
from daily_podcast.textutil import truncate

Handler = Callable[[dict[str, Any]], Awaitable[Any]]


class AgentError(PodcastError):
    """The agent did not finish its task."""


class ToolInputError(ValueError):
    """Invalid tool arguments. The message is sent back to the model so it can correct itself."""


@dataclass(frozen=True)
class LocalTool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    terminal: bool = False
    """Calling a terminal tool successfully ends the loop and returns the handler's value."""

    def spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _assistant_message(message: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
    calls = [call for call in (message.tool_calls or []) if call.type == "function"]
    if calls:
        entry["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.function.name, "arguments": call.function.arguments},
            }
            for call in calls
        ]
    # Reasoning models served through Chat Completions may need their reasoning echoed back.
    reasoning = (getattr(message, "model_extra", None) or {}).get("reasoning_content")
    if reasoning:
        entry["reasoning_content"] = reasoning
    return entry


async def run_agent(
    llm: Any,
    *,
    system: str,
    user: str,
    tools: Sequence[LocalTool],
    max_steps: int = 20,
) -> Any:
    """Run the model with tools until it calls a terminal tool. Returns that tool's value."""
    by_name = {tool.name: tool for tool in tools}
    terminal_names = [tool.name for tool in tools if tool.terminal]
    if not terminal_names:
        raise ValueError("run_agent needs at least one terminal tool")
    specs = [tool.spec() for tool in tools]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    nudged = False

    for _ in range(max_steps):
        message = await llm.chat(messages, tools=specs)
        entry = _assistant_message(message)
        messages.append(entry)
        calls = [call for call in (message.tool_calls or []) if call.type == "function"]

        if not calls:
            if nudged:
                raise AgentError("The model stopped without calling " + " or ".join(terminal_names))
            nudged = True
            messages.append(
                {
                    "role": "user",
                    "content": f"Finish by calling {' or '.join(terminal_names)} with your result.",
                }
            )
            continue

        for call in calls:
            tool = by_name.get(call.function.name)
            try:
                if tool is None:
                    raise ToolInputError(f"Unknown tool {call.function.name!r}")
                try:
                    arguments = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError as exc:
                    raise ToolInputError(f"Arguments are not valid JSON: {exc}") from exc
                if not isinstance(arguments, dict):
                    raise ToolInputError("Arguments must be a JSON object")
                value = await tool.handler(arguments)
            except ToolInputError as exc:
                content = f"ERROR: {exc}"
            else:
                if tool.terminal:
                    return value
                content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})

    raise AgentError(f"No result after {max_steps} steps")


def gateway_tools(
    tools: AixyTools,
    *,
    provider: str,
    max_chars: int,
    allowed_actions: set[str] | None = None,
) -> list[LocalTool]:
    """Expose Aixy's discovery and execution helpers to the model, pinned to one provider.

    The model discovers the provider's actions by itself; the pin keeps a news-reading agent
    from touching anything else the account has connected (Slack, Notion…). ``allowed_actions``
    narrows execution further, e.g. to read-only actions in a step that must not spend credits.
    """
    prefix = f"{provider.lower()}."

    async def search(arguments: dict[str, Any]) -> str:
        out = await tools.call_helper(SEARCH, {**arguments, "provider": provider})
        return truncate(out.text, max_chars)

    async def execute(arguments: dict[str, Any]) -> str:
        name = str(arguments.get("name", ""))
        if not name.lower().startswith(prefix):
            raise ToolInputError(f"Only {provider} actions are allowed here; got {name!r}")
        if allowed_actions is not None and name not in allowed_actions:
            allowed = ", ".join(sorted(allowed_actions))
            raise ToolInputError(f"{name!r} is not allowed in this step. Allowed: {allowed}")
        try:
            out = await tools.call_helper(EXECUTE, arguments)
        except AixyToolError as exc:
            raise ToolInputError(str(exc)) from exc
        text = truncate(out.text, max_chars)
        return f"ERROR: {text}" if out.is_error else text

    search_helper = tools.helper(SEARCH)
    execute_helper = tools.helper(EXECUTE)
    return [
        LocalTool(
            SEARCH,
            search_helper.description or "Discover actions.",
            search_helper.input_schema,
            search,
        ),
        LocalTool(
            EXECUTE,
            execute_helper.description or "Execute a discovered action.",
            execute_helper.input_schema,
            execute,
        ),
    ]
