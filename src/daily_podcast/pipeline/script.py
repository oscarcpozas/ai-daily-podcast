"""Stage 4 — script: write the episode for ElevenLabs, with audio tags."""

from __future__ import annotations

import json
from datetime import date

from daily_podcast.aixy import AixyLLM
from daily_podcast.config import Settings
from daily_podcast.models import Script, ScriptSegment, Selection, Story
from daily_podcast.prompts import render
from daily_podcast.textutil import sanitize_for_tts, spanish_date

WORDS_PER_STORY = 110
WORDS_OVERHEAD = 160


def _story_payload(story: Story) -> dict[str, object]:
    return {
        "category": story.classification.category.label_es,
        "sentiment": story.classification.sentiment.value,
        "source": story.article.source,
        "url": story.article.url,
        "title": story.article.title,
        "summary": story.article.summary,
        "key_facts": story.article.key_facts,
    }


def build_brief(selection: Selection, day: date) -> str:
    brief: dict[str, object] = {
        "date": spanish_date(day),
        "stories": [_story_payload(story) for story in selection.stories],
    }
    if selection.good_news:
        brief["good_news"] = _story_payload(selection.good_news)
    return json.dumps(brief, ensure_ascii=False, indent=2)


def clean_script(script: Script) -> Script:
    """Strip anything ElevenLabs v3 would read aloud instead of performing (SSML, Markdown)."""
    segments = [
        ScriptSegment(kind=s.kind, text=sanitize_for_tts(s.text), story_url=s.story_url)
        for s in script.segments
        if s.text.strip()
    ]
    return Script(title=script.title.strip(), segments=segments)


async def write_script(
    llm: AixyLLM, selection: Selection, *, day: date, settings: Settings
) -> Script:
    story_count = len(selection.stories) + (1 if selection.good_news else 0)
    system = render(
        "script",
        show_name=settings.show_name,
        tts_model=settings.tts_model,
        today=spanish_date(day),
        segment_chars=min(settings.tts_chunk_chars, 1500),
        target_words=WORDS_OVERHEAD + WORDS_PER_STORY * story_count,
    )
    script = await llm.complete_json(
        system=system, user=build_brief(selection, day), schema=Script, max_tokens=8000
    )
    return clean_script(script)
