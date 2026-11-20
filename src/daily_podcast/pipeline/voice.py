"""Stage 5 — voice: ElevenLabs text-to-speech through Aixy MCP.

ElevenLabs' creative API is asynchronous: ``creative_generate_speech`` starts a run on a flow
and ``creative_get_flow_run_status`` reports when its audio is ready. Generation spends credits
and must never be retried blindly, so every started job is checkpointed before polling: an
interrupted run resumes by polling the same job instead of paying for a new one.

The only model call in this stage is choosing a voice (once, then cached), done by a small agent
that may browse ElevenLabs' voice library but is not allowed to generate anything.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx2
from pydantic import BaseModel, Field

from daily_podcast.aixy import (
    AixyLLM,
    AixyToolError,
    AixyTools,
    LocalTool,
    ToolInputError,
    ToolOutput,
    run_agent,
)
from daily_podcast.aixy.agent import gateway_tools
from daily_podcast.config import Settings
from daily_podcast.errors import CheckpointError, PodcastError
from daily_podcast.models import AudioChunk, AudioManifest, Script
from daily_podcast.prompts import render
from daily_podcast.textutil import pack_chunks
from daily_podcast.workspace import atomic_write

log = logging.getLogger(__name__)

PROVIDER = "elevenlabs"
GENERATE_SPEECH = "elevenlabs.creative_generate_speech"
RUN_STATUS = "elevenlabs.creative_get_flow_run_status"
LIST_VOICES = "elevenlabs.creative_list_voices"

AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".ogg", ".opus", ".flac")
POLL_TIMEOUT_SECONDS = 15 * 60
DEFAULT_POLL_SECONDS = 5.0
MIN_POLL_SECONDS = 1.0
MAX_POLL_SECONDS = 60.0
MAX_POLL_ERRORS = 3  # consecutive failed status reads before giving up; the job is already paid
MAX_AUDIO_BYTES = 200 * 1024 * 1024


class VoiceError(PodcastError):
    """Text-to-speech could not produce audio."""


class SpeechFailedError(VoiceError):
    """ElevenLabs reported the generation as failed; its job must not be polled again."""


class Voice(BaseModel):
    voice_id: str
    name: str = ""
    reason: str = ""


class SpeechJob(BaseModel):
    """A started generation. Persisted so a resumed run polls it instead of paying again."""

    index: int
    fingerprint: str  # text + voice + model: a job only serves the exact audio it was paid for
    flow_id: str
    session_ids: list[str] = Field(min_length=1)


def fingerprint(text: str, *, voice_id: str, model_id: str) -> str:
    return hashlib.sha256(f"{model_id}\0{voice_id}\0{text}".encode()).hexdigest()[:32]


CHOOSE_VOICE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "voice_id": {"type": "string", "description": "voice_id exactly as listed by ElevenLabs."},
        "name": {"type": "string"},
        "reason": {"type": "string", "description": "One sentence on why it fits."},
    },
    "required": ["voice_id", "name"],
}


# --- choosing a voice -------------------------------------------------------------------------


async def choose_voice(llm: AixyLLM, tools: AixyTools, settings: Settings) -> Voice:
    if settings.voice_id:
        return Voice(voice_id=settings.voice_id, reason="ELEVENLABS_VOICE_ID")

    async def submit(arguments: dict[str, Any]) -> Voice:
        try:
            return Voice.model_validate(arguments)
        except ValueError as exc:
            raise ToolInputError(str(exc)) from exc

    return await run_agent(
        llm,
        system=render("voice", show_name=settings.show_name),
        user="Choose the host voice for today's episode.",
        tools=[
            *gateway_tools(
                tools,
                provider=PROVIDER,
                max_chars=settings.tool_result_max_chars,
                allowed_actions={LIST_VOICES},
            ),
            LocalTool(
                "choose_voice",
                "Report the chosen voice. Call it exactly once, at the end.",
                CHOOSE_VOICE,
                submit,
                terminal=True,
            ),
        ],
        max_steps=8,
    )


# --- reading ElevenLabs responses -------------------------------------------------------------


def _payload(output: ToolOutput) -> Any:
    if output.structured is not None:
        return output.structured
    try:
        return json.loads(output.text)
    except json.JSONDecodeError:
        return None


def find_key(data: Any, key: str) -> Any:
    """First value stored under ``key`` anywhere in a JSON document."""
    if isinstance(data, dict):
        if key in data:
            return data[key]
        values = list(data.values())
    elif isinstance(data, list):
        values = data
    else:
        return None
    for value in values:
        found = find_key(value, key)
        if found is not None:
            return found
    return None


def parse_job(output: ToolOutput, *, index: int, fingerprint: str) -> SpeechJob:
    if output.is_error:
        raise VoiceError(f"ElevenLabs refused the generation: {output.text[:500]}")
    data = _payload(output)
    flow_id = find_key(data, "flow_id")
    session_ids = find_key(data, "session_ids")
    if isinstance(session_ids, str):
        session_ids = [session_ids]
    if not flow_id or not session_ids:
        raise VoiceError(f"Unexpected ElevenLabs reply, no flow/session ids: {output.text[:500]}")
    return SpeechJob(
        index=index, fingerprint=fingerprint, flow_id=str(flow_id), session_ids=session_ids
    )


def _looks_like_audio(url: str) -> bool:
    path = url.split("?", 1)[0].lower()
    return path.endswith(AUDIO_EXTENSIONS) or "audio" in path


async def _download_audio(http: httpx2.AsyncClient, urls: list[str]) -> bytes | None:
    for url in sorted(urls, key=lambda u: not _looks_like_audio(u)):
        if not url.startswith("https://"):
            continue
        try:
            response = await http.get(url)
        except httpx2.HTTPError:
            continue
        content_type = response.headers.get("content-type", "")
        if (
            response.status_code == 200
            and (content_type.startswith("audio/") or content_type == "application/octet-stream")
            and 0 < len(response.content) <= MAX_AUDIO_BYTES
        ):
            return response.content
    return None


async def audio_from_output(http: httpx2.AsyncClient, output: ToolOutput) -> bytes:
    """Audio may come inline or as a short-lived download link inside the run status."""
    for mime, data in output.blobs:
        if mime.startswith("audio/") or mime == "application/octet-stream":
            return data
    audio = await _download_audio(http, output.urls())
    if audio is None:
        raise VoiceError(f"The finished run has no downloadable audio: {output.text[:500]}")
    return audio


# --- running jobs -----------------------------------------------------------------------------


async def wait_for_audio(tools: AixyTools, http: httpx2.AsyncClient, job: SpeechJob) -> bytes:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    errors = 0
    while True:
        try:
            output = await tools.execute(
                RUN_STATUS, {"flow_id": job.flow_id, "session_ids": job.session_ids}
            )
        except AixyToolError as exc:
            # Reading a status is free and the job is already paid: ride out a hiccup.
            errors += 1
            if errors >= MAX_POLL_ERRORS:
                raise VoiceError(f"Could not read the status of chunk {job.index}: {exc}") from exc
            log.warning("Status of chunk %d unavailable (%s); retrying", job.index, exc)
            await asyncio.sleep(DEFAULT_POLL_SECONDS)
            continue
        if output.is_error:
            raise VoiceError(f"Could not read the run status: {output.text[:500]}")
        errors = 0
        data = _payload(output)
        if find_key(data, "has_failures") is True:
            raise SpeechFailedError(f"ElevenLabs failed chunk {job.index}: {output.text[:500]}")
        if find_key(data, "all_completed") is True:
            return await audio_from_output(http, output)
        if time.monotonic() > deadline:
            raise VoiceError(f"Chunk {job.index} was not ready after {POLL_TIMEOUT_SECONDS}s")
        wait = find_key(data, "poll_after_seconds")
        seconds = float(wait) if isinstance(wait, int | float) else DEFAULT_POLL_SECONDS
        await asyncio.sleep(min(max(seconds, MIN_POLL_SECONDS), MAX_POLL_SECONDS))


def chunk_script(script: Script, max_chars: int) -> list[str]:
    return pack_chunks((segment.text for segment in script.segments), max_chars)


def load_jobs(path: Path) -> dict[int, SpeechJob]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {job.index: job for job in (SpeechJob.model_validate(item) for item in raw)}
    except (ValueError, TypeError, OSError) as exc:  # ValidationError is a ValueError
        raise CheckpointError(
            f"{path} is unreadable. It holds the ids of audio already paid for: inspect it "
            "before deleting it."
        ) from exc


def save_jobs(path: Path, jobs: dict[int, SpeechJob]) -> None:
    data = [job.model_dump() for job in sorted(jobs.values(), key=lambda j: j.index)]
    atomic_write(path, json.dumps(data, indent=2))


def _has_audio(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def reset_audio(audio_dir: Path) -> int:
    """Forget generated audio and saved jobs so the next run generates it again (spends credits)."""
    removed = 0
    leftovers = [*audio_dir.glob("part-*.mp3"), *audio_dir.glob("reply-*.json")]
    for leftover in [*leftovers, audio_dir / "jobs.json"]:
        if leftover.is_file():
            leftover.unlink()
            removed += 1
    return removed


def _part_name(index: int, print_: str) -> str:
    return f"part-{index:03d}-{print_[:8]}.mp3"


async def synthesize(
    tools: AixyTools,
    script: Script,
    voice: Voice,
    *,
    audio_dir: Path,
    settings: Settings,
) -> AudioManifest:
    # Aixy ties execution to actions selected by discovery in the same MCP session.
    for action in (GENERATE_SPEECH, RUN_STATUS):
        await tools.describe(action)

    jobs_file = audio_dir / "jobs.json"
    jobs = load_jobs(jobs_file)
    texts = chunk_script(script, settings.tts_chunk_chars)
    flow_id = next((job.flow_id for job in jobs.values()), None)
    chunks: list[AudioChunk] = []

    async with httpx2.AsyncClient(follow_redirects=True, timeout=120) as http:
        for index, text in enumerate(texts, start=1):
            print_ = fingerprint(text, voice_id=voice.voice_id, model_id=settings.tts_model)
            # The file name carries the fingerprint: audio for other text or another voice is
            # never reused, and a stale part from an earlier script is simply ignored.
            target = audio_dir / _part_name(index, print_)
            if _has_audio(target):
                log.info("Reusing %s", target.name)
            else:
                job = jobs.get(index)
                if job is None or job.fingerprint != print_:
                    arguments: dict[str, Any] = {
                        "prompt": text,
                        "model_id": settings.tts_model,
                        "voice_id": voice.voice_id,
                        "generations_count": 1,
                    }
                    if flow_id:  # keep the whole episode on one ElevenLabs canvas
                        arguments["flow_id"] = flow_id
                    started = await tools.execute(GENERATE_SPEECH, arguments)
                    # Paid from here on: keep the raw reply in case its shape is not what
                    # parse_job expects, so the job ids are never lost.
                    atomic_write(audio_dir / f"reply-{index:03d}.json", started.text)
                    job = parse_job(started, index=index, fingerprint=print_)
                    flow_id = job.flow_id
                    jobs[index] = job
                    save_jobs(jobs_file, jobs)  # before polling: never pay twice
                    log.info("Started chunk %d/%d (%d characters)", index, len(texts), len(text))
                try:
                    audio = await wait_for_audio(tools, http, job)
                except SpeechFailedError:
                    # ElevenLabs gave up on this job: drop it so a rerun generates the chunk
                    # again instead of polling the dead job forever.
                    del jobs[index]
                    save_jobs(jobs_file, jobs)
                    raise
                atomic_write(target, audio)
                log.info("Saved %s", target.name)
            chunks.append(AudioChunk(index=index, file=target.name, characters=len(text)))

    return AudioManifest(
        action=GENERATE_SPEECH,
        voice_id=voice.voice_id,
        model_id=settings.tts_model,
        flow_id=flow_id,
        chunks=chunks,
    )
