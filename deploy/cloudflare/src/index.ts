/**
 * Cloudflare entry point: a Cron Trigger starts a Workflow, the Workflow drives the Python
 * pipeline stage by stage inside a Container, and the episode ends up in R2.
 *
 * Why a container instead of a Python Worker: the pipeline needs ffmpeg (pydub stitches the
 * episode) and the OpenAI/MCP SDKs over raw sockets, which the Pyodide runtime does not provide.
 * The image is the repository itself, so the stages stay the ones covered by the tests.
 */

import { DurableObject, WorkflowEntrypoint } from "cloudflare:workers";
import type { WorkflowEvent, WorkflowStep } from "cloudflare:workers";

const STAGES = ["gather", "classify", "select", "script", "voice", "publish"] as const;
export type Stage = (typeof STAGES)[number];

/** What the Workflow stores in R2 once `publish` wrote the files, with the type each one needs. */
const ARTIFACTS: Record<string, string> = {
  "episode.mp3": "audio/mpeg",
  "episode.md": "text/markdown; charset=utf-8",
  "script.txt": "text/plain; charset=utf-8",
  "run.json": "application/json",
};

const CONTAINER_PORT = 8080;
/** The container is asked to stay up while a Workflow moves between stages. */
const INACTIVITY_TIMEOUT_MS = 5 * 60 * 1000;
const STARTUP_ATTEMPTS = 100;

type InstanceType = "lite" | "basic" | "standard-1" | "standard-2";

export type EpisodeParams = { date: string; fresh?: boolean };

/** What the container answers to a stage call; it is what the Workflow step persists. */
type StageResult = { stage: string; returncode: number; output: string };

export interface Env {
  PODCAST_CONTAINER: DurableObjectNamespace<PodcastContainer>;
  EPISODE_WORKFLOW: Workflow<EpisodeParams>;
  EPISODES: R2Bucket;
  AIXY_API_KEY: string;
  AIXY_MCP_TOKEN: string;
  AIXY_MODEL?: string;
  AIXY_WEB_PROVIDER?: string;
  AIXY_TTS_PROVIDER?: string;
  CONTAINER_INSTANCE?: InstanceType;
  PODCAST_NAME?: string;
}

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body, null, 2), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

export class PodcastContainer extends DurableObject<Env> {
  private starting: Promise<void> | undefined;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    // A restarted instance already has the pipeline's checkpoints on its disk: keep it warm.
    if (ctx.container?.running) {
      void ctx.blockConcurrencyWhile(() =>
        ctx.container!.setInactivityTimeout(INACTIVITY_TIMEOUT_MS),
      );
    }
  }

  async fetch(request: Request): Promise<Response> {
    this.starting ??= this.startAndWaitForPort().finally(() => {
      this.starting = undefined;
    });
    await this.starting;

    const url = new URL(request.url);
    url.protocol = "http:";
    url.host = "container";
    const forwarded = new Request(url, request);
    forwarded.headers.delete("host");
    return this.ctx.container!.getTcpPort(CONTAINER_PORT).fetch(forwarded);
  }

  private async startAndWaitForPort(): Promise<void> {
    const container = this.ctx.container!;
    if (!container.running) {
      // Secrets live in the Worker; the container only sees what is passed here.
      container.start({
        image: container.images.base,
        instance: this.env.CONTAINER_INSTANCE ?? "basic",
        // The gateway and the MCP endpoint are public HTTPS services.
        enableInternet: true,
        env: {
          AIXY_API_KEY: this.env.AIXY_API_KEY,
          AIXY_MCP_TOKEN: this.env.AIXY_MCP_TOKEN,
          AIXY_MODEL: this.env.AIXY_MODEL ?? "aixy/podcast-agent",
          AIXY_WEB_PROVIDER: this.env.AIXY_WEB_PROVIDER ?? "",
          AIXY_TTS_PROVIDER: this.env.AIXY_TTS_PROVIDER ?? "elevenlabs",
          PODCAST_NAME: this.env.PODCAST_NAME ?? "AI Daily Podcast",
          PODCAST_OUTPUT_DIR: "/data/out",
        },
      });
    }
    await container.setInactivityTimeout(INACTIVITY_TIMEOUT_MS);

    const port = container.getTcpPort(CONTAINER_PORT);
    let lastError: unknown;
    for (let attempt = 0; attempt < STARTUP_ATTEMPTS; attempt++) {
      try {
        const response = await port.fetch("http://container/health", {
          signal: AbortSignal.timeout(1000),
        });
        await response.body?.cancel();
        if (!response.ok) throw new Error(`health check returned ${response.status}`);
        return;
      } catch (error) {
        lastError = error;
        await scheduler.wait(200);
      }
    }
    throw new Error("The container did not become ready on port 8080", { cause: lastError });
  }
}

export class EpisodeWorkflow extends WorkflowEntrypoint<Env, EpisodeParams> {
  async run(event: WorkflowEvent<EpisodeParams>, step: WorkflowStep) {
    const { date, fresh = false } = event.payload;
    const stages: Record<string, StageResult> = {};

    // One durable step per stage: a failure retries that stage only, and the pipeline's
    // checkpoints mean a retry resumes instead of paying for the work twice.
    for (const stage of STAGES) {
      stages[stage] = await step.do(
        `stage ${stage}`,
        {
          retries: { limit: 2, delay: "2 minutes", backoff: "exponential" },
          timeout: stage === "voice" ? "45 minutes" : "20 minutes",
        },
        async () => this.callStage(stage, date, fresh),
      );
    }

    const stored = await step.do("store the episode in R2", async () => this.store(date));
    return { date, stages, stored };
  }

  private async callStage(stage: Stage, date: string, fresh: boolean): Promise<StageResult> {
    const query = new URLSearchParams({ date });
    if (fresh) query.set("fresh", "1");
    const response = await this.env.PODCAST_CONTAINER.getByName("episode").fetch(
      `http://container/stage/${stage}?${query}`,
      { method: "POST" },
    );
    const body = await response.text();
    if (!response.ok) {
      throw new Error(`stage ${stage} failed with ${response.status}: ${body.slice(-1500)}`);
    }
    return JSON.parse(body) as StageResult;
  }

  private async store(date: string): Promise<Record<string, string>> {
    const stored: Record<string, string> = {};
    for (const [name, contentType] of Object.entries(ARTIFACTS)) {
      const response = await this.env.PODCAST_CONTAINER.getByName("episode").fetch(
        `http://container/artifact?date=${date}&name=${name}`,
      );
      if (!response.ok || !response.body) {
        throw new Error(`cannot read ${name} from the container (${response.status})`);
      }
      const key = `episodes/${date}/${name}`;
      await this.env.EPISODES.put(key, response.body, { httpMetadata: { contentType } });
      stored[name] = key;
    }
    return stored;
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/health") {
      return json({ ok: true, stages: STAGES });
    }

    if (url.pathname === "/run") {
      if (request.method !== "POST" && request.method !== "GET") {
        return json({ error: "use GET or POST" }, 405);
      }
      const date = url.searchParams.get("date") ?? today();
      if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
        return json({ error: "date must be YYYY-MM-DD" }, 400);
      }
      const fresh = url.searchParams.get("fresh") === "1";
      try {
        const instance = await env.EPISODE_WORKFLOW.create({
          id: `episode-${date}`,
          params: { date, fresh },
        });
        return json({ started: instance.id, date, fresh }, 202);
      } catch (error) {
        // Workflow ids are unique: a second trigger for the same day is not an error.
        return json({ date, note: "an instance for this date already exists", detail: `${error}` }, 409);
      }
    }

    if (url.pathname === "/episodes") {
      const listed = await env.EPISODES.list({ prefix: "episodes/" });
      return json(
        listed.objects.map((object) => ({
          key: object.key,
          size: object.size,
          uploaded: object.uploaded,
        })),
      );
    }

    if (url.pathname.startsWith("/episodes/")) {
      const key = decodeURIComponent(url.pathname.slice(1));
      const object = await env.EPISODES.get(key);
      if (!object) return json({ error: "not found", key }, 404);
      return new Response(object.body, {
        headers: {
          "content-type": object.httpMetadata?.contentType ?? "application/octet-stream",
          "cache-control": "public, max-age=300",
        },
      });
    }

    const name = env.PODCAST_NAME ?? "AI Daily Podcast";
    return new Response(
      `<!doctype html><meta charset="utf-8"><title>${name}</title>
       <h1>${name}</h1>
       <p>Episodes are generated by a Workflow that drives the Python pipeline in a Container.</p>
       <ul>
         <li><code>GET /health</code></li>
         <li><code>GET /run?date=YYYY-MM-DD</code> — start an episode</li>
         <li><code>GET /episodes</code> — list the artifacts in R2</li>
       </ul>`,
      { headers: { "content-type": "text/html; charset=utf-8" } },
    );
  },

  async scheduled(_controller: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    const date = today();
    ctx.waitUntil(
      env.EPISODE_WORKFLOW.create({ id: `episode-${date}`, params: { date } })
        .then((instance) => console.log(`started ${instance.id}`))
        .catch((error) => console.error(`could not start ${date}:`, error)),
    );
  },
} satisfies ExportedHandler<Env>;