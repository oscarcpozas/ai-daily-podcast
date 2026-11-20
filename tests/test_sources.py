from datetime import date
from pathlib import Path

import pytest

from daily_podcast.config import ConfigError
from daily_podcast.sources import load_sources

DAY = date(2026, 11, 20)


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "sources.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_sources_and_renders_date_placeholders(tmp_path):
    path = write(
        tmp_path,
        """
[[source]]
name = "Diario"
url = "https://example.com/"
link_pattern = "^https://example\\\\.com/{today}/[a-z-]+$"
max_articles = 4

[[source]]
name = "Radio"
url = "https://radio.example.org/"
link_pattern = "/{today_compact}/\\\\d+\\\\.html$"
""",
    )
    diario, radio = load_sources(path)
    assert diario.max_articles == 4 and radio.max_articles == 8
    assert diario.pattern_for(DAY).search("https://example.com/2026-11-20/hola-mundo")
    assert radio.pattern_for(DAY).search("https://radio.example.org/x/20261120/12.html")
    assert not radio.pattern_for(DAY).search("https://radio.example.org/x/20261119/12.html")


def test_source_without_pattern_matches_anything(tmp_path):
    path = write(tmp_path, '[[source]]\nname = "A"\nurl = "https://a.example/"\n')
    assert load_sources(path)[0].pattern_for(DAY) is None


def test_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_sources(tmp_path / "nope.toml")


def test_duplicate_names_are_rejected(tmp_path):
    path = write(
        tmp_path,
        '[[source]]\nname = "A"\nurl = "https://a.example/"\n'
        '[[source]]\nname = "A"\nurl = "https://b.example/"\n',
    )
    with pytest.raises(ConfigError, match="Duplicate source names"):
        load_sources(path)


def test_invalid_url_is_rejected(tmp_path):
    path = write(tmp_path, '[[source]]\nname = "A"\nurl = "not a url"\n')
    with pytest.raises(ConfigError, match="Invalid source #1"):
        load_sources(path)
