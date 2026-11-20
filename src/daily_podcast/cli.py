"""Command line interface: ``daily-podcast run``, ``daily-podcast doctor``…"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape
from rich.table import Table

from daily_podcast import __version__
from daily_podcast.aixy import AixyInferenceError, AixyLLM, AixyTools
from daily_podcast.config import ConfigError, Settings
from daily_podcast.errors import PodcastError
from daily_podcast.runner import EpisodeRun, RunOptions, Stage
from daily_podcast.sources import load_sources

app = typer.Typer(
    help="A daily news podcast produced end-to-end by an AI agent, powered by Aixy.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

DEFAULT_INTRO = Path("assets/intro.mp3")
EXPECTED_PROVIDERS = ("TinyFish", "ElevenLabs")


def _setup(verbose: bool) -> Settings:
    load_dotenv()
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False, markup=False)],
    )
    for noisy in ("httpx", "httpx2", "openai", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return Settings.from_env()


def _fail(message: str) -> typer.Exit:
    console.print(f"[bold red]✗[/] {escape(message)}")
    return typer.Exit(code=1)


@app.callback(invoke_without_command=True)
def main(
    version: Annotated[bool, typer.Option("--version", help="Show the version and exit.")] = False,
) -> None:
    if version:
        console.print(f"ai-daily-podcast {__version__}")
        raise typer.Exit()


@app.command()
def run(
    day: Annotated[
        str | None, typer.Option("--date", help="Episode date (YYYY-MM-DD). Default: today.")
    ] = None,
    until: Annotated[Stage, typer.Option(help="Stop after this stage.")] = Stage.PUBLISH,
    fresh: Annotated[bool, typer.Option(help="Ignore checkpoints and redo every stage.")] = False,
    regenerate_audio: Annotated[
        bool,
        typer.Option(help="Discard generated speech and generate it again (spends credits)."),
    ] = False,
    source: Annotated[
        list[str] | None, typer.Option("--source", "-s", help="Only use these sources (by name).")
    ] = None,
    max_stories: Annotated[int, typer.Option(min=1, max=30)] = 10,
    per_category: Annotated[int, typer.Option(min=1, max=10)] = 3,
    min_relevance: Annotated[int, typer.Option(min=1, max=5)] = 2,
    intro: Annotated[Path | None, typer.Option(help="Intro jingle.")] = DEFAULT_INTRO,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Produce an episode: gather → classify → select → script → voice → publish."""
    settings = _setup(verbose)
    try:
        episode_day = date.fromisoformat(day) if day else date.today()
    except ValueError as exc:
        raise _fail(f"Invalid --date: {day}") from exc

    options = RunOptions(
        day=episode_day,
        until=until,
        fresh=fresh,
        regenerate_audio=regenerate_audio,
        max_stories=max_stories,
        per_category=per_category,
        min_relevance=min_relevance,
        intro=intro,
        sources_filter=source or [],
    )

    def notify(stage: Stage, message: str) -> None:
        console.print(f"[bold cyan]{stage.value:>8}[/] {message}")

    console.rule(
        f"[bold]{settings.show_name}[/] · {episode_day.isoformat()} · model {settings.model}"
    )
    episode = EpisodeRun(settings, options, notify=notify)
    try:
        report = asyncio.run(episode.run())
    except PodcastError as exc:
        raise _fail(str(exc)) from exc

    usage = episode.llm.usage if episode._llm else None
    table = Table(title="Run summary", show_header=True, header_style="bold")
    table.add_column("Stage")
    table.add_column("Status")
    table.add_column("Details")
    for name, info in report.stages.items():
        details = ", ".join(f"{k}={v}" for k, v in info.items() if k != "status")
        table.add_row(name, info["status"], details)
    console.print(table)
    if usage and usage.calls:
        models = ", ".join(f"{m} ×{n}" for m, n in usage.models.most_common())
        console.print(
            f"Aixy: {usage.calls} model calls · {usage.prompt_tokens + usage.completion_tokens:,} "
            f"tokens · routed to {models} · session [bold]{report.session_id}[/]"
        )
    for name, path in report.outputs.items():
        console.print(f"[green]✓[/] {name}: {path}")


@app.command()
def doctor(
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
    ping: Annotated[bool, typer.Option(help="Send one tiny request to the routing model.")] = True,
) -> None:
    """Check configuration, the routing model and the Aixy MCP connectors."""
    settings = _setup(verbose)
    ok = True

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= passed
        mark = "[green]✓[/]" if passed else "[red]✗[/]"
        console.print(f"{mark} {escape(label)}" + (f" [dim]{escape(detail)}[/]" if detail else ""))

    try:
        sources = load_sources(settings.sources_file)
        check("sources", True, f"{len(sources)} in {settings.sources_file}")
    except ConfigError as exc:
        check("sources", False, str(exc))

    check("AIXY_API_KEY", bool(settings.aixy_api_key), settings.aixy_base_url)
    check("AIXY_MCP_TOKEN", bool(settings.aixy_mcp_token), settings.aixy_mcp_url)

    async def probe() -> None:
        if settings.aixy_api_key and ping:
            llm = AixyLLM(settings, session_id="doctor")
            try:
                message = await llm.chat(
                    [{"role": "user", "content": "Reply with OK."}], max_tokens=10
                )
                resolved = ", ".join(llm.usage.models) or "?"
                check(f"model {settings.model}", True, f"→ {resolved}: {message.content!r}")
            except (AixyInferenceError, PodcastError) as exc:
                check(f"model {settings.model}", False, str(exc))
        if settings.aixy_mcp_token:
            try:
                async with AixyTools(settings) as tools:
                    check("Aixy MCP", True, ", ".join(tools.helpers))
                    connected = {
                        c.get("name", ""): bool(c.get("connected"))
                        for c in await tools.connections()
                    }
                    for provider in EXPECTED_PROVIDERS:
                        check(f"connector {provider}", connected.get(provider, False))
                    try:
                        actions = await tools.search("text to speech", provider="elevenlabs")
                        check("discovery", True, f"{len(actions)} ElevenLabs actions found")
                    except PodcastError as exc:
                        check("discovery", False, str(exc))
            except (PodcastError, OSError) as exc:
                check("Aixy MCP", False, str(exc))

    asyncio.run(probe())
    if not ok:
        raise typer.Exit(code=1)


@app.command()
def sources(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    """List the trusted news sources."""
    settings = _setup(verbose)
    try:
        items = load_sources(settings.sources_file)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc
    table = Table(show_header=True, header_style="bold")
    for column in ("Name", "Front page", "Link pattern", "Max"):
        table.add_column(column)
    for item in items:
        # Patterns contain [a-z-]: escape them, or Rich swallows them as markup.
        pattern = escape(item.link_pattern) if item.link_pattern else "—"
        table.add_row(item.name, str(item.url), pattern, str(item.max_articles))
    console.print(table)


if __name__ == "__main__":
    app()
