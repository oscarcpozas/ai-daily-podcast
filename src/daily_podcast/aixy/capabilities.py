"""What the pipeline may use, discovered from Aixy at runtime.

No provider is named here. The pipeline asks Aixy which connected provider fulfils a capability
(reading a web page, searching the web, listing voices) and works with whatever answers, so a
connector can be swapped in Aixy — Exa for TinyFish, or a different speech provider — without
touching this code. The agent still discovers actions itself; these helpers only scope what it
is allowed to run.
"""

from __future__ import annotations

import logging

from daily_podcast.aixy.mcp import Action, AixyToolError, AixyTools

log = logging.getLogger(__name__)

#: Aixy's discovery is a semantic search over action descriptions, so queries are in English.
WEB_QUERY = "read the readable content of a web page given its URL, and search the web"
WEB_CATEGORY = "web_search"

#: Browser automation acts on the page and is metered. The researcher only reads.
INTERACTIVE = ("automation", "browser", "click", "login", "screenshot")


def is_interactive(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in INTERACTIVE)


async def web_actions(tools: AixyTools, *, limit: int = 10) -> list[Action]:
    """Every action Aixy offers for the web, whatever provider serves it."""
    return await tools.search(WEB_QUERY, category=WEB_CATEGORY, limit=limit)


async def web_providers(tools: AixyTools, *, limit: int = 10) -> frozenset[str]:
    """The providers that can read the web for this account, as Aixy reports them."""
    actions = await web_actions(tools, limit=limit)
    return frozenset(a.provider for a in actions if a.provider and not is_interactive(a.name))


async def resolve_action(
    tools: AixyTools,
    candidates: tuple[str, ...],
    *,
    query: str,
    provider: str | None = None,
) -> str:
    """The action Aixy exposes for a capability, preferring the names this code knows.

    Discovery has the last word: if the provider renamed or re-exposed its actions, the one that
    answers the capability is used instead of failing on a stale hardcoded name.
    """
    for name in candidates:
        try:
            await tools.describe(name)
        except AixyToolError as exc:
            log.info("%s is not in the Aixy catalog (%s); discovering instead", name, exc)
            continue
        return name

    for action in await tools.search(query, provider=provider, limit=5):
        if provider is None or action.provider.lower() == provider.lower():
            log.info("Using the discovered action %s for %r", action.name, query)
            return action.name
    raise AixyToolError(
        f"Aixy exposes no action for {query!r} (tried {', '.join(candidates)}). "
        "Check the connector in Aixy."
    )
