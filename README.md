# youtube-metadata-mcp

An MCP (Model Context Protocol) server that gives an LLM two tools for YouTube videos:
transcripts (deduplicated, paginated) and metadata. It wraps [`yt-dlp`](https://github.com/yt-dlp/yt-dlp)
and serves over SSE for the Claude.ai connector. This is a fork/rewrite of a small n8n-oriented
transcript proxy, refocused as a standalone MCP service (the n8n JSON-RPC endpoints and API-key auth
have been removed — see [CHANGELOG.md](./CHANGELOG.md)).

## Why

YouTube auto-generated subtitles are "rolling": each cue repeats the previous line and adds one new
line, so raw WebVTT is roughly 6x the size of the actual spoken text, and a long video can silently
overflow a client's response budget. This server deduplicates cues into compact text and paginates by
character budget, always telling the caller whether it received the whole transcript.

## Tools

### `get_transcript`

| Param | Type | Default | Notes |
|---|---|---|---|
| `url` | string | — | Any YouTube URL form (`watch?v=`, `youtu.be/`, `embed/`, `shorts/`, `live/`) or a raw 11-char video ID |
| `language` | string | `en` | Preferred subtitle language; falls back through a per-language order (e.g. `fr` → `fr, fr-orig, en, es, de`) if unavailable |
| `format` | `"paragraphs"` \| `"segments"` | `"paragraphs"` | `paragraphs`: text grouped into 60s buckets, one `[mm:ss] ...` line per bucket. `segments`: list of `{start, end, text}` |
| `start` | float \| null | `null` | Only return units starting at or after this time (seconds) |
| `end` | float \| null | `null` | Only return units starting before this time (seconds) |
| `max_chars` | int | `40000` | Character budget for this call, clamped to `[2000, 200000]` |

Response:

```json
{
  "video_id": "xjaqUJ_48k0",
  "language": "fr",
  "requested_language": "fr",
  "format": "paragraphs",
  "text": "[00:00] ...\n[01:00] ...",
  "coverage": {
    "range_start": 0.0,
    "range_end": 931.5,
    "video_duration": 1430.0,
    "complete": false,
    "next_start": 932.1
  },
  "stats": {"raw_cues": 1342, "segments": 450, "returned_chars": 39876},
  "note": "Partial transcript: call get_transcript again with start=<next_start> to continue."
}
```

`segments` responses carry a `segments` array instead of `text`; the two are mutually exclusive.

**Pagination example** — retrieving a full 40-minute transcript:

1. Call `get_transcript(url=...)`. Response has `coverage.complete: false`, `coverage.next_start: 932.1`.
2. Call `get_transcript(url=..., start=932.1)`. Continue until `coverage.complete: true` (`next_start` is
   `null`).

Errors look like `{"error": "...", "details": "...", "attempted_languages": [...]}`; an unparseable URL
returns `{"error": "Invalid YouTube URL"}`.

### `get_metadata`

Takes `url`. Returns `id`, `title`, `description`, `upload_date` (`YYYY-MM-DD`), `channel`, `channel_id`,
`channel_url`, `duration`, `duration_string`, `view_count`, `like_count`, `tags`, `categories`,
`thumbnail`, `webpage_url`.

## Deployment

The image is published to GHCR on every tagged release (never `:latest` — always pin an exact version).

```yaml
services:
  app:
    image: ghcr.io/fraktalis/youtube-metadata-mcp:v2.0.0
    container_name: youtube-metadata-mcp
    restart: unless-stopped
    ports:
      - "5000:5000"
    labels:
      - "wud.watch=false"
```

Authentication is the reverse proxy's job (Caddy, etc.) — the server has none of its own. Point the
Claude.ai connector at `https://<your-domain>/sse`.

`GET /health` returns `{"status": "ok", "version": "..."}` with HTTP 200; wire it into Uptime Kuma as an
HTTP(s) monitor.

## Local development

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync                       # installs runtime + dev deps, creates .venv
uv run python -m youtube_mcp  # serve on 0.0.0.0:5000 (PORT env var overrides)
```

### Tests

```bash
uv run pytest                 # default: no network (network tests are marked and deselected)
uv run pytest -m network      # include tests that hit real YouTube
```

### Lint

```bash
uv run ruff check .
uv run ruff format --check .
```

### Docker

```bash
docker compose up -d --build
docker logs youtube-metadata-mcp
```

## Release process

1. Bump the version in `youtube_mcp/__init__.py` (`__version__`) and `pyproject.toml`.
2. Update `CHANGELOG.md`.
3. Tag and push: `git tag vX.Y.Z && git push origin vX.Y.Z`.
4. `release.yml` runs tests, builds the amd64 image, and pushes it to GHCR tagged `X.Y.Z`, `X.Y`, and the
   commit SHA — then publishes a GitHub Release with auto-generated notes.

## Dependency updates

Dependabot checks the `uv` ecosystem **daily**: yt-dlp breaks against YouTube frequently enough that it
needs to be caught fast. Other Python dependency bumps are grouped to avoid noise; GitHub Actions and the
Docker base images are checked weekly.
