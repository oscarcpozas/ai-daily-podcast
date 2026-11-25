"""The episode run: wires every stage, checkpoints each one and reports the result."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from daily_podcast import __version__
from daily_podcast.aixy import AixyLLM, AixyTools
from daily_podcast.config import Settings
from daily_podcast.errors import CheckpointError, PodcastError
from daily_podcast.models import ArticleSet, AudioManifest, Script, Selection, StorySet
from daily_podcast.pipeline.classify import classify
from daily_podcast.pipeline.gather import gather
from daily_podcast.pipeline.publish import master_episode, show_notes
from daily_podcast.pipeline.script import write_script
from daily_podcast.pipeline.select import editorial_pass, runners_up, select
from daily_podcast.pipeline.voice import Voice, choose_voice, reset_audio, synthesize
from daily_podcast.sources import load_sources
from daily_podcast.workspace import Workspace

log = logging.getLogger(__name__)

VOICE = "audio/voice.json"


class Stage(StrEnum):
    GATHER = "gather"
    CLASSIFY = "classify"
    SELECT = "select"
    SCRIPT = "script"
    VOICE = "voice"
    PUBLISH = "publish"


STAGES = tuple(Stage)


class EmptyEpisodeError(PodcastError):
    """There is nothing to put on air."""


@dataclass
class RunOptions:
    day: date
    until: Stage = Stage.PUBLISH
    fresh: bool = False
    editorial: bool = False  # an agent reviews the deterministic lineup (one extra model call)
    max_stories: int = 10
    per_category: int = 3
    min_relevance: int = 2
    intro: Path | None = None
    sources_filter: list[str] = field(default_factory=list)
    regenerate_audio: bool = False  # forget generated speech: spends ElevenLabs credits


@dataclass
class RunReport:
    session_id: str
    day: date
    started_at: datetime
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)

    def as_dict(self, llm: AixyLLM | None) -> dict[str, Any]:
        return {
            "version": __version__,
            "session_id": self.session_id,
            "episode": self.day.isoformat(),
            "started_at": self.started_at.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "model": llm.model if llm else None,
            "usage": llm.usage.as_dict() if llm else None,
            "stages": self.stages,
            "outputs": self.outputs,
        }


Notify = Callable[[Stage, str], None]


class EpisodeRun:
    def __init__(
        self,
        settings: Settings,
        options: RunOptions,
        *,
        notify: Notify | None = None,
        llm: AixyLLM | None = None,
        tools: AixyTools | None = None,
    ) -> None:
        self.settings = settings
        self.options = options
        self.notify = notify or (lambda stage, message: None)
        self.workspace = Workspace(settings.output_dir, options.day)
        self.report = RunReport(
            session_id=f"podcast-{options.day.isoformat()}-{uuid.uuid4().hex[:8]}",
            day=options.day,
            started_at=datetime.now(UTC),
        )
        self._llm = llm
        self._tools = tools
        self._stack: AsyncExitStack | None = None

    @property
    def stages(self) -> list[Stage]:
        return list(STAGES[: STAGES.index(self.options.until) + 1])

    def _cached(self, stage: Stage, checkpoint: str) -> bool:
        if self.options.fresh or not self.workspace.has(checkpoint):
            return False
        self.report.stages[stage] = {"status": "cached"}
        self.notify(stage, f"reusing {checkpoint}")
        return True

    @property
    def llm(self) -> AixyLLM:
        if self._llm is None:
            self._llm = AixyLLM(self.settings, session_id=self.report.session_id)
        return self._llm

    async def run(self) -> RunReport:
        try:
            async with AsyncExitStack() as stack:
                self._stack = stack
                await self._run_stages()
        finally:
            self.workspace.write_json(Workspace.REPORT, self.report.as_dict(self._llm))
        return self.report

    async def _tools_session(self) -> AixyTools:
        """The Aixy MCP session, opened the first time a stage needs it.

        Opening it lazily means a run whose tool stages are all checkpointed (or stopped
        before them with ``--until``) never connects, and a stage that loses its checkpoint
        midway, as ``select`` can cause, still finds a session.
        """
        if self._tools is None:
            assert self._stack is not None
            self._tools = await self._stack.enter_async_context(AixyTools(self.settings))
        return self._tools

    async def _timed(self, stage: Stage, step: Callable[[], Any]) -> None:
        started = time.monotonic()
        detail = await step()
        if self.report.stages.get(stage, {}).get("status") == "cached":
            return  # _cached recorded it: do not overwrite it with "done"
        self.report.stages[stage] = {
            "status": "done",
            "seconds": round(time.monotonic() - started, 1),
            **(detail or {}),
        }

    async def _run_stages(self) -> None:
        steps = {
            Stage.GATHER: self._gather,
            Stage.CLASSIFY: self._classify,
            Stage.SELECT: self._select,
            Stage.SCRIPT: self._script,
            Stage.VOICE: self._voice,
            Stage.PUBLISH: self._publish,
        }
        for stage in self.stages:
            await self._timed(stage, steps[stage])

    async def _gather(self) -> dict[str, Any] | None:
        if self._cached(Stage.GATHER, Workspace.ARTICLES):
            return None
        sources = load_sources(self.settings.sources_file)
        if self.options.sources_filter:
            wanted = {name.lower() for name in self.options.sources_filter}
            sources = [s for s in sources if s.name.lower() in wanted]
        self.notify(Stage.GATHER, f"reading {len(sources)} sources through Aixy")
        tools = await self._tools_session()
        articles = await gather(
            self.llm,
            tools,
            sources,
            day=self.options.day,
            max_chars=self.settings.tool_result_max_chars,
            concurrency=self.settings.concurrency,
            web_provider=self.settings.web_provider,
        )
        if not articles.articles:
            raise EmptyEpisodeError("No articles were gathered from any source")
        self.workspace.save(Workspace.ARTICLES, articles)
        return {"articles": len(articles.articles), "sources": len(sources)}

    async def _classify(self) -> dict[str, Any] | None:
        if self._cached(Stage.CLASSIFY, Workspace.STORIES):
            return None
        articles = self.workspace.load(Workspace.ARTICLES, ArticleSet)
        self.notify(Stage.CLASSIFY, f"classifying {len(articles.articles)} articles")
        stories = await classify(self.llm, articles, concurrency=self.settings.concurrency)
        if not stories.stories:
            raise EmptyEpisodeError("No article could be classified")
        self.workspace.save(Workspace.STORIES, stories)
        return {"stories": len(stories.stories)}

    async def _select(self) -> dict[str, Any] | None:
        # Selection is deterministic and instant: always recompute it so CLI options apply.
        stories = self.workspace.load(Workspace.STORIES, StorySet)
        selection = select(
            stories,
            max_stories=self.options.max_stories,
            per_category=self.options.per_category,
            min_relevance=self.options.min_relevance,
        )
        if not selection.stories:
            raise EmptyEpisodeError("No story passed the editorial selection")
        if self.options.editorial:
            self.notify(Stage.SELECT, "an editorial agent is reviewing the lineup")
            selection = await editorial_pass(
                self.llm,
                selection,
                alternates=runners_up(stories, selection),
                day=self.options.day,
                settings=self.settings,
            )
        if self._selection_changed(selection):
            # Script and audio were written for another lineup: drop them so they are redone.
            self.workspace.discard(
                Workspace.SCRIPT, Workspace.SCRIPT_TEXT, Workspace.AUDIO_MANIFEST
            )
            self.notify(Stage.SELECT, "the lineup changed: the script and voice will be redone")
        self.workspace.save(Workspace.SELECTION, selection)
        self.notify(Stage.SELECT, f"{len(selection.stories)} stories on air")
        return {"selected": len(selection.stories), "good_news": selection.good_news is not None}

    def _selection_changed(self, selection: Selection) -> bool:
        if not self.workspace.has(Workspace.SELECTION):
            return False
        try:
            previous = self.workspace.load(Workspace.SELECTION, Selection)
        except CheckpointError:
            return True
        return previous != selection

    async def _script(self) -> dict[str, Any] | None:
        if self._cached(Stage.SCRIPT, Workspace.SCRIPT):
            return None
        selection = self.workspace.load(Workspace.SELECTION, Selection)
        self.notify(Stage.SCRIPT, "writing the script with ElevenLabs audio tags")
        script = await write_script(
            self.llm, selection, day=self.options.day, settings=self.settings
        )
        self.workspace.save(Workspace.SCRIPT, script)
        self.workspace.write_text(Workspace.SCRIPT_TEXT, script.as_text() + "\n")
        self.report.outputs["script"] = str(self.workspace.path(Workspace.SCRIPT_TEXT))
        return {"segments": len(script.segments), "characters": len(script.as_text())}

    async def _voice(self) -> dict[str, Any] | None:
        if not self.options.regenerate_audio and self._cached(
            Stage.VOICE, Workspace.AUDIO_MANIFEST
        ):
            return None
        tools = await self._tools_session()
        script = self.workspace.load(Workspace.SCRIPT, Script)
        if self.options.regenerate_audio:
            removed = reset_audio(self.workspace.audio_dir)
            self.notify(Stage.VOICE, f"discarded {removed} saved audio files; generating again")
        if self.workspace.has(VOICE) and not self.options.fresh:
            voice = self.workspace.load(VOICE, Voice)
        else:
            self.notify(Stage.VOICE, "choosing an ElevenLabs voice via Aixy")
            voice = await choose_voice(self.llm, tools, self.settings)
            self.workspace.save(VOICE, voice)
        self.notify(Stage.VOICE, f"generating speech with {self.settings.tts_model}")
        manifest = await synthesize(
            tools,
            script,
            voice,
            audio_dir=self.workspace.audio_dir,
            settings=self.settings,
        )
        self.workspace.save(Workspace.AUDIO_MANIFEST, manifest)
        return {"chunks": len(manifest.chunks), "voice": voice.name or voice.voice_id}

    async def _publish(self) -> dict[str, Any] | None:
        manifest = self.workspace.load(Workspace.AUDIO_MANIFEST, AudioManifest)
        script = self.workspace.load(Workspace.SCRIPT, Script)
        selection = self.workspace.load(Workspace.SELECTION, Selection)
        episode = master_episode(
            manifest,
            audio_dir=self.workspace.audio_dir,
            target=self.workspace.path(Workspace.EPISODE),
            intro=self.options.intro,
        )
        notes = self.workspace.write_text(
            Workspace.SHOW_NOTES,
            show_notes(script, selection, day=self.options.day, show_name=self.settings.show_name),
        )
        self.report.outputs.update({"episode": str(episode), "show_notes": str(notes)})
        self.notify(Stage.PUBLISH, f"episode ready: {episode}")
        return None
