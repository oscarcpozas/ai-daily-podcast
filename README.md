# AI Daily Podcast

A daily Spanish news podcast, produced end-to-end by an AI agent: it reads trusted sources,
classifies and selects the stories, writes the script, voices it and masters the episode.

**Every model call and every tool runs through [Aixy](https://aixy-gateway.com).** This project
has no direct provider keys: no OpenAI, no Anthropic, no ElevenLabs or TinyFish credentials. Aixy
provides the intelligence and the connectors behind one OpenAI-compatible endpoint and one MCP
server.

```
sources.toml ─► gather ─► classify ─► select ─► script ─► voice ─► publish
               (web tools   (sentiment,  (dedupe,   (ElevenLabs  (ElevenLabs   (mp3 + show
                via Aixy)    category)    balance)   v3 tags)     via Aixy)      notes)
```

| Stage    | What it does                                                            | Reached through Aixy                        |
|----------|-------------------------------------------------------------------------|---------------------------------------------|
| gather   | One agent per trusted source reads its front page, extracts and classifies articles | MCP → whichever web connector is connected  |
| classify | Category, sentiment and relevance the researcher did not set            | inference → `aixy/podcast-agent`            |
| select   | Merges the same event across sources and balances topics; `--editorial` adds an agent | deterministic, no model call by default     |
| script   | Writes the episode with ElevenLabs v3 audio tags                        | inference → `aixy/podcast-agent`            |
| voice    | Picks a Spanish voice, then generates the speech chunk by chunk         | MCP → ElevenLabs creative API               |
| publish  | Joins the chunks with the intro, masters the mp3, writes show notes     | local (ffmpeg)                              |

## Why Aixy

The agent asks Aixy for three things and nothing else:

- **One routing model.** The code requests `aixy/podcast-agent`, a virtual route in Aixy that
  decides which underlying models answer. Changing those models is configured in Aixy, not in
  this code.
- **Connectors without provider credentials in the code.** A web connector and ElevenLabs are
  connected once in Aixy. The agent discovers their actions over MCP at runtime and runs them
  through Aixy's `aixy_execute_tool`. There is no provider SDK and no provider key here.
- **No connector is hardcoded.** The code asks Aixy which connected provider can read a web page
  or search the web, and works with the answer: swap Exa for TinyFish (or anything else) in Aixy
  and the next run uses it. `AIXY_WEB_PROVIDER` only pins one when an account has several.
- **Visibility per run.** Each run prints its Aixy session id, the number of model calls and the
  tokens used, broken down by routed model, and records them in `run.json`.

## Requirements

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- [ffmpeg](https://ffmpeg.org/) for mastering: `brew install ffmpeg`
- An Aixy account with a project key (for the routing model), a personal MCP token, and
  connectors: one that can read the web (Exa, TinyFish…) and ElevenLabs

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
uv run daily-podcast run --editorial                # an agent reviews the lineup
uv run daily-podcast sources                        # list the trusted sources
```

Each stage writes a checkpoint under `out/<date>/`, so an interrupted run resumes at the stage
where it stopped. If you change the lineup (for example `--max-stories 5`), the script and the
audio that belonged to the old lineup are discarded and redone.

Speech is the one stage that spends credits, so it is handled conservatively:

- Each chunk's job is saved to `audio/jobs.json` before it is polled, so a retry polls the same
  job instead of generating the audio twice.
- Audio is matched to its chunk by a fingerprint of the text, the voice and the model. Audio for
  other text or another voice is never reused.
- `--fresh` redoes every stage, including the script and the voice choice. The model rewrites the
  script, so expect new audio and new credits. `--regenerate-audio` discards the saved audio and
  jobs and generates them again for the *same* script, for example after an ElevenLabs outage.
- If ElevenLabs reports a job as failed, it is dropped from `jobs.json` and the next run
  generates that chunk again.

## Configuration

- **`sources.toml`**: the trusted sources. Each entry has a `name`, a `url` and an optional
  `link_pattern` regex that article URLs must match. `{today}` and `{today_compact}` are replaced
  with the episode date.
- **`.env`**: credentials and overrides. See [`.env.example`](.env.example) for every variable.
  `AIXY_WEB_PROVIDER` pins the connector that reads the web; leave it empty and Aixy's discovery
  picks whichever one is connected. `AIXY_TTS_PROVIDER` names the connector that serves speech
  (default `elevenlabs`). The file wins over exported variables: a stale
  `export AIXY_API_KEY=...` from another project no longer shadows it, and `doctor` prints which
  source each credential came from.

### Routing model targets

`AIXY_MODEL` (default `aixy/podcast-agent`) is a routing model, and the models behind it decide
whether a run can finish:

- **Point it at non-reasoning targets** (`qwen-flash`, `claude-haiku`). Reasoning targets
  (`deepseek-v4*`, `glm-5.3`) spend the whole token budget on `reasoning_content`: the gateway
  returns empty `content`, and with a real reading step (tools plus a 10–26 KB tool result) they
  reason past the 30 s per-target timeout, so every target fails and the run dies with
  `502 - Every eligible routing target failed`.
- A `502` from the route is therefore usually a configuration signal, not a transient one:
  re-check the target list before retrying.

## Layout

```
src/daily_podcast/
  aixy/        LLM client, MCP client (discovery, execution, retries), capability discovery
               and the tool-use agent loop
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
uv run pyright       # type check
```

## Roadmap

The decisions behind the pipeline live in the spec issue (#14). Open work is tracked there too:

- #15 — use Exa as the web connector (today TinyFish is the only one connected)
- #16 — create the `aixy/podcast-agent` route in the podcast project
- #17 — first end-to-end rehearsal, gather through publish
- #18 — publish each episode to a Google Drive folder through Aixy
- #19 — add a type checker to the toolchain

## License

No license has been chosen yet. All rights reserved until one is added.
