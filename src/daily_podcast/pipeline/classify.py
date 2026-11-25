"""Stage 2 — classify: category, sentiment and relevance for every article."""

from __future__ import annotations

import asyncio
import json
import logging

from daily_podcast.aixy import AixyLLM, LLMOutputError
from daily_podcast.aixy.llm import AixyInferenceError
from daily_podcast.models import Article, ArticleSet, Classification, Story, StorySet
from daily_podcast.prompts import render

log = logging.getLogger(__name__)


def _article_payload(article: Article) -> str:
    return json.dumps(
        {
            "source": article.source,
            "title": article.title,
            "summary": article.summary,
            "key_facts": article.key_facts,
        },
        ensure_ascii=False,
        indent=2,
    )


async def classify(llm: AixyLLM, articles: ArticleSet, *, concurrency: int) -> StorySet:
    """Classify every article — but only the ones the researcher did not classify while reading.

    The researcher already reads the full article, so it classifies for free; this stage is the
    fallback (and the legacy path for checkpoints written before that existed).
    """
    system = render("classify")
    semaphore = asyncio.Semaphore(concurrency)
    pending = [a for a in articles.articles if a.classification is None]

    async def one(article: Article) -> Story | None:
        async with semaphore:
            try:
                result = await llm.complete_json(
                    system=system,
                    user=_article_payload(article),
                    schema=Classification,
                    max_tokens=1500,
                )
            except (LLMOutputError, AixyInferenceError) as exc:
                log.warning("Could not classify %s: %s", article.url, exc)
                return None
            return Story(article=article, classification=result)

    results = await asyncio.gather(*(one(article) for article in pending))
    classified = {story.article.url: story for story in results if story is not None}
    stories: list[Story] = []
    for article in articles.articles:
        if article.classification is not None:
            stories.append(Story(article=article, classification=article.classification))
        elif (story := classified.get(article.url)) is not None:
            stories.append(story)
    return StorySet(stories=stories)
