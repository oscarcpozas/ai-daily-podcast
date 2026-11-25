from datetime import date

import pytest

from daily_podcast.aixy import LLMOutputError
from daily_podcast.config import Settings
from daily_podcast.models import Article, Category, Classification, Sentiment, Story, StorySet
from daily_podcast.pipeline.select import RUNNING_ORDER, editorial_pass, runners_up, select

DAY = date(2026, 11, 25)


class EditorFake:
    """Answers complete_json with a scripted EditorialChoice (or raises)."""

    def __init__(self, choice: object) -> None:
        self.choice = choice
        self.calls = 0

    async def complete_json(self, *, system, user, schema, attempts=2, max_tokens=None):
        self.calls += 1
        if isinstance(self.choice, Exception):
            raise self.choice
        return schema.model_validate(self.choice)


def story(
    title: str,
    *,
    url: str | None = None,
    category: Category = Category.GENERAL,
    sentiment: Sentiment = Sentiment.NEUTRAL,
    relevance: int = 3,
    facts: int = 2,
) -> Story:
    return Story(
        article=Article(
            url=url or f"https://example.com/{abs(hash(title))}",
            source="Test",
            title=title,
            summary="s",
            key_facts=[f"fact {i}" for i in range(facts)],
        ),
        classification=Classification(category=category, sentiment=sentiment, relevance=relevance),
    )


def test_low_relevance_is_dropped():
    result = select(StorySet(stories=[story("Ruido total", relevance=1)]))
    assert result.stories == []


def test_duplicates_across_sources_keep_the_best_version():
    weak = story("Congreso aprueba presupuestos 2027", relevance=3, facts=1)
    strong = story("El Congreso aprueba los presupuestos 2027", relevance=5, facts=4)
    result = select(StorySet(stories=[weak, strong]))
    assert [s.article.title for s in result.stories] == [strong.article.title]


def test_per_category_cap_and_overall_cap():
    politics_titles = [
        "El Congreso debate la reforma electoral",
        "Moncloa anuncia un plan de vivienda",
        "La oposición pide elecciones anticipadas",
        "Hacienda revisa los impuestos autonómicos",
        "Sánchez comparece sobre los presupuestos",
        "El Senado aprueba la ley de transparencia",
    ]
    stories = [
        story(title, category=Category.POLITICS, relevance=5 - i % 2)
        for i, title in enumerate(politics_titles)
    ] + [story("Partido de fútbol en Madrid", category=Category.SPORTS, relevance=4)]
    result = select(StorySet(stories=stories), per_category=2, max_stories=10)
    politics = [s for s in result.stories if s.classification.category is Category.POLITICS]
    assert len(politics) == 2
    assert any(s.classification.category is Category.SPORTS for s in result.stories)


def test_output_follows_running_order():
    stories = [
        story("Deportes de hoy en el estadio", category=Category.SPORTS, relevance=5),
        story("Economía del país sube hoy mucho", category=Category.ECONOMY, relevance=4),
        story("Actualidad general del día hoy", category=Category.GENERAL, relevance=2),
    ]
    result = select(StorySet(stories=stories), min_relevance=1)
    order = [RUNNING_ORDER.index(s.classification.category) for s in result.stories]
    assert order == sorted(order)


def test_good_news_is_a_positive_story_not_already_on_air():
    on_air = story("Tema principal del día", relevance=5)
    uplifting = story("Una ONG rescata a un perro", sentiment=Sentiment.POSITIVE, relevance=2)
    result = select(StorySet(stories=[on_air, uplifting]), max_stories=1, min_relevance=1)
    assert [s.article.title for s in result.stories] == [on_air.article.title]
    assert result.good_news is not None
    assert result.good_news.article.title == uplifting.article.title


# --- the optional editorial agent -------------------------------------------------------------


def _lineup():
    """A deterministic selection of three, one positive alternate, and one spare."""
    first = story("Tema principal", url="https://x/1", relevance=5)
    second = story("Segunda noticia", url="https://x/2", relevance=4)
    third = story("Tercera noticia", url="https://x/3", relevance=3)
    closer = story("Buena noticia", url="https://x/4", sentiment=Sentiment.POSITIVE, relevance=2)
    spare = story("Sobrante", url="https://x/5", relevance=2)
    stories = StorySet(stories=[first, second, third, closer, spare])
    return select(stories, max_stories=2, min_relevance=1), stories


async def test_runners_up_are_the_best_stories_that_did_not_make_the_cut():
    selection, stories = _lineup()
    urls = [s.article.url for s in runners_up(stories, selection)]
    assert "https://x/5" in urls
    assert set(urls).isdisjoint({s.article.url for s in selection.stories})


async def test_the_editor_may_reorder_and_swap_in_an_alternate():
    selection, stories = _lineup()
    editor = EditorFake(
        {
            "order": ["https://x/3", "https://x/5", "https://x/1"],
            "good_news": "https://x/4",
            "note": "abro con la tercera",
        }
    )
    result = await editorial_pass(
        editor, selection, alternates=runners_up(stories, selection), day=DAY, settings=Settings()
    )
    assert [s.article.url for s in result.stories] == ["https://x/3", "https://x/5", "https://x/1"]
    assert result.good_news is not None and result.good_news.article.url == "https://x/4"
    assert editor.calls == 1


async def test_unknown_urls_are_ignored_and_a_non_positive_closer_is_refused():
    selection, _ = _lineup()
    editor = EditorFake(
        {"order": ["https://x/2", "https://invented/x"], "good_news": "https://x/2"}
    )
    result = await editorial_pass(editor, selection, alternates=[], day=DAY, settings=Settings())
    assert [s.article.url for s in result.stories] == ["https://x/2"]
    assert result.good_news is None  # it airs in the body, and it is not positive either


async def test_a_broken_or_empty_editorial_choice_falls_back_to_the_deterministic_lineup():
    selection, stories = _lineup()
    for broken in (
        LLMOutputError("no JSON"),
        {"order": [], "good_news": None},
        {"order": ["https://nothing/1"], "good_news": None},
    ):
        editor = EditorFake(broken)
        result = await editorial_pass(
            editor,
            selection,
            alternates=runners_up(stories, selection),
            day=DAY,
            settings=Settings(),
        )
        assert result == selection


@pytest.mark.parametrize("sentiment", [Sentiment.NEUTRAL, Sentiment.NEGATIVE])
async def test_the_closer_must_be_good_news(sentiment):
    selection, _ = _lineup()
    downer = story("Mala noticia", url="https://x/9", sentiment=sentiment, relevance=4)
    editor = EditorFake({"order": ["https://x/1", "https://x/2"], "good_news": "https://x/9"})
    result = await editorial_pass(
        editor, selection, alternates=[downer], day=DAY, settings=Settings()
    )
    assert result.good_news is None
