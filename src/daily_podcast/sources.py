"""Load the list of trusted news sources."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import ValidationError

from daily_podcast.config import ConfigError
from daily_podcast.models import Source


def load_sources(path: Path) -> list[Source]:
    if not path.is_file():
        raise ConfigError(f"Sources file not found: {path}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc

    raw_sources = data.get("source", [])
    if not raw_sources:
        raise ConfigError(f"{path} declares no [[source]] entries")

    sources: list[Source] = []
    for index, raw in enumerate(raw_sources, start=1):
        try:
            sources.append(Source.model_validate(raw))
        except ValidationError as exc:
            raise ConfigError(f"Invalid source #{index} in {path}:\n{exc}") from exc

    names = [source.name for source in sources]
    duplicates = {name for name in names if names.count(name) > 1}
    if duplicates:
        raise ConfigError(f"Duplicate source names in {path}: {', '.join(sorted(duplicates))}")
    return sources
