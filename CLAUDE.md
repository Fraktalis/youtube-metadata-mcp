# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project overview

`youtube-metadata-mcp` is an MCP server (FastMCP + Starlette) that exposes two tools over SSE —
`get_transcript` and `get_metadata` — backed by `yt-dlp`. It runs as a Docker container behind a reverse
proxy that handles auth; the server itself has none. Consumer: the Claude.ai connector at `/sse`.

## Architecture

```
youtube_mcp/
  __init__.py    # __version__ — bump this together with pyproject.toml on release
  __main__.py    # entrypoint: uvicorn serving `server.app` on 0.0.0.0:${PORT:-5000}
  transcript.py  # PURE functions only, no I/O: VTT parsing, dedup, paragraph/segment
                 # formatting, pagination. This is the module with the interesting logic
                 # and the one with the most test coverage — keep it side-effect-free so
                 # it stays trivially testable without mocking subprocess or the network.
  ytdlp.py       # subprocess wrappers around yt-dlp: fetch_subtitles(video_id, lang) ->
                 # (vtt_text, actual_lang), fetch_metadata(video_id) -> dict. All I/O and
                 # subprocess handling lives here, isolated from transcript.py.
  server.py      # FastMCP @mcp.tool() definitions + the Starlette `app` (SSE mount + GET /health)
tests/
  fixtures/rolling_autosub_excerpt.fr.vtt   # REAL data (first 3 min of xjaqUJ_48k0) — do not modify
  ...
```

The old root-level `server.py`, `api_keys.json` and `GEMINI.md` are gone; there is no n8n compatibility
layer anymore.

### Transcript pipeline

1. `ytdlp.fetch_subtitles`: extract the video ID from the URL (supports `watch?v=`, `youtu.be/`,
   `embed/`, `shorts/`, `live/`, and raw 11-char IDs), run `yt-dlp` in a per-call
   `tempfile.TemporaryDirectory()` with `--write-sub --write-auto-sub --skip-download
   --extractor-args youtube:player_client=default`, 30s timeout, off the event loop via
   `anyio.to_thread.run_sync`. Success = a `.vtt` file exists in that tempdir; the returncode is
   ignored (yt-dlp can exit non-zero on partial success). Manual subs win over auto-subs when both
   flags produce a match. Language fallback order: `en` → `en, fr, es, de, pt, ja, ko`; `fr` → `fr,
   fr-orig, en, es, de`; anything else → `lang, en, fr, es`.
2. `transcript.dedup_cues`: YouTube's rolling auto-subs repeat the previous cue's line(s) and add one new
   line per cue. Dedup walks cues in order, keeping the last-emitted line per track; a cue whose lines
   are all already emitted only extends the previous segment's `end_s` and produces no new segment.
   Manual/non-rolling subtitles (no repeated lines) must pass through as one segment per cue/line,
   unchanged.
3. `transcript.build_paragraph_units` / `build_segment_units`: bucket segments into 60s windows (`[mm:ss]`, or
   `[h:mm:ss]` once `video_duration >= 3600`) or return them as `{start, end, text}` records.
4. `transcript.select_segments` + `paginate_units` (driven by `build_transcript_payload`): filter by `start`/`end`, accumulate rendered units until `max_chars` would be
   exceeded (always return at least one unit even if it alone exceeds the budget), and compute
   `coverage` (`range_start`, `range_end`, `video_duration`, `complete`, `next_start`).

### Invariants the tests protect — do not regress these

- **Dedup correctness**: on the real fixture, each spoken sentence appears exactly once in the
  deduplicated output, and total deduplicated chars are ≤ ~1/3 of the raw cue text chars.
- **Dedup idempotence / robustness**: empty cues, HTML entities (`&amp;` → `&`), and `[Musique]`-style
  tags must not break dedup or duplicate/drop lines.
- **Pagination continuity**: calling `get_transcript` again with `start=coverage.next_start` must
  resume exactly where the previous call left off — no gap, no repeated unit.
- **`coverage.complete`**: `false` iff at least one unit inside the requested `[start, end)` window was
  left out of this response; `next_start` is `null` exactly when `complete` is `true`.
- **At-least-one-unit guarantee**: pagination always returns at least one unit, even a single one
  larger than `max_chars`, rather than an empty response.
- **Resume token is exact**: `coverage.next_start` is never rounded (rounding 52.039 up to 52.04 once
  dropped segments). Tests page through `build_transcript_payload` on the real fixture with the token
  round-tripped through JSON.
- **Works behind a reverse proxy**: the SDK's `sse_app()` defaults to localhost-only Host checking
  (421 on any public Host header). `server.transport_security_from_env()` disables it unless
  `ALLOWED_HOSTS` is set; a test posts with a public Host header.
- **`transcript.py` has no I/O**: any test for it should run with no filesystem or subprocess access.
  If a change needs I/O in there, it belongs in `ytdlp.py` instead.
- **Tempdir isolation**: concurrent transcript requests must not be able to delete or clobber each
  other's `.vtt` files (this is why each call gets its own `tempfile.TemporaryDirectory()`, not the
  CWD).

## Commands

```bash
uv sync --frozen              # install locked runtime + dev deps
uv run python -m youtube_mcp  # run the server locally
uv run pytest                 # tests, network tests deselected by default (marked @pytest.mark.network)
uv run ruff check .           # lint
uv run ruff format --check .  # format check
docker build -t youtube-metadata-mcp .
docker compose up -d --build  # local container
```

## Conventions

- Python 3.12, managed with `uv`; `pyproject.toml` + `uv.lock` are both committed and `uv sync --frozen`
  must not need to re-resolve.
- All runtime dependencies are pinned exactly (`==`) in `pyproject.toml`. Never relax a pin to a range
  without a reason recorded in the commit message.
- yt-dlp is a pinned Python dependency, invoked as `sys.executable -m yt_dlp` (the interpreter's own
  environment, not PATH) — never a `latest` binary download.
- yt-dlp is installed with the `default,deno` extras: `default` pulls `yt-dlp-ejs` and `deno` pulls the
  Deno JS runtime, both required since yt-dlp 2025.11.12 to solve YouTube's player challenge. Dropping
  the extras silently breaks YouTube extraction in production while unit tests stay green.
- Docker base image and the `uv` image are pinned to exact tags. `:latest` is never used anywhere — images, GHCR release tags,
  Docker Hub pulls, none of it.
- Default `pytest` run has no network access; anything hitting real YouTube is marked
  `@pytest.mark.network` and excluded by `addopts = "-m 'not network'"`.
- Fixtures are real data excerpts (e.g. `tests/fixtures/rolling_autosub_excerpt.fr.vtt`), not
  hand-written stand-ins — don't replace them with synthetic VTT unless a synthetic case is what's
  actually being tested.
- ruff config: line-length 100, `select = ["E", "F", "I", "UP", "B", "SIM"]`.
- No n8n compatibility, no API-key auth, no `AUTH_ENABLED` — auth is entirely the reverse proxy's job.

## Release flow

1. Bump `__version__` in `youtube_mcp/__init__.py` and the version in `pyproject.toml` together.
2. Add a `CHANGELOG.md` entry.
3. `git tag vX.Y.Z && git push origin vX.Y.Z`.
4. `.github/workflows/release.yml` re-runs lint + tests, builds the amd64 image, pushes it to
   `ghcr.io/<owner>/youtube-metadata-mcp` tagged `X.Y.Z`, `X.Y`, and the commit SHA (no `:latest`), and
   publishes a GitHub Release with auto-generated notes.

## CI

`.github/workflows/ci.yml` runs on every PR and push to `main`: lint + test, then a Docker build with a
smoke test (container starts, `/health` returns 200, `python -m yt_dlp --version` and `deno --version`
both succeed inside the image). All third-party Actions are pinned to a commit SHA with a version
comment; Dependabot keeps `github-actions`, `docker`, and `uv` (daily, because of yt-dlp) current.
