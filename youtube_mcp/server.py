"""FastMCP tools + Starlette app.

SSE is mounted exactly as before (so the connector URL `/sse` keeps working)
plus a `GET /health` endpoint for uptime checks. All legacy n8n JSON-RPC
compatibility has been removed; auth is the reverse proxy's job.
"""

from __future__ import annotations

from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from youtube_mcp import __version__, transcript, ytdlp

mcp = MCPServer("youtube-transcript-custom", version=__version__)


@mcp.tool()
async def get_transcript(
    url: str,
    language: str = "en",
    format: str = "paragraphs",
    start: float | None = None,
    end: float | None = None,
    max_chars: int = transcript.DEFAULT_MAX_CHARS,
) -> dict[str, Any]:
    """Get a YouTube video's transcript, deduplicated and paginated.

    YouTube's auto-generated subtitles are "rolling": most cues just repeat
    the previous line plus one new one, so raw VTT is roughly 3x bloated.
    This tool dedups that into clean segments before returning anything.

    Output is compact "paragraphs" by default: lines like "[mm:ss] text..."
    (or "[h:mm:ss] ..." for videos an hour or longer), one line per 60-second
    bucket of speech. Pass format="segments" for a list of
    {"start", "end", "text"} objects with exact per-line timing instead.

    Long transcripts are paginated by character budget (max_chars, default
    40000, clamped to [2000, 200000]) rather than being silently truncated.
    Always check `coverage.complete`: if it is false, the response also
    carries `coverage.next_start` — call this tool again with
    `start=<that value>` to fetch the next page. Concatenating every page in
    order reproduces the full transcript with no gaps or repeats. Use `start`
    and `end` (seconds) together to fetch a specific time window instead.

    Args:
        url: YouTube video URL (any format: watch?v=, youtu.be/, embed/,
            shorts/, live/) or a raw 11-character video id.
        language: Requested subtitle language code (e.g. "en", "fr").
            Defaults to "en". If unavailable, common alternatives are tried
            and the actually-returned language is reported back.
        format: "paragraphs" (default, compact) or "segments" (exact timing).
        start: Only include content starting at or after this many seconds.
        end: Only include content starting before this many seconds.
        max_chars: Character budget for this page (default 40000).

    Returns:
        On success: video_id, language, requested_language, format,
        text or segments, coverage (range_start, range_end, video_duration,
        complete, next_start), and stats (raw_cues, segments, returned_chars).
        On failure: {"error": ..., "details": ..., "attempted_languages": [...]}.
    """
    video_id = transcript.extract_video_id(url)
    if not video_id:
        return {"error": "Invalid YouTube URL"}

    if format not in ("paragraphs", "segments"):
        return {"error": f"Invalid format: {format!r}. Must be 'paragraphs' or 'segments'."}

    try:
        vtt_text, actual_lang = await anyio.to_thread.run_sync(
            ytdlp.fetch_subtitles, video_id, language
        )
    except ytdlp.SubtitleFetchError as exc:
        return {
            "error": str(exc),
            "details": exc.details,
            "attempted_languages": exc.attempted_languages,
        }

    return transcript.build_transcript_payload(
        vtt_text=vtt_text,
        video_id=video_id,
        language=actual_lang,
        requested_language=language,
        fmt=format,  # type: ignore[arg-type]
        start=start,
        end=end,
        max_chars=max_chars,
    )


@mcp.tool()
async def get_metadata(url: str) -> dict[str, Any]:
    """Retrieve metadata for a YouTube video.

    Args:
        url: YouTube video URL (any format) or a raw 11-character video id.

    Returns:
        Dictionary containing id, title, description, upload_date
        (YYYY-MM-DD), channel, channel_id, channel_url, duration,
        duration_string, view_count, like_count, tags, categories,
        thumbnail, and webpage_url. On failure: {"error": ..., "details": ...}.
    """
    video_id = transcript.extract_video_id(url)
    if not video_id:
        return {"error": "Invalid YouTube URL"}

    try:
        return await anyio.to_thread.run_sync(ytdlp.fetch_metadata, video_id)
    except ytdlp.MetadataFetchError as exc:
        return {"error": str(exc), "details": exc.details}


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "version": __version__})


app = mcp.sse_app()
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
