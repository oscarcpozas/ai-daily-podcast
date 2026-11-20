"""Stage 1 — gather: one research agent per trusted source, scraping with TinyFish via Aixy."""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from daily_podcast.aixy import AgentError, AixyLLM, AixyTools, LocalTool, ToolInputError, run_agent
from daily_podcast.aixy.agent import gateway_tools
from daily_podcast.aixy.llm import AixyInferenceError
from daily_podcast.aixy.mcp import AixyToolError
from daily_podcast.models import Article, ArticleSet, Source
from daily_podcast.prompts import render
from daily_podcast.textutil import spanish_date

log = logging.getLogger(__name__)

SCRAPER_PROVIDER = "tinyfish"
# Page content is untrusted: the researcher may only read pages, never run anything else.
READ_ACTIONS = {"tinyfish.fetch_content"}

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
                },
                "required": ["url", "title", "summary"],
            },
        }
    },
    "required": ["articles"],
}


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
            accepted.append(Article.model_validate({**item, "url": url, "source": source.name}))
        except ValueError:
            rejected.append(url)
    return accepted[: source.max_articles], rejected


async def gather_source(
    llm: AixyLLM, tools: AixyTools, source: Source, *, day: date, max_chars: int
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
            tools, provider=SCRAPER_PROVIDER, max_chars=max_chars, allowed_actions=READ_ACTIONS
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


async def gather(
    llm: AixyLLM,
    tools: AixyTools,
    sources: list[Source],
    *,
    day: date,
    max_chars: int,
    concurrency: int,
) -> ArticleSet:
    semaphore = asyncio.Semaphore(concurrency)

    async def one(source: Source) -> list[Article]:
        async with semaphore:
            try:
                articles = await gather_source(llm, tools, source, day=day, max_chars=max_chars)
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
