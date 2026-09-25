# syntax=docker/dockerfile:1
#
# youtube-metadata-mcp — multi-stage build (amd64 only)
#
# Base image: Debian slim-bookworm, not alpine. yt-dlp's YouTube extraction
# needs a JS runtime (Deno, installed via the yt-dlp[deno] extra) and musl/alpine has historically
# caused issues with that class of native/JS tooling; slim-bookworm (glibc)
# is the safer target for yt-dlp deployments.
ARG PYTHON_VERSION=3.12.16

FROM python:${PYTHON_VERSION}-slim-bookworm AS base

# --- uv, pinned to an exact release, taken from the official distroless image ---
FROM ghcr.io/astral-sh/uv:0.12.19 AS uv

# ---------------------------------------------------------------------------
# builder: resolve and install the locked dependency set + the project itself
# ---------------------------------------------------------------------------
FROM base AS builder

COPY --from=uv /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Install dependencies first (better layer caching), then the project.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---------------------------------------------------------------------------
# runtime: minimal final image, non-root, no build tooling
# ---------------------------------------------------------------------------
FROM base AS runtime

# ffmpeg is intentionally NOT installed: this service only reads subtitles
# (--skip-download), it never transcodes or muxes media.
# Deno (JS runtime required by yt-dlp EJS for YouTube challenges) and
# yt-dlp-ejs come from the locked `yt-dlp[default,deno]` extras in .venv/bin.

RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid app --create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder --chown=app:app /app /app

ENV PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH

USER app

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/health', timeout=3).status == 200 else 1)"

CMD ["python", "-m", "youtube_mcp"]
