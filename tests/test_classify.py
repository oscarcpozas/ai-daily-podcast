"""The researcher classifies while it reads; the classify stage only pays for the rest."""

from __future__ import annotations

from datetime import date

from daily_podcast.aixy import AixyInferenceError, LLMOutputError
from daily_podcast.models import Article, ArticleSet, Category, Classification, Sentiment
from daily_podcast.pipeline.classify import classify

DAY = date(2026, 11, 20)


class FakeLLM:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def complete_json(self, *, system, user, schema, attempts=2, max_tokens=None):
        self.calls += 1
        if self.fail:
            raise LLMOutputError("no valid JSON")
        return Classification(category=Category.POLITICS, sentiment=Sentiment.NEUTRAL, relevance=3)


def article(url: str, classification: Classification | None = None) -> Article:
    return Article(url=url, source="T", title="t", summary="s", classification=classification)


def pre_classified(url: str) -> Article:
    return article(
        url, Classification(category=Category.SCIENCE, sentiment=Sentiment.NEGATIVE, relevance=5)
    )


async def test_articles_the_researcher_classified_do_not_cost_a_call():
    articles = ArticleSet(
        articles=[
            pre_classified("https://a/1"),
            article("https://a/2"),
            pre_classified("https://a/3"),
        ]
    )
    llm = FakeLLM()
    stories = (await classify(llm, articles, concurrency=2)).stories

    assert llm.calls == 1  # only https://a/2
    assert [s.article.url for s in stories] == ["https://a/1", "https://a/2", "https://a/3"]
    assert stories[0].classification.category is Category.SCIENCE
    assert stories[1].classification.category is Category.POLITICS


async def test_nothing_left_to_classify_means_no_model_call_at_all():
    articles = ArticleSet(articles=[pre_classified(f"https://a/{i}") for i in range(3)])
    stories = (await classify(FakeLLM(fail=True), articles, concurrency=2)).stories
    assert len(stories) == 3  # the gateway failure never happened: nothing was asked


async def test_a_failed_classification_drops_only_that_article():
    articles = ArticleSet(articles=[article("https://a/1"), pre_classified("https://a/2")])
    stories = (await classify(FakeLLM(fail=True), articles, concurrency=2)).stories
    assert [s.article.url for s in stories] == ["https://a/2"]


async def test_an_inference_failure_is_not_fatal():
    class Unreachable(FakeLLM):
        async def complete_json(self, **kwargs):
            self.calls += 1
            raise AixyInferenceError("HTTP 503")

    stories = (
        await classify(Unreachable(), ArticleSet(articles=[article("https://a/1")]), concurrency=1)
    ).stories
    assert stories == []
