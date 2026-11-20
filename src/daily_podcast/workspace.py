"""Per-episode working directory with JSON checkpoints, so runs can resume."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

M = TypeVar("M", bound=BaseModel)


class Workspace:
    ARTICLES = "articles.json"
    STORIES = "stories.json"
    SELECTION = "selection.json"
    SCRIPT = "script.json"
    SCRIPT_TEXT = "script.txt"
    AUDIO_MANIFEST = "audio/manifest.json"
    EPISODE = "episode.mp3"
    SHOW_NOTES = "episode.md"
    REPORT = "run.json"

    def __init__(self, root: Path, day: date) -> None:
        self.day = day
        self.dir = root / day.isoformat()

    def path(self, name: str) -> Path:
        return self.dir / name

    @property
    def audio_dir(self) -> Path:
        return self.dir / "audio"

    def has(self, name: str) -> bool:
        return self.path(name).is_file()

    def save(self, name: str, model: BaseModel) -> Path:
        return self.write_text(name, model.model_dump_json(indent=2))

    def load(self, name: str, model: type[M]) -> M:
        return model.model_validate_json(self.path(name).read_text(encoding="utf-8"))

    def write_text(self, name: str, text: str) -> Path:
        target = self.path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def write_json(self, name: str, data: object) -> Path:
        return self.write_text(name, json.dumps(data, indent=2, ensure_ascii=False))
