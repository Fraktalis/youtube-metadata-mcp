# Changelog

## [2.0.0] - 2026-09-25

### Added
- Restructured project as an installable package (`youtube_mcp/`), managed with `uv`, targeting Python 3.12.
- `get_transcript` pagination: `start`, `end`, `max_chars` (clamped to `[2000, 200000]`) params let a full
  transcript be retrieved across several calls with no silent truncation. Every response includes a
  `coverage` block (`range_start`, `range_end`, `video_duration`, `complete`, `next_start`) so a caller
  always knows what it has and how to continue (`start=next_start`).
- `format` param on `get_transcript`: `paragraphs` (default, dedup'd 60s-bucketed text) or `segments`
  (list of `{start, end, text}`).
- Dedup algorithm for rolling YouTube auto-subs: each spoken line is emitted once instead of once per
  overlapping cue, cutting transcript size roughly 3x or more.
- `ALLOWED_HOSTS` / `ALLOWED_ORIGINS` env vars to opt into Host/Origin checking on the SSE transport
  (off by default so the server works behind a reverse proxy).
- `GET /health` endpoint returning `{"status": "ok", "version": "..."}`, for uptime monitoring (Uptime Kuma).
- Docker image published to GHCR (`ghcr.io/<owner>/youtube-metadata-mcp`) on every tagged release, built via
  GitHub Actions with `docker/build-push-action`, tagged by exact semver (never `:latest`).
- CI (`ci.yml`): lint (`ruff check`, `ruff format --check`), test (`pytest`, no network by default), and a
  Docker build + smoke test (health check, `yt-dlp --version`, `deno --version`) on every PR and push to main.
- Dependabot: daily checks on the `uv` ecosystem (yt-dlp breaks against YouTube often and needs to stay
  fresh) with other Python deps grouped to keep noise down; weekly checks on GitHub Actions and Docker base
  images.
- `yt-dlp[default,deno]` extras: `yt-dlp-ejs` + Deno JS runtime, required to solve YouTube's player
  challenge (yt-dlp ≥ 2025.11.12). Both locked in `uv.lock` and updated by Dependabot.

### Changed
- `get_transcript` default output is now compact, deduplicated `paragraphs` text instead of raw WebVTT-derived
  JSON cues.
- yt-dlp is now a pinned Python dependency (invoked as `python -m yt_dlp` from the venv), not a `latest`
  binary downloaded at image build time.
- yt-dlp subprocess calls now run in a per-call `tempfile.TemporaryDirectory()` instead of the working
  directory, fixing a race where concurrent requests for different videos could delete each other's
  temporary `.vtt` files.
- Tool execution now runs subprocess work off the event loop (`anyio.to_thread.run_sync`), so one slow
  yt-dlp call no longer blocks other requests.
- `--write-sub` is now passed alongside `--write-auto-sub`, so manual subtitles are preferred over
  auto-generated ones when both exist.
- Base image switched to `python:3.12-slim-bookworm` (glibc) from Alpine; runtime dependencies are pinned
  to exact versions.
- CORS middleware removed: the previous wildcard-origin-with-credentials configuration was invalid, and
  CORS is not needed for server-to-server MCP traffic.

### Removed
- **Breaking**: the legacy n8n JSON-RPC endpoints (`POST /`, `/test_transcript`, `/test_metadata`) and
  `X-API-KEY` / `api_keys.json` authentication. Auth is now solely the reverse proxy's responsibility; the
  `/sse` connector endpoint is unaffected.
- `AUTH_ENABLED` environment variable and the `require_api_key` decorator.
- ffmpeg from the runtime image (subtitles-only workload never needed it).

### Fixed
- Tempdir race condition described above (concurrent requests corrupting each other's `.vtt` files).
- Silent truncation of long transcripts: previously the client silently cut off responses around ~25k
  tokens with no indication the transcript was incomplete; `coverage.complete` now makes this explicit.

## [Unreleased] - 2025-11-20

### Added
- **Multi-language fallback strategy**: Automatically tries alternative languages if requested language is unavailable
  - English request: tries en → fr → es → de → pt → ja → ko
  - French request: tries fr → fr-orig → en → es → de
  - Other languages: tries requested → en → fr → es
- **Language metadata in responses**: All endpoints now return `language` (actual language obtained) and `requested_language` fields
- **Enhanced error reporting**: Error responses include `details` and `attempted_languages` fields for better debugging

### Changed
- **Improved error handling**: No longer relies on yt-dlp returncode (which can be non-zero even on success)
- **Success detection**: Now checks for actual `.vtt` file creation instead of subprocess return code
- **Timeout protection**: Added 30-second timeout to yt-dlp subprocess calls

### Fixed
- **Videos in non-English languages**: Fixed issue where videos with non-English primary language (e.g., French) would fail when requesting English subtitles
- **Partial failures**: Fixed cases where yt-dlp returns error code but successfully downloads subtitles
- **Better cleanup**: Properly removes existing VTT files before each download attempt

## Previous Versions

See git history for earlier changes.
