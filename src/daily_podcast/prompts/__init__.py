"""Prompt templates. Each ``*.md`` file is a ``string.Template`` (``$name`` placeholders)."""

from __future__ import annotations

from importlib.resources import files
from string import Template


def render(name: str, **values: object) -> str:
    template = files(__package__).joinpath(f"{name}.md").read_text(encoding="utf-8")
    return Template(template).substitute({key: str(value) for key, value in values.items()})
