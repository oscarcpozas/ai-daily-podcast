"""Inference through the Aixy gateway (OpenAI-compatible Chat Completions).

The code never names a concrete model. It asks for a routing model (``aixy/podcast-agent`` by
default) and Aixy decides which provider and model serve each request. The resolved model is
recorded so every run reports what Aixy actually chose.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, TypeVar, cast

import openai
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, ValidationError

from daily_podcast.config import Settings
from daily_podcast.textutil import extract_json

M = TypeVar("M", bound=BaseModel)

SESSION_HEADER = "X-Aixy-Session-ID"


class AixyInferenceError(RuntimeError):
    """The gateway rejected or failed a model request."""


class LLMOutputError(RuntimeError):
    """The model kept replying with output that does not match the expected schema."""


@dataclass
class UsageStats:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    models: Counter[str] = field(default_factory=Counter)

    def record(self, response: Any) -> None:
        self.calls += 1
        if getattr(response, "model", None):
            self.models[response.model] += 1
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.prompt_tokens += usage.prompt_tokens or 0
            self.completion_tokens += usage.completion_tokens or 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "resolved_models": dict(self.models.most_common()),
        }


class AixyLLM:
    def __init__(
        self,
        settings: Settings,
        *,
        session_id: str,
        client: AsyncOpenAI | None = None,
    ) -> None:
        if client is None:
            settings.require_inference()
            client = AsyncOpenAI(
                api_key=settings.aixy_api_key,
                base_url=settings.aixy_base_url,
                # Groups every request of a run under one session in Aixy → Activity → Sessions.
                default_headers={SESSION_HEADER: session_id},
                max_retries=3,
                timeout=300,
            )
        self.model = settings.model
        self.session_id = session_id
        self.usage = UsageStats()
        self._client = client

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        """Send one Chat Completions request and return the assistant message."""
        kwargs: dict[str, Any] = {}
        if tools:
            kwargs["tools"] = tools
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        try:
            # Plain dicts keep the agent loop provider-agnostic (e.g. reasoning_content echoes).
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=cast("list[ChatCompletionMessageParam]", messages),
                **kwargs,
            )
        except openai.NotFoundError as exc:
            raise AixyInferenceError(
                f"Aixy could not resolve model {self.model!r}: {exc.message}. "
                "Make sure the routing model exists in the project that owns AIXY_API_KEY."
            ) from exc
        except openai.APIStatusError as exc:
            raise AixyInferenceError(
                f"Aixy returned HTTP {exc.status_code} for {self.model!r}: {exc.message}"
            ) from exc
        except openai.APIConnectionError as exc:
            raise AixyInferenceError(f"Could not reach the Aixy gateway: {exc}") from exc

        self.usage.record(response)
        if not response.choices:
            raise AixyInferenceError(f"{self.model!r} returned no choices")
        return response.choices[0].message

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: type[M],
        attempts: int = 2,
        max_tokens: int | None = None,
    ) -> M:
        """Ask for a JSON reply and validate it, feeding validation errors back once."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        last_error: Exception | None = None
        for _ in range(attempts):
            message = await self.chat(messages, max_tokens=max_tokens)
            text = message.content or ""
            try:
                return schema.model_validate(extract_json(text))
            except (ValueError, ValidationError) as exc:
                last_error = exc
                messages += [
                    {"role": "assistant", "content": text},
                    {
                        "role": "user",
                        "content": (
                            "Your reply could not be parsed or did not match the required "
                            f"schema:\n{exc}\n\nReply again with only the corrected JSON."
                        ),
                    },
                ]
        raise LLMOutputError(
            f"Model output did not match {schema.__name__} after {attempts} attempts: {last_error}"
        )
