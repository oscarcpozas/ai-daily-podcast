from __future__ import annotations

from typing import Any

from daily_podcast.aixy.mcp import ToolOutput
from daily_podcast.pipeline import voice


class FakeTools:
    """Stands in for AixyTools: records every call, answers like ElevenLabs through Aixy."""

    def __init__(self, *, fail_status: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_status = fail_status
        self.started = 0

    async def describe(self, name: str) -> None:
        return None

    async def execute(self, name: str, arguments: dict[str, Any]) -> ToolOutput:
        self.calls.append((name, arguments))
        if name == voice.GENERATE_SPEECH:
            self.started += 1
            body = {"flow_id": "FLOW", "session_ids": [f"S{self.started}"]}
            return ToolOutput(structured=body, texts=[str(body)])
        if self.fail_status:
            body = {"has_failures": True}
            return ToolOutput(structured=body, texts=[str(body)])
        body = {"all_completed": True}
        return ToolOutput(structured=body, texts=[str(body)], blobs=[("audio/mpeg", b"ID3audio")])

    def generations(self) -> int:
        return sum(1 for name, _ in self.calls if name == voice.GENERATE_SPEECH)
