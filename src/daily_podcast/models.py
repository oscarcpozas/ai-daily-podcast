"""Domain models shared by every pipeline stage."""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator


class Category(StrEnum):
    GENERAL = "GENERAL"
    POLITICS = "POLITICS"
    INTERNATIONAL = "INTERNATIONAL"
    ECONOMY = "ECONOMY"
    SCIENCE = "SCIENCE"
    TECHNOLOGY = "TECHNOLOGY"
    CULTURE = "CULTURE"
    ENTERTAINMENT = "ENTERTAINMENT"
    SPORTS = "SPORTS"
    WEATHER = "WEATHER"

    @property
    def label_es(self) -> str:
        return _CATEGORY_LABELS_ES[self]


_CATEGORY_LABELS_ES = {
    Category.GENERAL: "Actualidad",
    Category.POLITICS: "Política",
    Category.INTERNATIONAL: "Internacional",
    Category.ECONOMY: "Economía",
    Category.SCIENCE: "Ciencia",
    Category.TECHNOLOGY: "Tecnología",
    Category.CULTURE: "Cultura",
    Category.ENTERTAINMENT: "Entretenimiento",
    Category.SPORTS: "Deportes",
    Category.WEATHER: "El tiempo",
}

# Aliases models tend to produce, mapped to canonical categories.
_CATEGORY_ALIASES = {
    "POLITIC": Category.POLITICS,
    "POLITICA": Category.POLITICS,
    "WORLD": Category.INTERNATIONAL,
    "TECH": Category.TECHNOLOGY,
    "SPORT": Category.SPORTS,
    "BUSINESS": Category.ECONOMY,
    "HEALTH": Category.SCIENCE,
}


class Sentiment(StrEnum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


class Source(BaseModel):
    """A trusted news source, declared in ``sources.toml``."""

    name: str
    url: HttpUrl
    link_pattern: str | None = Field(
        default=None,
        description="Regex an article URL must match. Supports {today} and {today_compact}.",
    )
    max_articles: int = Field(default=8, ge=1, le=30)

    def pattern_for(self, day: date) -> re.Pattern[str] | None:
        if not self.link_pattern:
            return None
        rendered = self.link_pattern.replace("{today}", day.isoformat()).replace(
            "{today_compact}", day.strftime("%Y%m%d")
        )
        return re.compile(rendered)


class Classification(BaseModel):
    category: Category
    sentiment: Sentiment
    relevance: int = Field(ge=1, le=5)
    reason: str = ""

    @field_validator("category", mode="before")
    @classmethod
    def _normalize_category(cls, value: object) -> object:
        if isinstance(value, str):
            key = value.strip().upper()
            return _CATEGORY_ALIASES.get(key, key)
        return value

    @field_validator("sentiment", mode="before")
    @classmethod
    def _normalize_sentiment(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value


class Article(BaseModel):
    url: str
    source: str
    title: str
    summary: str
    key_facts: list[str] = Field(default_factory=list)
    classification: Classification | None = None
    """Set by the researcher while it reads; the classify stage only pays for the rest."""


class Story(BaseModel):
    article: Article
    classification: Classification


class ArticleSet(BaseModel):
    articles: list[Article] = Field(default_factory=list)


class StorySet(BaseModel):
    stories: list[Story] = Field(default_factory=list)


class Selection(BaseModel):
    stories: list[Story] = Field(default_factory=list)
    good_news: Story | None = None


class ScriptSegment(BaseModel):
    kind: Literal["intro", "story", "transition", "outro"]
    text: str
    story_url: str | None = None


class Script(BaseModel):
    title: str
    segments: list[ScriptSegment] = Field(min_length=1)

    def as_text(self) -> str:
        return "\n\n".join(segment.text for segment in self.segments)


class AudioChunk(BaseModel):
    index: int
    file: str
    characters: int


class AudioManifest(BaseModel):
    action: str
    voice_id: str | None = None
    model_id: str | None = None
    flow_id: str | None = None
    chunks: list[AudioChunk]
