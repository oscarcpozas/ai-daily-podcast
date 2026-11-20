"""Tools through the Aixy MCP gateway.

Aixy exposes every connected provider (TinyFish, ElevenLabs, …) behind one MCP endpoint and
four helpers: ``aixy_search_tools`` discovers actions, ``aixy_execute_tool`` runs them,
``aixy_connections`` lists accounts and ``aixy_connect`` returns a link to connect one.
Provider credentials never touch this code: they live in Aixy.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self

import httpx2
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from daily_podcast import __version__
from daily_podcast.config import Settings

SEARCH = "aixy_search_tools"
EXECUTE = "aixy_execute_tool"
CONNECTIONS = "aixy_connections"

_URL = re.compile(r"https?://[^\s\"'<>)\]]+")
_SCHEMA_KEYS = ("inputSchema", "input_schema")

# Discovery answers these while it refreshes the caller's catalog; retrying is the documented fix.
_TRANSIENT_DISCOVERY = ("capacity is busy", "awaiting authorized reconciliation", "scope changed")
log = logging.getLogger(__name__)


class AixyToolError(RuntimeError):
    """The MCP gateway failed a request (protocol error, auth, discovery unavailable…)."""


@dataclass(frozen=True)
class Action:
    """A provider action discovered through Aixy, e.g. ``tinyfish.fetch_content``."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] | None = None

    @property
    def provider(self) -> str:
        return self.name.split(".", 1)[0] if "." in self.name else ""


@dataclass
class ToolOutput:
    texts: list[str] = field(default_factory=list)
    blobs: list[tuple[str, bytes]] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    structured: Any = None
    is_error: bool = False

    @property
    def text(self) -> str:
        return "\n".join(self.texts)

    @classmethod
    def from_result(cls, result: types.CallToolResult) -> ToolOutput:
        out = cls(structured=result.structured_content, is_error=bool(result.is_error))
        for item in result.content:
            if isinstance(item, types.TextContent):
                out.texts.append(item.text)
            elif isinstance(item, types.AudioContent | types.ImageContent):
                out.blobs.append((item.mime_type, base64.b64decode(item.data)))
            elif isinstance(item, types.EmbeddedResource):
                resource = item.resource
                if isinstance(resource, types.BlobResourceContents):
                    mime = resource.mime_type or "application/octet-stream"
                    out.blobs.append((mime, base64.b64decode(resource.blob)))
                else:
                    out.texts.append(resource.text)
            elif isinstance(item, types.ResourceLink):
                out.links.append(str(item.uri))
        return out

    def urls(self) -> list[str]:
        """Every URL in the output, explicit links first, without duplicates."""
        found = list(self.links)
        haystacks = list(self.texts)
        if self.structured is not None:
            haystacks.append(json.dumps(self.structured))
        for text in haystacks:
            found.extend(url.rstrip(".,;") for url in _URL.findall(text))
        return list(dict.fromkeys(found))


def parse_actions(text: str) -> list[Action]:
    """Extract actions from an ``aixy_search_tools`` reply, whatever its exact JSON shape."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []

    found: dict[str, Action] = {}

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for value in node:
                walk(value)
            return
        if not isinstance(node, dict):
            return
        name = node.get("name")
        if isinstance(name, str) and (
            "description" in node or any(k in node for k in _SCHEMA_KEYS)
        ):
            schema = next((node[k] for k in _SCHEMA_KEYS if isinstance(node.get(k), dict)), None)
            previous = found.get(name)
            if previous is None or (schema and not previous.input_schema):
                found[name] = Action(name, str(node.get("description") or ""), schema)
        for key, value in node.items():
            if key not in _SCHEMA_KEYS and isinstance(value, dict | list):
                walk(value)

    walk(data)
    return list(found.values())


def _is_transient(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _TRANSIENT_DISCOVERY)


class AixyTools:
    """One MCP session against Aixy, shared by every stage of a run."""

    #: Seconds to wait before each retry of a discovery call that failed transiently.
    retry_delays: tuple[float, ...] = (2, 5, 10, 20)

    def __init__(self, settings: Settings, *, session: ClientSession | None = None) -> None:
        self._settings = settings
        self._session = session
        self._stack: AsyncExitStack | None = None
        self._described: dict[str, Action] = {}
        self.helpers: dict[str, types.Tool] = {}

    async def __aenter__(self) -> Self:
        if self._session is None:
            self._settings.require_tools()
            stack = AsyncExitStack()
            try:
                http = httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {self._settings.aixy_mcp_token}"},
                    # Provider actions such as text-to-speech can take minutes to answer.
                    timeout=httpx2.Timeout(30, read=600),
                )
                await stack.enter_async_context(http)
                streams = await stack.enter_async_context(
                    streamable_http_client(self._settings.aixy_mcp_url, http_client=http)
                )
                session = ClientSession(
                    streams[0],
                    streams[1],
                    client_info=types.Implementation(name="ai-daily-podcast", version=__version__),
                )
                self._session = await stack.enter_async_context(session)
                await self._session.initialize()
            except BaseException:
                await stack.aclose()
                raise
            self._stack = stack
        listing = await self._session.list_tools()
        self.helpers = {tool.name: tool for tool in listing.tools}
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._stack is not None:
            await self._stack.aclose()

    def helper(self, name: str) -> types.Tool:
        try:
            return self.helpers[name]
        except KeyError as exc:
            raise AixyToolError(f"The Aixy MCP server does not expose {name!r}") from exc

    async def _call_once(self, name: str, arguments: dict[str, Any]) -> ToolOutput:
        if self._session is None:
            raise AixyToolError("AixyTools must be used as an async context manager")
        try:
            result = await self._session.call_tool(name, arguments)
        except MCPError as exc:
            raise AixyToolError(f"{name} failed: {exc}") from exc
        if not isinstance(result, types.CallToolResult):
            raise AixyToolError(f"{name} returned an unsupported result: {type(result).__name__}")
        return ToolOutput.from_result(result)

    async def call_helper(self, name: str, arguments: dict[str, Any]) -> ToolOutput:
        """Call an Aixy helper. Discovery calls are retried while Aixy reports a transient state."""
        delays = self.retry_delays if name == SEARCH else ()
        for delay in (*delays, None):
            try:
                out = await self._call_once(name, arguments)
            except AixyToolError as exc:
                if delay is None or not _is_transient(str(exc)):
                    raise
                message = str(exc)
            else:
                if delay is None or not (out.is_error and _is_transient(out.text)):
                    return out
                message = out.text
            log.info("Aixy discovery is not ready (%s); retrying in %ss", message, delay)
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def search(
        self, query: str = "", *, provider: str | None = None, limit: int = 5
    ) -> list[Action]:
        arguments: dict[str, Any] = {"query": query, "limit": limit}
        if provider:
            arguments["provider"] = provider
        out = await self.call_helper(SEARCH, arguments)
        if out.is_error:
            raise AixyToolError(f"{SEARCH} failed: {out.text}")
        return parse_actions(out.text)

    async def describe(self, name: str) -> Action:
        """Full definition of one action, input schema included. Cached for the session."""
        if name in self._described:
            return self._described[name]
        out = await self.call_helper(SEARCH, {"name": name})
        if out.is_error:
            raise AixyToolError(f"{SEARCH} could not describe {name!r}: {out.text}")
        for action in parse_actions(out.text):
            if action.name == name:
                self._described[name] = action
                return action
        raise AixyToolError(f"Action {name!r} was not found in the Aixy catalog")

    async def execute(self, name: str, arguments: dict[str, Any]) -> ToolOutput:
        return await self.call_helper(EXECUTE, {"name": name, "arguments": arguments})

    async def connections(self) -> list[dict[str, Any]]:
        out = await self.call_helper(CONNECTIONS, {})
        try:
            data = json.loads(out.text)
        except json.JSONDecodeError as exc:
            raise AixyToolError(f"Unexpected {CONNECTIONS} reply: {out.text[:200]}") from exc
        return data if isinstance(data, list) else []
