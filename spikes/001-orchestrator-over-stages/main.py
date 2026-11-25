"""Spike 001 — one orchestrator agent driving the pipeline stages as tools.

Compares:
  runner  the deterministic EpisodeRun (classify → select → script)          [baseline]
  agent   one Aixy agent with `run_stage`/`read_checkpoint`/`finish` tools   [candidate]

Both start from the same seeded articles.json (real RTVE pages of 2026-11-25) and use the same
real Aixy inference (route: aixy/alibaba-routing). The agent path injects ONE transient
failure on the first `script` call to see whether it recovers; the runner path runs clean.

Run with the project venv and AIXY_API_KEY in the environment:

    .venv/bin/python main.py seed      # model-summarises seed-raw.json → articles.json
    .venv/bin/python main.py runner    # deterministic baseline
    .venv/bin/python main.py agent     # orchestrator over stages-as-tools
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

from daily_podcast.aixy import AgentError, AixyLLM, LocalTool, run_agent
from daily_podcast.config import Settings
from daily_podcast.models import Article, ArticleSet
from daily_podcast.runner import EpisodeRun, RunOptions, Stage

HERE = Path(__file__).parent
DAY = date(2026, 11, 25)
MODEL = "aixy/alibaba-routing"  # stands in for the future aixy/podcast-agent route
STAGES = ("gather", "classify", "select", "script", "voice", "publish")
CHECKPOINTS = ("articles.json", "stories.json", "selection.json", "script.json", "run.json")

SEED_SYSTEM = """You are the researcher of a daily Spanish news podcast. You are given the raw \
text of ONE news page from rtve.es (navigation noise included; ignore it). Reply with only JSON:
{"url": "...", "source": "RTVE", "title": "...", "summary": "...", "key_facts": ["...", ...]}
- Use exactly the url from the material.
- summary: 3 to 5 factual sentences in Spanish (Spain), no opinions, only facts present in the text.
- key_facts: up to 5 short facts with the names, figures and places that matter.
- Do not classify the article and do not invent facts."""


def settings_for(run: str) -> Settings:
    return replace(Settings.from_env(), model=MODEL, output_dir=HERE / run)


def workspace(run: str) -> Path:
    return HERE / run / DAY.isoformat()


def show(label: str, seconds: float, llm: AixyLLM, extra: dict[str, Any]) -> None:
    print(
        json.dumps(
            {
                "mode": label,
                "seconds": round(seconds, 1),
                "calls": llm.usage.calls,
                "prompt_tokens": llm.usage.prompt_tokens,
                "completion_tokens": llm.usage.completion_tokens,
                "models": dict(llm.usage.models),
                **extra,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


# --- seed: real pages → articles.json, one real Aixy call per article ------------------------
# (A single 8-article call produced malformed JSON from the route model — see README. The
#  per-article shape is what production classify uses, so the seed matches it.)


async def seed() -> None:
    raw = json.loads((HERE / "seed-raw.json").read_text(encoding="utf-8"))
    llm = AixyLLM(settings_for("agent"), session_id="spike-seed")
    articles: list[Article] = []
    for url, page in raw["articles"].items():
        article = await llm.complete_json(
            system=SEED_SYSTEM,
            user=json.dumps(
                {"url": url, "title": page["title"], "text": page["text"][:5000]},
                ensure_ascii=False,
            ),
            schema=Article,
            max_tokens=900,
        )
        articles.append(article)
        print(f"  summarised {url.rsplit('/', 1)[-1]}: {len(article.summary)} chars")
    article_set = ArticleSet(articles=articles)
    for run in ("agent", "runner"):
        path = workspace(run) / "articles.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(article_set.model_dump_json(indent=2), encoding="utf-8")
    show("seed", 0.0, llm, {"articles": len(articles)})


# --- baseline: the deterministic runner -------------------------------------------------------


async def runner() -> None:
    episode = EpisodeRun(settings_for("runner"), RunOptions(day=DAY, until=Stage.SCRIPT))
    started = time.monotonic()
    report = await episode.run()
    show(
        "runner",
        time.monotonic() - started,
        episode.llm,
        {"stages": report.stages, "outputs": report.outputs},
    )


# --- candidate: one orchestrator agent over stages-as-tools ------------------------------------


class TracingLLM:
    """Records every tool call the orchestrator makes, and forwards to the real AixyLLM."""

    def __init__(self, llm: AixyLLM) -> None:
        self.llm = llm
        self.trace: list[str] = []
        self.transcript: list[dict[str, Any]] = []

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        message = await self.llm.chat(messages, **kwargs)
        calls = [
            f"{call.function.name}({call.function.arguments})"
            for call in (message.tool_calls or [])
            if call.type == "function"
        ]
        self.trace.extend(calls)
        self.transcript.append(
            {
                "assistant": message.content or "",
                "calls": calls,
                "last_user": messages[-1]["content"][:200]
                if messages[-1]["role"] == "user"
                else "",
            }
        )
        return message


ORCHESTRATOR_SYSTEM = f"""You are the producer of a daily Spanish news podcast. Your workspace \
for today is {workspace("agent")}, and it already holds a gathered articles.json checkpoint.

Your job: get today's episode written, up to and including the script. The voice and publish \
stages are disabled in this spike (generating speech spends credits) — do not try to enable them.

You drive the pipeline through tools:
- run_stage(stage): runs one stage — gather, classify, select, script, voice, publish. Stages \
are checkpointed: re-running a finished stage is cheap and reports "cached".
- read_checkpoint(file): shows the content of a checkpoint file in your workspace \
({", ".join(CHECKPOINTS)}).

A stage may fail. A transient gateway error (5xx, timeout) is worth a retry; a content error \
may mean an earlier stage needs redoing. Decide yourself; do not invent results — rely on what \
the tools return.

When the script exists, call finish with your report:
{{"episode_ready": true|false, "stages_run": ["..."], "recovered_from": "..."|null, "notes": "..."}}"""


async def agent() -> None:
    settings = settings_for("agent")
    orchestrator = TracingLLM(AixyLLM(settings, session_id="spike-orchestrator"))
    stage_llm = AixyLLM(settings, session_id="spike-agent-stages")
    injected: dict[str, bool] = {"failed_once": False}

    async def run_stage(arguments: dict[str, Any]) -> str:
        name = str(arguments.get("stage", ""))
        if name not in STAGES:
            return json.dumps({"error": f"unknown stage {name!r}; valid: {', '.join(STAGES)}"})
        if name in ("voice", "publish"):
            return json.dumps(
                {"stage": name, "status": "skipped", "detail": "disabled in this spike"}
            )
        if name == "script" and not injected["failed_once"]:
            # Fault injection: the first script attempt dies like a flaky gateway would.
            injected["failed_once"] = True
            return json.dumps(
                {"stage": name, "status": "error", "detail": "Aixy returned HTTP 503 (transient)"}
            )
        started = time.monotonic()
        episode = EpisodeRun(settings, RunOptions(day=DAY, until=Stage(name)), llm=stage_llm)
        report = await episode.run()
        return json.dumps(
            {
                "stage": name,
                "seconds": round(time.monotonic() - started, 1),
                "stages": report.stages,
                "outputs": report.outputs,
            },
            ensure_ascii=False,
        )

    async def read_checkpoint(arguments: dict[str, Any]) -> str:
        name = str(arguments.get("file", ""))
        path = workspace("agent") / name
        if name not in CHECKPOINTS or not path.is_file():
            return json.dumps({"error": f"no such checkpoint; valid: {', '.join(CHECKPOINTS)}"})
        return path.read_text(encoding="utf-8")[:6000]

    async def finish(arguments: dict[str, Any]) -> dict[str, Any]:
        return arguments

    tools = [
        LocalTool(
            "run_stage",
            "Run one pipeline stage. Checkpointed: finished stages report cached.",
            {
                "type": "object",
                "properties": {"stage": {"type": "string", "enum": list(STAGES)}},
                "required": ["stage"],
            },
            run_stage,
        ),
        LocalTool(
            "read_checkpoint",
            "Read a checkpoint file of your workspace.",
            {
                "type": "object",
                "properties": {"file": {"type": "string", "enum": list(CHECKPOINTS)}},
                "required": ["file"],
            },
            read_checkpoint,
        ),
        LocalTool(
            "finish",
            "Hand over your final report. Call it exactly once, at the end.",
            {
                "type": "object",
                "properties": {
                    "episode_ready": {"type": "boolean"},
                    "stages_run": {"type": "array", "items": {"type": "string"}},
                    "recovered_from": {"type": ["string", "null"]},
                    "notes": {"type": "string"},
                },
                "required": ["episode_ready", "stages_run", "notes"],
            },
            finish,
            terminal=True,
        ),
    ]

    started = time.monotonic()
    try:
        result = await run_agent(
            orchestrator,
            system=ORCHESTRATOR_SYSTEM,
            user="Produce today's episode up to the script.",
            tools=tools,
            max_steps=16,
        )
        outcome: Any = result
        error: str | None = None
    except AgentError as exc:
        outcome = None
        error = str(exc)
    (HERE / "agent-transcript.json").write_text(
        json.dumps(
            {
                "error": error,
                "seconds": round(time.monotonic() - started, 1),
                "trace": orchestrator.trace,
                "turns": orchestrator.transcript,
                "stage_llm": {
                    "calls": stage_llm.usage.calls,
                    "tokens": stage_llm.usage.prompt_tokens + stage_llm.usage.completion_tokens,
                },
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    show(
        "agent",
        time.monotonic() - started,
        orchestrator.llm,
        {
            "error": error,
            "report": outcome,
            "trace": orchestrator.trace,
            "stage_llm_calls": stage_llm.usage.calls,
            "stage_llm_tokens": stage_llm.usage.prompt_tokens + stage_llm.usage.completion_tokens,
            "script_exists": (workspace("agent") / "script.json").is_file(),
        },
    )


if __name__ == "__main__":
    asyncio.run({"seed": seed, "runner": runner, "agent": agent}[sys.argv[1]]())
