"""Stage 1 — gather: one research agent per trusted source, reading the web through Aixy.

The web connector is whatever the Aixy account has connected: Exa, TinyFish or another. This
stage never names one. It asks Aixy's discovery which providers can read the web, hands those to
the researcher and refuses everything else, so a connector swapped in Aixy needs no code change.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from daily_podcast.aixy import AgentError, AixyLLM, AixyTools, LocalTool, ToolInputError, run_agent
from daily_podcast.aixy.agent import gateway_tools
from daily_podcast.aixy.capabilities import INTERACTIVE, web_providers
from daily_podcast.aixy.llm import AixyInferenceError
from daily_podcast.aixy.mcp import AixyToolError
from daily_podcast.models import Article, ArticleSet, Classification, Source
from daily_podcast.prompts import render
from daily_podcast.textutil import spanish_date

log = logging.getLogger(__name__)

SUBMIT_ARTICLES: dict[str, Any] = {
    "type": "object",
    "properties": {
        "articles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "key_facts": {"type": "array", "items": {"type": "string"}},
                    "category": {
                        "type": "string",
                        "description": "One of the ten categories listed in your instructions.",
                    },
                    "sentiment": {
                        "type": "string",
                        "enum": ["positive", "neutral", "negative"],
                    },
                    "relevance": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5,
                        "description": "How newsworthy for a general audience in Spain today.",
                    },
                },
                "required": ["url", "title", "summary"],
            },
        }
    },
    "required": ["articles"],
}

_CLASSIFICATION_FIELDS = ("category", "sentiment", "relevance", "reason")


def _split_classification(item: dict[str, Any]) -> dict[str, Any]:
    """Move the researcher's classification fields into the article's ``classification``.

    An invalid one is dropped (and logged) rather than rejecting the article: the classify
    stage still runs for anything without a usable classification.
    """
    found = {key: item[key] for key in _CLASSIFICATION_FIELDS if key in item}
    article = {key: value for key, value in item.items() if key not in _CLASSIFICATION_FIELDS}
    if found:
        try:
            article["classification"] = Classification.model_validate(found)
        except ValidationError as exc:
            first = exc.errors()[0]
            # Without the offending value the warning is undebuggable: it is the only trace of a
            # model that invented a category or a sentiment.
            log.warning(
                "Dropping an invalid classification from the researcher's reply (%s: %s in %r)",
                first.get("msg", "invalid"),
                ".".join(str(part) for part in first.get("loc", ())),
                found,
            )
    return article


def _same_site(url: str, source: Source) -> bool:
    """Only http(s) links that stay on the source's own site can reach the show notes."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    site = (urlsplit(str(source.url)).hostname or "").removeprefix("www.")
    host = parts.hostname.removeprefix("www.")
    return host == site or host.endswith(f".{site}")


def _accept_articles(
    source: Source, day: date, raw: dict[str, Any]
) -> tuple[list[Article], list[str]]:
    pattern = source.pattern_for(day)
    accepted: list[Article] = []
    rejected: list[str] = []
    for item in raw.get("articles") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", "")).strip()
        if not _same_site(url, source) or (pattern and not pattern.match(url)):
            rejected.append(url)
            continue
        try:
            accepted.append(
                Article.model_validate(
                    _split_classification(item) | {"url": url, "source": source.name}
                )
            )
        except ValueError:
            rejected.append(url)
    return accepted[: source.max_articles], rejected


async def gather_source(
    llm: AixyLLM,
    tools: AixyTools,
    source: Source,
    *,
    day: date,
    max_chars: int,
    providers: frozenset[str],
) -> list[Article]:
    pattern = source.pattern_for(day)

    async def submit(arguments: dict[str, Any]) -> list[Article]:
        accepted, rejected = _accept_articles(source, day, arguments)
        if rejected and not accepted:
            raise ToolInputError(
                f"None of the URLs are valid article links of {source.name} "
                f"(they must stay on {source.url}"
                + (f" and match {pattern.pattern}" if pattern else "")
                + "). Pick article URLs that qualify."
            )
        if rejected:
            log.info("%s: dropped %d invalid or off-pattern URLs", source.name, len(rejected))
        return accepted

    tools_for_agent = [
        *gateway_tools(
            tools,
            provider=providers,
            max_chars=max_chars,
            refuse=INTERACTIVE,
        ),
        LocalTool(
            "submit_articles",
            "Hand over the articles you read. Call it exactly once, at the end.",
            SUBMIT_ARTICLES,
            submit,
            terminal=True,
        ),
    ]
    user = "\n".join(
        [
            f"Source: {source.name}",
            f"Front page: {source.url}",
            f"Article link pattern (regex): {pattern.pattern if pattern else 'none'}",
            f"Today: {day.isoformat()} ({spanish_date(day)})",
        ]
    )
    return await run_agent(
        llm,
        system=render("gather", max_articles=source.max_articles, today=day.isoformat()),
        user=user,
        tools=tools_for_agent,
        max_steps=16,
    )


async def web_scope(tools: AixyTools, pinned: str | None = None) -> frozenset[str]:
    """The providers this run may read the web with.

    Discovered from Aixy, so the connector is a deployment choice: ``AIXY_WEB_PROVIDER`` only
    pins one when an account has several connected and the run must be deterministic.
    """
    if pinned:
        return frozenset({pinned.strip().lower()})
    providers = await web_providers(tools)
    if not providers:
        raise AixyToolError(
            "Aixy offers no web tools to this account: connect a web connector (Exa, TinyFish…) "
            "in Aixy, or pin one with AIXY_WEB_PROVIDER."
        )
    return providers


async def gather(
    llm: AixyLLM,
    tools: AixyTools,
    sources: list[Source],
    *,
    day: date,
    max_chars: int,
    concurrency: int,
    web_provider: str | None = None,
) -> ArticleSet:
    providers = await web_scope(tools, web_provider)
    log.info("Reading the web with %s through Aixy", ", ".join(sorted(providers)))
    semaphore = asyncio.Semaphore(concurrency)

    async def one(source: Source) -> list[Article]:
        async with semaphore:
            try:
                articles = await gather_source(
                    llm, tools, source, day=day, max_chars=max_chars, providers=providers
                )
            except (AgentError, AixyInferenceError, AixyToolError) as exc:
                log.warning("%s: skipped (%s)", source.name, exc)
                return []
            log.info("%s: %d articles", source.name, len(articles))
            return articles

    results = await asyncio.gather(*(one(source) for source in sources))
    seen: set[str] = set()
    articles: list[Article] = []
    for article in (a for batch in results for a in batch):
        if article.url not in seen:
            seen.add(article.url)
            articles.append(article)
    return ArticleSet(articles=articles)
