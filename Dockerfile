FROM python:3.12-slim

# Build deps for any wheels that need to compile (polars, etc. usually have prebuilts)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# uv is faster than pip; keep the image lean
RUN pip install --no-cache-dir uv

# Install deps first so they cache when only source changes
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN uv pip install --system -e ".[dev]"

# Rest of the repo
COPY . .

# DB lives on the mounted volume; never the container filesystem
ENV POLYSIM_DB=/data/polysim.db
ENV PYTHONUNBUFFERED=1

# Make the start scripts executable. Fly's [processes] in fly.toml selects
# which one to run on each machine.
RUN chmod +x /app/deploy/start-live.sh /app/deploy/start-web.sh

# Default to the live orchestrator if no process override is given.
CMD ["/app/deploy/start-live.sh"]
