"""``cli.load_environment``: the project's .env must win over a stale exported variable."""

from __future__ import annotations

import os

from daily_podcast.cli import load_environment


def test_the_env_file_overrides_an_exported_variable(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("AIXY_API_KEY=from-the-file\n", encoding="utf-8")
    monkeypatch.setenv("AIXY_API_KEY", "from-the-shell")

    assert load_environment(env) == str(env)
    assert os.environ["AIXY_API_KEY"] == "from-the-file"


def test_a_missing_env_file_leaves_the_environment_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("AIXY_API_KEY", "from-the-shell")

    assert load_environment(tmp_path / "nope.env") is None
    assert os.environ["AIXY_API_KEY"] == "from-the-shell"
