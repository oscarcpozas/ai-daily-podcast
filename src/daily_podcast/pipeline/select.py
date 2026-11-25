"""Stage 3 — select: a deterministic editorial pass over the classified stories.

No model call by default: ranking is cheap, explainable and easy to tune from the CLI.
``--editorial`` adds one optional agent on top — an editor's eye that may reorder, drop or swap
stories. It can only pick from what the deterministic pass ranked, and any failure falls back
to the deterministic selection, so the episode never depends on it.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date

from pydantic import BaseModel, Field

from daily_podcast.aixy import AixyInferenceError, AixyLLM, LLMOutputError
from daily_podcast.config import Settings
from daily_podcast.models import Category, Selection, Sentiment, Story, StorySet
from daily_podcast.prompts import render
from daily_podcast.textutil import spanish_date, title_similarity

log = logging.getLogger(__name__)

# Order in which categories are read on air.
RUNNING_ORDER: tuple[Category, ...] = (
    Category.GENERAL,
    Category.POLITICS,
    Category.INTERNATIONAL,
    Category.ECONOMY,
    Category.TECHNOLOGY,
    Category.SCIENCE,
    Category.CULTURE,
    Category.ENTERTAINMENT,
    Category.SPORTS,
    Category.WEATHER,
)

DUPLICATE_THRESHOLD = 0.5


def _score(story: Story) -> tuple[int, int]:
    # Higher relevance first; on ties, prefer stories with more concrete facts.
    return story.classification.relevance, len(story.article.key_facts)


def _deduplicate(stories: list[Story]) -> list[Story]:
    """Drop the same event reported by several sources, keeping the best-scored version."""
    kept: list[Story] = []
    for story in sorted(stories, key=_score, reverse=True):
        title = story.article.title
        if all(
            title_similarity(title, other.article.title) < DUPLICATE_THRESHOLD for other in kept
        ):
            kept.append(story)
    return kept


def select(
    stories: StorySet,
    *,
    max_stories: int = 10,
    per_category: int = 3,
    min_relevance: int = 2,
) -> Selection:
    candidates = _deduplicate(
        [s for s in stories.stories if s.classification.relevance >= min_relevance]
    )

    by_category: dict[Category, list[Story]] = defaultdict(list)
    for story in candidates:  # already sorted by score
        if len(by_category[story.classification.category]) < per_category:
            by_category[story.classification.category].append(story)

    # Take the best stories overall, then read them in running order.
    chosen = sorted(
        (story for bucket in by_category.values() for story in bucket), key=_score, reverse=True
    )[:max_stories]
    chosen_urls = {story.article.url for story in chosen}

    good_news = next(
        (
            story
            for story in candidates
            if story.classification.sentiment is Sentiment.POSITIVE
            and story.article.url not in chosen_urls
        ),
        None,
    )

    order = {category: index for index, category in enumerate(RUNNING_ORDER)}
    chosen.sort(key=lambda s: (order[s.classification.category], -s.classification.relevance))
    return Selection(stories=chosen, good_news=good_news)


# --- the optional editorial agent -------------------------------------------------------------


class EditorialChoice(BaseModel):
    """What the editorial agent may change about the deterministic selection."""

    order: list[str] = Field(
        description="Article URLs to read on air, best order for the listener."
    )
    good_news: str | None = Field(
        default=None, description="URL of a positive story to close with."
    )
    note: str = ""


def runners_up(stories: StorySet, selection: Selection, *, count: int = 5) -> list[Story]:
    """The best stories that did not make the deterministic cut, for an editor to consider."""
    chosen = {story.article.url for story in selection.stories}
    if selection.good_news is not None:
        chosen.add(selection.good_news.article.url)
    rest = [story for story in stories.stories if story.article.url not in chosen]
    return sorted(rest, key=_score, reverse=True)[:count]


def _editor_payload(story: Story) -> dict[str, object]:
    return {
        "url": story.article.url,
        "title": story.article.title,
        "summary": story.article.summary,
        "source": story.article.source,
        "category": story.classification.category.label_es,
        "sentiment": story.classification.sentiment.value,
        "relevance": story.classification.relevance,
    }


def editorial_brief(selection: Selection, alternates: list[Story]) -> str:
    """The JSON brief the editor sees: the deterministic lineup plus the alternates."""
    return json.dumps(
        {
            "selected": [_editor_payload(story) for story in selection.stories],
            "alternates": [_editor_payload(story) for story in alternates],
            "current_good_news": (
                _editor_payload(selection.good_news) if selection.good_news else None
            ),
        },
        ensure_ascii=False,
        indent=2,
    )


async def editorial_pass(
    llm: AixyLLM,
    selection: Selection,
    *,
    alternates: list[Story],
    day: date,
    settings: Settings,
) -> Selection:
    """One editorial eye over the deterministic lineup. Falls back to it on any failure."""
    try:
        choice = await llm.complete_json(
            system=render("editorial", show_name=settings.show_name, today=spanish_date(day)),
            user=editorial_brief(selection, alternates),
            schema=EditorialChoice,
            max_tokens=1200,
        )
    except (LLMOutputError, AixyInferenceError) as exc:
        log.warning("The editorial pass failed (%s); keeping the deterministic selection", exc)
        return selection

    pool_stories = [*selection.stories, *alternates]
    if selection.good_news is not None:
        pool_stories.append(selection.good_news)  # the editor may keep the current closer
    pool = {story.article.url: story for story in pool_stories}
    chosen = [pool[url] for url in dict.fromkeys(choice.order) if url in pool]
    if not chosen:
        log.warning("The editorial choice used no known URL; keeping the deterministic selection")
        return selection

    closer = pool.get(choice.good_news or "")
    on_air = {story.article.url for story in chosen}
    if closer is None or closer.article.url in on_air:
        closer = None  # a story cannot close the episode and air in the body
    elif closer.classification.sentiment is not Sentiment.POSITIVE:
        closer = None  # the closer is "la buena noticia": only a positive story may close
    if choice.note:
        log.info("Editor: %s", choice.note)
    return Selection(stories=chosen, good_news=closer)
