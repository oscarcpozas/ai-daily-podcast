"""AixyLLM surface errors: the failures that used to surface as empty JSON deep in the pipeline."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from daily_podcast.aixy.llm import AixyInferenceError, AixyLLM
from daily_podcast.config import Settings


def _client(message: SimpleNamespace, model: str = "deepseek-v4-pro-0813") -> SimpleNamespace:
    response = SimpleNamespace(
        model=model,
        usage=None,
        choices=[SimpleNamespace(message=message, finish_reason=message.finish_reason)],
    )

    class Completions:
        async def create(self, **_: object) -> SimpleNamespace:
            return response

    return SimpleNamespace(chat=SimpleNamespace(completions=Completions()))


def _llm(client: SimpleNamespace) -> AixyLLM:
    settings = Settings.from_env()
    return AixyLLM(settings, session_id="test", client=client)  # type: ignore[arg-type]


def test_empty_content_from_a_reasoning_target_names_the_cause():
    message = SimpleNamespace(content="", reasoning_content="pensando…", finish_reason="length")
    with pytest.raises(AixyInferenceError, match="reasoning and returned no content"):
        asyncio.run(_llm(_client(message)).chat([{"role": "user", "content": "hola"}]))


def test_empty_content_with_tool_calls_is_a_normal_step():
    call = SimpleNamespace(
        id="1", type="function", function=SimpleNamespace(name="x", arguments="{}")
    )
    message = SimpleNamespace(content="", tool_calls=[call], finish_reason="tool_calls")
    result = asyncio.run(_llm(_client(message)).chat([{"role": "user", "content": "hola"}]))
    assert result.tool_calls[0].function.name == "x"
