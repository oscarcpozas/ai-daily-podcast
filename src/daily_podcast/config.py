"""Runtime configuration, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from daily_podcast.errors import PodcastError

DEFAULT_BASE_URL = "https://api.aixy-gateway.com/v1"
DEFAULT_MCP_URL = "https://api.aixy-gateway.com/mcp"
DEFAULT_MODEL = "aixy/podcast-agent"


class ConfigError(PodcastError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    aixy_api_key: str | None = field(default=None, repr=False)
    aixy_mcp_token: str | None = field(default=None, repr=False)
    aixy_base_url: str = DEFAULT_BASE_URL
    aixy_mcp_url: str = DEFAULT_MCP_URL
    model: str = DEFAULT_MODEL
    web_provider: str | None = None
    """Provider used to read the web. Empty: whatever Aixy discovers (Exa, TinyFish…)."""
    tts_provider: str = "elevenlabs"
    """Connector that serves speech. A deployment choice, not a code constant."""
    voice_id: str | None = None
    tts_model: str = "eleven_v3"
    tts_chunk_chars: int = 4500
    show_name: str = "AI Daily Podcast"
    sources_file: Path = Path("sources.toml")
    output_dir: Path = Path("out")
    concurrency: int = 5
    tool_result_max_chars: int = 40_000

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env

        def get(name: str) -> str | None:
            value = env.get(name, "").strip()
            return value or None

        def get_int(name: str, default: int, minimum: int = 1) -> int:
            raw = get(name)
            if raw is None:
                return default
            try:
                value = int(raw)
            except ValueError as exc:
                raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
            if value < minimum:
                raise ConfigError(f"{name} must be at least {minimum}, got {value}")
            return value

        return cls(
            aixy_api_key=get("AIXY_API_KEY"),
            aixy_mcp_token=get("AIXY_MCP_TOKEN"),
            aixy_base_url=get("AIXY_BASE_URL") or DEFAULT_BASE_URL,
            aixy_mcp_url=get("AIXY_MCP_URL") or DEFAULT_MCP_URL,
            model=get("AIXY_MODEL") or DEFAULT_MODEL,
            web_provider=get("AIXY_WEB_PROVIDER"),
            tts_provider=get("AIXY_TTS_PROVIDER") or "elevenlabs",
            voice_id=get("ELEVENLABS_VOICE_ID"),
            tts_model=get("ELEVENLABS_MODEL") or "eleven_v3",
            tts_chunk_chars=get_int("TTS_CHUNK_CHARS", 4500, minimum=200),
            show_name=get("PODCAST_NAME") or "AI Daily Podcast",
            sources_file=Path(get("PODCAST_SOURCES") or "sources.toml"),
            output_dir=Path(get("PODCAST_OUTPUT_DIR") or "out"),
            concurrency=get_int("PODCAST_CONCURRENCY", 5),
            tool_result_max_chars=get_int("TOOL_RESULT_MAX_CHARS", 40_000, minimum=1000),
        )

    def require_inference(self) -> None:
        if not self.aixy_api_key:
            raise ConfigError(
                "AIXY_API_KEY is not set. Create a project key in the Aixy dashboard "
                "(the project that owns the routing model) and add it to .env."
            )

    def require_tools(self) -> None:
        if not self.aixy_mcp_token:
            raise ConfigError(
                "AIXY_MCP_TOKEN is not set. Create a personal MCP token in Aixy "
                "(Agents → MCP tools → API keys) and add it to .env."
            )
