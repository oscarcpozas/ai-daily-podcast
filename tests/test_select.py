from daily_podcast.models import Article, Category, Classification, Sentiment, Story, StorySet
from daily_podcast.pipeline.select import RUNNING_ORDER, select


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
