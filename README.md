# AI Daily Podcast

A daily Spanish news podcast, produced end-to-end by an AI agent: it reads trusted sources,
classifies and selects the stories, writes the script, voices it and masters the episode.

**Every model call and every tool runs through [Aixy](https://aixy-gateway.com).** This project
has no direct provider keys: no OpenAI, no Anthropic, no ElevenLabs or TinyFish credentials. Aixy
provides the intelligence and the connectors behind one OpenAI-compatible endpoint and one MCP
server.

```
sources.toml ─► gather ─► classify ─► select ─► script ─► voice ─► publish
               (TinyFish    (sentiment,  (dedupe,   (ElevenLabs  (ElevenLabs   (mp3 + show
                via Aixy)    category)    balance)   v3 tags)     via Aixy)      notes)
```

| Stage    | What it does                                                            | Reached through Aixy                 |
|----------|-------------------------------------------------------------------------|--------------------------------------|
| gather   | One agent per trusted source reads its front page and extracts articles | MCP → TinyFish `fetch_content`       |
| classify | Category, sentiment and relevance for every article                     | inference → `aixy/podcast-agent`     |
| select   | Merges the same event reported by several sources and balances topics  | deterministic, no model call         |
| script   | Writes the episode with ElevenLabs v3 audio tags                        | inference → `aixy/podcast-agent`     |
| voice    | Picks a Spanish voice, then generates the speech chunk by chunk         | MCP → ElevenLabs creative API        |
| publish  | Joins the chunks with the intro, masters the mp3, writes show notes     | local (ffmpeg)                       |

## Why Aixy

The agent asks Aixy for three things and nothing else:

- **One routing model.** The code requests `aixy/podcast-agent`, a virtual route in Aixy that
  decides which underlying models answer. Changing those models is configured in Aixy, not in
  this code.
- **Connectors without provider credentials in the code.** TinyFish and ElevenLabs are connected
  once in Aixy. The agent discovers their actions over MCP at runtime and runs them through
  Aixy's `aixy_execute_tool`. There is no provider SDK and no provider key here.
- **Visibility per run.** Each run prints its Aixy session id, the number of model calls and the
  tokens used, broken down by routed model, and records them in `run.json`.

## Requirements

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- [ffmpeg](https://ffmpeg.org/) for mastering: `brew install ffmpeg`
- An Aixy account with a project key (for the routing model), a personal MCP token, and TinyFish
  and ElevenLabs connected

## Quick start

```bash
git clone https://github.com/oscarcpozas/ai-daily-podcast.git
cd ai-daily-podcast
uv sync
cp .env.example .env          # then fill in AIXY_API_KEY and AIXY_MCP_TOKEN

uv run daily-podcast doctor   # checks the config, the routing model and the connectors
uv run daily-podcast run      # writes out/<date>/episode.mp3
```

Useful options:

```bash
uv run daily-podcast run --date 2026-11-20          # another day
uv run daily-podcast run --until script             # stop after a stage
uv run daily-podcast run --source "RTVE" --fresh    # one source, ignore checkpoints
uv run daily-podcast sources                        # list the trusted sources
```

Each stage writes a checkpoint under `out/<date>/`, so an interrupted run resumes at the stage
where it stopped. Speech is checkpointed per chunk before it is polled, so a retry does not
generate audio twice.

## Configuration

- **`sources.toml`**: the trusted sources. Each entry has a `name`, a `url` and an optional
  `link_pattern` regex that article URLs must match. `{today}` and `{today_compact}` are replaced
  with the episode date.
- **`.env`**: credentials and overrides. See [`.env.example`](.env.example) for every variable.

## Layout

```
src/daily_podcast/
  aixy/        LLM client, MCP client (discovery, execution, retries) and the tool-use agent loop
  pipeline/    one module per stage
  prompts/     the prompts, as Markdown templates
  runner.py    wires the stages and checkpoints them
  cli.py       the `daily-podcast` command
assets/        intro jingle
docs/          architecture diagram of the first version
```

## Development

```bash
uv run pytest        # unit tests: no network, no credentials needed
uv run ruff check    # lint
uv run ruff format   # format
```

## Roadmap

- Publish each episode to a Google Drive folder. The `publish` stage is where it will plug in.

## License

No license has been chosen yet. All rights reserved until one is added.
