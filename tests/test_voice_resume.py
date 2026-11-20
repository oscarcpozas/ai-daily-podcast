from __future__ import annotations

import pytest

from daily_podcast.config import Settings
from daily_podcast.models import Script, ScriptSegment
from daily_podcast.pipeline import voice
from tests.conftest import FakeTools

SETTINGS = Settings(tts_chunk_chars=400)
VOICE = voice.Voice(voice_id="V1", name="Lucía")


def script(text: str = "Hola, esto es el boletín de hoy.") -> Script:
    return Script(title="t", segments=[ScriptSegment(kind="intro", text=text)])


async def test_a_second_run_reuses_audio_and_pays_nothing(tmp_path):
    tools = FakeTools()
    first = await voice.synthesize(tools, script(), VOICE, audio_dir=tmp_path, settings=SETTINGS)
    second = await voice.synthesize(tools, script(), VOICE, audio_dir=tmp_path, settings=SETTINGS)
    assert tools.generations() == 1
    assert first.chunks == second.chunks


async def test_a_saved_job_is_polled_instead_of_generating_again(tmp_path):
    """Interrupted after the paid start: the rerun must poll the saved job, not pay again."""
    tools = FakeTools()
    text = script().segments[0].text
    print_ = voice.fingerprint(text, voice_id="V1", model_id=SETTINGS.tts_model)
    voice.save_jobs(
        tmp_path / "jobs.json",
        {1: voice.SpeechJob(index=1, fingerprint=print_, flow_id="OLD", session_ids=["SX"])},
    )
    await voice.synthesize(tools, script(), VOICE, audio_dir=tmp_path, settings=SETTINGS)
    assert tools.generations() == 0
    status_calls = [a for n, a in tools.calls if n == voice.RUN_STATUS]
    assert status_calls == [{"flow_id": "OLD", "session_ids": ["SX"]}]


async def test_changed_text_or_voice_is_never_served_stale_audio(tmp_path):
    tools = FakeTools()
    await voice.synthesize(
        tools, script("Primer texto."), VOICE, audio_dir=tmp_path, settings=SETTINGS
    )
    await voice.synthesize(
        tools, script("Otro texto."), VOICE, audio_dir=tmp_path, settings=SETTINGS
    )
    other = voice.Voice(voice_id="V2")
    await voice.synthesize(
        tools, script("Otro texto."), other, audio_dir=tmp_path, settings=SETTINGS
    )
    assert tools.generations() == 3


async def test_a_failed_job_is_dropped_so_the_next_run_regenerates(tmp_path):
    failing = FakeTools(fail_status=True)
    with pytest.raises(voice.SpeechFailedError):
        await voice.synthesize(failing, script(), VOICE, audio_dir=tmp_path, settings=SETTINGS)
    assert voice.load_jobs(tmp_path / "jobs.json") == {}

    healthy = FakeTools()
    await voice.synthesize(healthy, script(), VOICE, audio_dir=tmp_path, settings=SETTINGS)
    assert healthy.generations() == 1


async def test_reset_audio_removes_parts_and_jobs(tmp_path):
    tools = FakeTools()
    await voice.synthesize(tools, script(), VOICE, audio_dir=tmp_path, settings=SETTINGS)
    assert voice.reset_audio(tmp_path) == 3  # part, raw reply and jobs.json
    assert list(tmp_path.iterdir()) == []


async def test_the_raw_paid_reply_is_kept_even_if_it_cannot_be_parsed(tmp_path):
    class OddReply(FakeTools):
        async def execute(self, name, arguments):
            self.calls.append((name, arguments))
            return voice.ToolOutput(texts=['{"unexpected": "shape", "run": "R-42"}'])

    with pytest.raises(voice.VoiceError):
        await voice.synthesize(OddReply(), script(), VOICE, audio_dir=tmp_path, settings=SETTINGS)
    assert "R-42" in (tmp_path / "reply-001.json").read_text(encoding="utf-8")


def test_a_corrupt_jobs_file_is_reported_not_overwritten(tmp_path):
    (tmp_path / "jobs.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(voice.CheckpointError, match="already paid for"):
        voice.load_jobs(tmp_path / "jobs.json")
    assert (tmp_path / "jobs.json").read_text(encoding="utf-8") == "{not json"
