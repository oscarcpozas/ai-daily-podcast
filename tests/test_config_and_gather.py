from __future__ import annotations

from datetime import date

import pytest

from daily_podcast.config import ConfigError, Settings
from daily_podcast.models import Source
from daily_podcast.pipeline.gather import _accept_articles, _same_site

DAY = date(2026, 11, 20)
RTVE = Source(
    name="RTVE",
    url="https://www.rtve.es/noticias/ultimas-noticias/",
    link_pattern=r"^https://www\.rtve\.es/noticias/{today_compact}/[a-z0-9-]+/\d+\.shtml$",
)
OPEN = Source(name="Open", url="https://example.com/")


def test_secrets_do_not_appear_in_the_repr():
    text = repr(Settings(aixy_api_key="sk-secret-value", aixy_mcp_token="tok-secret-value"))
    assert "secret" not in text


@pytest.mark.parametrize(
    ("name", "value"),
    [("TTS_CHUNK_CHARS", "0"), ("TTS_CHUNK_CHARS", "-5"), ("PODCAST_CONCURRENCY", "0")],
)
def test_non_positive_numbers_are_rejected(name, value):
    with pytest.raises(ConfigError, match=name):
        Settings.from_env({name: value})


def test_a_non_integer_is_rejected():
    with pytest.raises(ConfigError, match="integer"):
        Settings.from_env({"TTS_CHUNK_CHARS": "lots"})


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/a", True),
        ("http://www.example.com/a", True),
        ("https://news.example.com/a", True),
        ("https://evil.test/a", False),
        ("https://example.com.evil.test/a", False),
        ("javascript:alert(1)", False),
        ("ftp://example.com/a", False),
        ("not a url", False),
    ],
)
def test_only_links_on_the_sources_own_site_are_accepted(url, expected):
    assert _same_site(url, OPEN) is expected


def test_articles_off_site_or_off_pattern_are_rejected_even_without_a_pattern():
    good = "https://www.rtve.es/noticias/20261120/ley/123.shtml"
    raw = {
        "articles": [
            {"url": good, "title": "ok", "summary": "s"},
            {"url": "https://evil.test/20261120/x/1.shtml", "title": "bad", "summary": "s"},
            {
                "url": "https://www.rtve.es/noticias/20261119/old/9.shtml",
                "title": "old",
                "summary": "s",
            },
        ]
    }
    accepted, rejected = _accept_articles(RTVE, DAY, raw)
    assert [a.url for a in accepted] == [good]
    assert len(rejected) == 2

    accepted, rejected = _accept_articles(
        OPEN, DAY, {"articles": [{"url": "https://evil.test/x", "title": "t", "summary": "s"}]}
    )
    assert accepted == [] and len(rejected) == 1
