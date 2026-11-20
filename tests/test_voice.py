import pytest

from daily_podcast.aixy.mcp import ToolOutput
from daily_podcast.pipeline.voice import (
    SpeechJob,
    VoiceError,
    find_key,
    load_jobs,
    parse_job,
    save_jobs,
)


def test_find_key_searches_nested_documents():
    data = {"run": {"status": [{"all_completed": True}]}}
    assert find_key(data, "all_completed") is True
    assert find_key({"x": [{"flow_id": "f1"}]}, "flow_id") == "f1"
    assert find_key({"a": 1}, "missing") is None


def test_parse_job_reads_structured_reply():
    out = ToolOutput(
        texts=['{"flow_id": "F", "session_ids": ["S1"]}'],
        structured={"flow_id": "F", "session_ids": ["S1"]},
    )
    job = parse_job(out, index=2, characters=40)
    assert job == SpeechJob(index=2, characters=40, flow_id="F", session_ids=["S1"])


def test_parse_job_accepts_a_single_session_string():
    out = ToolOutput(texts=['{"flow_id": "F", "session_ids": "S1"}'])
    assert parse_job(out, index=1, characters=1).session_ids == ["S1"]


def test_parse_job_fails_loudly_on_error_or_missing_ids():
    with pytest.raises(VoiceError):
        parse_job(ToolOutput(texts=["denied"], is_error=True), index=1, characters=1)
    with pytest.raises(VoiceError):
        parse_job(ToolOutput(texts=['{"ok": true}']), index=1, characters=1)


def test_jobs_round_trip_through_the_checkpoint_file(tmp_path):
    path = tmp_path / "audio" / "jobs.json"
    job = SpeechJob(index=1, characters=10, flow_id="F", session_ids=["S1", "S2"])
    save_jobs(path, {1: job})
    assert load_jobs(path) == {1: job}
    assert load_jobs(tmp_path / "missing.json") == {}


def test_urls_are_extracted_without_duplicates():
    out = ToolOutput(
        links=["https://cdn.example/a.mp3"],
        texts=["listo: https://cdn.example/a.mp3 y https://cdn.example/b.mp3."],
    )
    assert out.urls() == ["https://cdn.example/a.mp3", "https://cdn.example/b.mp3"]
