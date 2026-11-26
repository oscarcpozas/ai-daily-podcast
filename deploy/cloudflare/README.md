# Deploying to Cloudflare

[![Deploy to Cloudflare](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/oscarcpozas/ai-daily-podcast)

One Worker, one container, one bucket. The Cron Trigger starts a Workflow every morning, the
Workflow drives the pipeline stage by stage inside the container, and the episode lands in R2.

## The pieces

| Piece | What it does here | Why it is the right primitive |
| --- | --- | --- |
| **Cron Trigger** (`0 6 * * *`) | Starts the day's episode | A schedule is a schedule; no scheduler to keep alive |
| **Workflows** (`EpisodeWorkflow`) | One durable step per stage, with retries and a timeout each | A failed `voice` step retries that step only, and the run survives a Worker redeploy |
| **Container** (`PodcastContainer`) | Runs this repository as-is: Python, the OpenAI/MCP clients, `ffmpeg` for pydub | Python Workers run on Pyodide: no `ffmpeg`, and `httpx`-based SDKs need raw sockets |
| **Durable Object** | Owns the container instance, starts it with the run's env | One warm instance per episode keeps the stage checkpoints on disk |
| **R2** (`EPISODES`) | `episodes/<date>/{episode.mp3,episode.md,script.txt,run.json}` | Storage with no egress bill, which is what a podcast feed needs |
| **Worker** (`fetch`) | `/health`, `/run?date=…`, `/episodes`, `/episodes/<key>` | Manual runs without waiting for the cron |
| **Secrets** | `AIXY_API_KEY`, `AIXY_MCP_TOKEN` | The container only receives what the Durable Object passes to `start()` |

No D1, no KV, no Queues: the pipeline's own JSON checkpoints and R2 cover state, and adding
bindings would only make the one-click deploy harder to reproduce.

## Deploy it

### One click

Press the button above. Cloudflare clones the repository into your account, provisions the R2
bucket, builds the image and deploys. Then add the two secrets in the dashboard
(**Worker → Settings → Variables and Secrets**):

- `AIXY_API_KEY` — from your Aixy project
- `AIXY_MCP_TOKEN` — from your Aixy project's MCP endpoint

### From a laptop

```bash
npm install
npx wrangler secret put AIXY_API_KEY
npx wrangler secret put AIXY_MCP_TOKEN
npx wrangler deploy          # builds the image with Docker, so Docker must be running
```

Then trigger an episode:

```bash
curl "https://ai-daily-podcast.<your-subdomain>.workers.dev/run?date=$(date -u +%F)"
npx wrangler workflows instances describe episode-workflow episode-$(date -u +%F)
```

The first deploy needs a few minutes before the container answers: the image is being provisioned.

## Make it your own podcast

Everything that makes it *this* show is configuration, not code:

- **`sources.toml`** — the sites to read, with an optional `link_pattern` regex. Add yours
  (a newspaper, a subreddit mirror, a blog index, a company newsroom) and the researcher adapts:
  it discovers the web connector through Aixy and reads what it finds.
- **`src/daily_podcast/prompts/`** — the voice of the show: `gather.md`, `classify.md`,
  `select.md`, `script.md`, `editorial.md`.
- **`vars` in `wrangler.jsonc`** — `PODCAST_NAME`, `AIXY_MODEL`, `AIXY_WEB_PROVIDER`,
  `AIXY_TTS_PROVIDER`, `CONTAINER_INSTANCE`, and the cron expression.
- **Aixy side** — the routing model's targets, the connected web connector and the TTS connector.
  The pipeline names capabilities, never providers.

## What it costs

- **Containers** — billed per vCPU-second, GiB-second and disk. `basic` (¼ vCPU, 1 GiB) runs a
  full episode in a handful of minutes, well inside the Workers Paid monthly allowance; raise
  `CONTAINER_INSTANCE` to `standard-1` if pydub ever runs out of memory on a long episode.
- **R2** — a few MB per episode, and downloads are free.
- **Aixy** — the real cost of the show: model tokens plus ElevenLabs characters, all inside the
  gateway's session and cost reports.

## Known limits

- The container's disk is per-instance. If an instance is evicted mid-run the next stage restarts
  from an empty workspace and redoes the earlier stages (the `voice` step's checkpoints are the
  ones that would cost credits). Keeping checkpoints in R2 is the follow-up.
- One container instance, one episode at a time: triggered by design, since ElevenLabs bills per
  character.
- No podcast feed yet: the audio sits in R2 under `episodes/<date>/`. An RSS Worker over that
  prefix is the next step.