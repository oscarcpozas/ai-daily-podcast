"""Stage 3 — select: a deterministic editorial pass over the classified stories.

No model call here: ranking is cheap, explainable and easy to tune from the CLI.
"""

from __future__ import annotations

from collections import defaultdict

from daily_podcast.models import Category, Selection, Sentiment, Story, StorySet
from daily_podcast.textutil import title_similarity

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
