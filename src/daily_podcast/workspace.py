"""Per-episode working directory with JSON checkpoints, so runs can resume."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from daily_podcast.errors import CheckpointError

M = TypeVar("M", bound=BaseModel)


def atomic_write(path: Path, data: str | bytes) -> None:
    """Write through a temp file and rename, so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    if isinstance(data, str):
        temp.write_text(data, encoding="utf-8")
    else:
        temp.write_bytes(data)
    os.replace(temp, path)


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
        path = self.path(name)
        try:
            return model.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, OSError) as exc:
            raise CheckpointError(
                f"Cannot read the checkpoint {path}: delete it, or rerun with --fresh."
            ) from exc

    def discard(self, *names: str) -> list[str]:
        """Delete checkpoints; returns the names that existed."""
        removed = [name for name in names if self.has(name)]
        for name in removed:
            self.path(name).unlink()
        return removed

    def write_text(self, name: str, text: str) -> Path:
        target = self.path(name)
        atomic_write(target, text)
        return target

    def write_json(self, name: str, data: object) -> Path:
        return self.write_text(name, json.dumps(data, indent=2, ensure_ascii=False))
