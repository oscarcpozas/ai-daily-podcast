"""HTTP face of the pipeline for Cloudflare Containers.

The Worker's Workflow calls ``POST /stage/{stage}`` once per stage and copies the files from
``GET /artifact`` into R2 at the end. Each call is the real CLI (``python -m daily_podcast``), so
the checkpoints in ``PODCAST_OUTPUT_DIR`` do the caching: a retried stage resumes, and only the
expensive one (``voice``) ever spends credits twice if the instance died before saving its audio.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

STAGES = ("gather", "classify", "select", "script", "voice", "publish")
APP_DIR = Path(os.environ.get("PODCAST_APP_DIR", "/app"))
OUTPUT_DIR = Path(os.environ.get("PODCAST_OUTPUT_DIR", "/data/out"))

app = FastAPI(title="ai-daily-podcast", docs_url=None, redoc_url=None)


@app.get("/health")
async def health() -> dict[str, object]:
    return {"ok": True, "stages": list(STAGES), "output": str(OUTPUT_DIR)}


@app.post("/stage/{stage}")
async def run_stage(
    stage: str,
    date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    fresh: bool = False,
) -> dict[str, object]:
    if stage not in STAGES:
        raise HTTPException(status_code=404, detail=f"unknown stage {stage!r}")

    command = [sys.executable, "-m", "daily_podcast", "run", "--until", stage]
    if date:
        command += ["--date", date]
    if fresh:
        command += ["--fresh"]

    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=APP_DIR,
        env={**os.environ, "PODCAST_OUTPUT_DIR": str(OUTPUT_DIR), "PYTHONUNBUFFERED": "1"},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    raw, _ = await process.communicate()
    output = raw.decode(errors="replace")
    if process.returncode != 0:
        # The Worker puts the tail in the Workflow step's error, which is what the operator reads.
        raise HTTPException(status_code=500, detail={"stage": stage, "output": output[-6000:]})
    return {"stage": stage, "returncode": process.returncode, "output": output[-4000:]}


@app.get("/artifact")
async def artifact(date: str, name: str) -> FileResponse:
    base = (OUTPUT_DIR / date).resolve()
    target = (base / name).resolve()
    if base not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail=f"{name} is not an artifact of {date}")
    return FileResponse(target, filename=target.name)
