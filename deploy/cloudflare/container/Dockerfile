# The image IS this repository: the pipeline runs unmodified, with ffmpeg available for pydub.
# Build context is the repository root (see build_context in wrangler.jsonc).
FROM python:3.12-slim

# pydub shells out to ffmpeg to master the episode from the generated chunks.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The project itself. `pip install .` reads README.md and LICENSE, hence both.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY sources.toml ./
COPY assets ./assets
RUN pip install --no-cache-dir .

# The HTTP face the Worker talks to.
COPY deploy/cloudflare/container/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY deploy/cloudflare/container/app.py ./container_app.py

ENV PODCAST_APP_DIR=/app \
    PODCAST_OUTPUT_DIR=/data/out \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data/out
EXPOSE 8080
CMD ["uvicorn", "container_app:app", "--host", "0.0.0.0", "--port", "8080"]