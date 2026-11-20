from __future__ import annotations

from datetime import date

import pytest

from daily_podcast.config import Settings
from daily_podcast.errors import CheckpointError
from daily_podcast.models import (
    Article,
    ArticleSet,
    Category,
    Classification,
    Script,
    ScriptSegment,
    Selection,
    Sentiment,
    Story,
    StorySet,
)
from daily_podcast.runner import EpisodeRun, RunOptions, Stage
from daily_podcast.workspace import Workspace

DAY = date(2026, 11, 20)


def story(title: str, relevance: int) -> Story:
    return Story(
        article=Article(
            url=f"https://example.com/{relevance}", source="T", title=title, summary="s"
        ),
        classification=Classification(
            category=Category.GENERAL, sentiment=Sentiment.NEUTRAL, relevance=relevance
        ),
    )


def seeded(tmp_path, **options) -> EpisodeRun:
    """A run whose gather and classify checkpoints exist: nothing needs a network."""
    settings = Settings(output_dir=tmp_path)
    run = EpisodeRun(settings, RunOptions(day=DAY, until=Stage.SELECT, **options))
    stories = StorySet(
        stories=[
            story("El Congreso debate la reforma electoral", 5),
            story("Moncloa anuncia un plan de vivienda", 4),
            story("Hacienda revisa los impuestos autonómicos", 3),
        ]
    )
    run.workspace.save(
        Workspace.ARTICLES, ArticleSet(articles=[s.article for s in stories.stories])
    )
    run.workspace.save(Workspace.STORIES, stories)
    return run


async def test_checkpointed_stages_are_reported_as_cached_not_done(tmp_path):
    report = await seeded(tmp_path).run()
    assert report.stages[Stage.GATHER] == {"status": "cached"}
    assert report.stages[Stage.CLASSIFY] == {"status": "cached"}
    assert report.stages[Stage.SELECT]["status"] == "done"


async def test_a_run_that_only_reads_checkpoints_never_opens_the_gateway(tmp_path):
    # No AIXY_MCP_TOKEN is configured: opening a tools session would raise ConfigError.
    run = seeded(tmp_path)
    await run.run()
    assert run._tools is None


async def test_changing_the_lineup_discards_the_stale_script_and_audio(tmp_path):
    first = seeded(tmp_path, max_stories=3)
    await first.run()
    ws = first.workspace
    ws.save(Workspace.SCRIPT, Script(title="t", segments=[ScriptSegment(kind="intro", text="x")]))
    ws.write_text(Workspace.SCRIPT_TEXT, "x")
    ws.write_text(Workspace.AUDIO_MANIFEST, "{}")

    again = seeded(tmp_path, max_stories=3)
    await again.run()
    assert ws.has(Workspace.SCRIPT) and ws.has(Workspace.AUDIO_MANIFEST)  # same lineup: kept

    narrower = EpisodeRun(
        Settings(output_dir=tmp_path), RunOptions(day=DAY, until=Stage.SELECT, max_stories=1)
    )
    await narrower.run()
    assert not ws.has(Workspace.SCRIPT)
    assert not ws.has(Workspace.SCRIPT_TEXT)
    assert not ws.has(Workspace.AUDIO_MANIFEST)
    assert len(ws.load(Workspace.SELECTION, Selection).stories) == 1


def test_a_corrupt_checkpoint_names_the_file_and_the_way_out(tmp_path):
    ws = Workspace(tmp_path, DAY)
    ws.write_text(Workspace.STORIES, "{broken")
    with pytest.raises(CheckpointError, match="--fresh"):
        ws.load(Workspace.STORIES, StorySet)


def test_checkpoints_are_written_atomically(tmp_path):
    ws = Workspace(tmp_path, DAY)
    ws.write_text(Workspace.SCRIPT_TEXT, "hola")
    assert [p.name for p in ws.dir.iterdir()] == [Workspace.SCRIPT_TEXT]
