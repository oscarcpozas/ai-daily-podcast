"""Stage 6 — publish: master the episode and write the show notes.

Upload to Google Drive will plug in here once the Drive connector is enabled in Aixy.
"""

from __future__ import annotations

import logging
import shutil
from datetime import date
from pathlib import Path

from daily_podcast.errors import PodcastError
from daily_podcast.models import AudioManifest, Script, Selection, Story
from daily_podcast.textutil import spanish_date

log = logging.getLogger(__name__)

INTRO_SECONDS = 4.1
GAP_MS = 350


class MasteringError(PodcastError):
    """The episode audio could not be assembled."""


def master_episode(
    manifest: AudioManifest, *, audio_dir: Path, target: Path, intro: Path | None
) -> Path:
    if shutil.which("ffmpeg") is None:
        raise MasteringError("ffmpeg is required to assemble the episode (brew install ffmpeg)")
    from pydub import AudioSegment  # imported lazily: pydub warns at import without ffmpeg

    episode = AudioSegment.empty()
    if intro is not None and intro.is_file():
        episode += AudioSegment.from_file(intro)[: int(INTRO_SECONDS * 1000)]
    elif intro is not None:
        log.warning("Intro jingle %s not found: publishing the episode without it", intro)
    gap = AudioSegment.silent(duration=GAP_MS)
    for chunk in sorted(manifest.chunks, key=lambda c: c.index):
        episode += AudioSegment.from_file(audio_dir / chunk.file) + gap
    episode.export(target, format="mp3", bitrate="192k", tags={"title": target.stem})
    return target


def _story_line(story: Story) -> str:
    article, category = story.article, story.classification.category
    return f"- [{article.title}]({article.url}) · {article.source} · {category.label_es}"


def show_notes(script: Script, selection: Selection, *, day: date, show_name: str) -> str:
    lines = [
        f"# {script.title}",
        "",
        f"**{show_name}** · {spanish_date(day)}",
        "",
        "## Noticias",
        "",
        *(_story_line(story) for story in selection.stories),
    ]
    if selection.good_news:
        lines += ["", "## La buena noticia", "", _story_line(selection.good_news)]
    lines += [
        "",
        "---",
        "",
        "Episodio producido de principio a fin por un agente de IA. Toda la inteligencia "
        "(modelos, scraping y voz) la provee [Aixy](https://aixy-gateway.com).",
        "",
    ]
    return "\n".join(lines)
