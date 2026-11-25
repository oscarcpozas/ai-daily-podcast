# Spike 001 — one orchestrator agent over the pipeline stages

**Question:** is there anything to gain by putting an agent on top of the pipeline, with the stages
as tools (`run_stage`, `read_checkpoint`, `finish`), instead of the deterministic runner?

**Short answer:** no. The agent recovers well from a transient failure, but it does not know when
to stop, costs ~12× in tokens and adds no guarantee the runner does not already have. The staged
structure stays; agents belong *inside* the stages, where judgment is needed (`--editorial`, the
`gather` researcher, the voice choice), not above them.

## Setup

Both paths start from the same `articles.json`, seeded with 8 real RTVE pages of 2026-11-25
(`seed-raw.json`) summarised by the real model. Inference goes through Aixy on the
`aixy/alibaba-routing` route (standing in for `aixy/podcast-agent`, which still returns 404 with
the local key).

| Path | What it is |
|------|------------|
| `runner` | the deterministic `EpisodeRun`: classify → select → script, checkpointed |
| `agent` | one agent with `run_stage` / `read_checkpoint` / `finish`, same stages |

The agent path gets **one injected transient failure** on its first `script` call (simulated HTTP
503) to see whether it recovers. The runner path runs clean.

```bash
.venv/bin/python main.py seed      # seeds articles.json with real calls
.venv/bin/python main.py runner
.venv/bin/python main.py agent     # leaves agent-transcript.json with the full trace
```

## Results (2026-11-25)

| Metric | runner | agent |
|--------|--------|-------|
| Stage calls | 9 | 9 |
| Stage tokens (prompt+completion) | 11 749 | 11 749 |
| Orchestrator calls | — | 16 (exhausts `max_steps`) |
| Orchestrator tokens | — | **111 723** |
| Script produced | yes | yes (on the retry) |
| Recovers from the injected 503 | n/a | **yes** |
| Calls `finish` with a report | n/a | **no** |
| Outcome | `EpisodeRunReport` | `AgentError: No result after 16 steps` |

The real agent trace (`agent-transcript.json`):

```
run_stage(gather) → run_stage(classify) → run_stage(select)
run_stage(script)   ← injected 503
run_stage(script)   ← retry, script written
read_checkpoint(script.json) ×11   ← loop; never calls finish
```

### What went well

- **It recovered from the transient failure.** The agent read the error, recognised it as
  retryable and called `script` again. Checkpoints made the retry cheap: `classify` was not
  repeated.
- **The checkpoint works as a contract.** Each `run_stage` left its file behind and the retry
  reused the work already done, exactly like the runner.

### What went wrong

- **It does not finish.** It read `script.json` eleven times in a row and exhausted its 16 steps
  without calling `finish`. The runner does not have this problem because it does not decide when
  to stop: it stops.
- **Cost.** 111 723 prompt tokens to do what the runner did with 9 483. The loop re-reads 6 000
  characters of checkpoint on every turn: the context grows and every step costs more. Fine with
  `qwen-flash`; not fine with the production route model.
- **No report.** Because it never called `finish` there is no structured result: no stages run, no
  account of what it recovered from, no notice that the script exists. The runner always returns a
  validated `EpisodeRunReport`.

### Side finding: long JSON from the route model

The first attempt to seed all 8 articles in **one** call returned invalid JSON twice
(`Expecting ',' delimiter`: the model closed an array and opened the next object without a comma).
One item per call worked first time. That is exactly how the real pipeline classifies (one call per
article, validated with Pydantic), so the design was already on the right side; the spike confirms
it with a real case.

## Conclusion

- **The deterministic runner stays.** It gives cost control, readable checkpoints, resumption and a
  per-run report; the orchestrator improves none of that and adds a new failure mode (not
  finishing).
- **Agents stay where there is judgment, not where there is sequence:** the `gather` researcher
  (reads and decides what is news), `--editorial` in `select`, and the voice choice. All of them
  with validated output and the deterministic path as a fallback.
- **If an orchestrator is ever wanted**, it has to bring something the runner does not have: e.g.
  deciding *which* sources to read, or re-running a stage for quality. With the stages as a black
  box and a `finish` the model can ignore, it does not pay off.

## Measurement notes

- Tokens are the ones the Aixy gateway reports (`usage` per call). The agent path's stage calls
  reported exactly the same tokens as the runner: same inputs, same model, and the gateway served
  identical responses from cache (hence the 0.1 s script too). The meaningful cost comparison is
  the **orchestrator's**, whose prompt grows with every turn of the loop.
- `voice` and `publish` are disabled in this spike: the former spends ElevenLabs credits.