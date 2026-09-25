"""Subprocess wrappers around yt-dlp.

Every call writes into its own `tempfile.TemporaryDirectory()`, so concurrent
requests (even for the same video id) never see each other's files, and
nothing is ever written to the process's current working directory.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from youtube_mcp.transcript import language_attempts

YTDLP_TIMEOUT_S = 30
# Run the yt-dlp pinned in this interpreter's environment, not whatever is on PATH.
YTDLP_CMD = (sys.executable, "-m", "yt_dlp")


class YtDlpError(Exception):
    """Base error for a failed yt-dlp invocation."""

    def __init__(self, message: str, details: str | None = None) -> None:
        super().__init__(message)
        self.details = details


class SubtitleFetchError(YtDlpError):
    """Raised when no subtitle could be fetched in any attempted language."""

    def __init__(self, message: str, details: str | None, attempted_languages: list[str]) -> None:
        super().__init__(message, details)
        self.attempted_languages = attempted_languages


class MetadataFetchError(YtDlpError):
    """Raised when video metadata could not be fetched."""


def _video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _lang_from_filename(filename: str, fallback: str) -> str:
    # e.g. "xjaqUJ_48k0.fr.vtt" -> "fr"
    parts = filename.split(".")
    if len(parts) >= 3:
        return parts[-2]
    return fallback


def fetch_subtitles(video_id: str, lang: str) -> tuple[str, str]:
    """Fetch a transcript's raw VTT text for video_id, with language fallback.

    Returns (vtt_text, actual_language). Raises SubtitleFetchError if no
    subtitle could be fetched in any attempted language.
    """
    attempts = language_attempts(lang)
    last_details: str | None = None

    for attempt_lang in attempts:
        with tempfile.TemporaryDirectory(prefix=f"ytdlp-{video_id}-") as tmpdir:
            out_template = str(Path(tmpdir) / "%(id)s.%(ext)s")
            cmd = [
                *YTDLP_CMD,
                "--extractor-args",
                "youtube:player_client=default",
                "--write-sub",
                "--write-auto-sub",
                "--skip-download",
                "--sub-lang",
                attempt_lang,
                _video_url(video_id),
                "-o",
                out_template,
            ]
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=YTDLP_TIMEOUT_S
                )
            except subprocess.TimeoutExpired:
                last_details = f"Timeout fetching subtitles for '{attempt_lang}'"
                continue

            # Ignore returncode: yt-dlp can exit non-zero even after writing subs.
            vtt_files = sorted(Path(tmpdir).glob(f"{video_id}*.vtt"))
            if vtt_files:
                vtt_path = vtt_files[0]
                actual_lang = _lang_from_filename(vtt_path.name, attempt_lang)
                vtt_text = vtt_path.read_text(encoding="utf-8")
                return vtt_text, actual_lang

            if result.stderr and result.stderr.strip():
                last_details = result.stderr.strip()
            else:
                last_details = f"No subtitles for '{attempt_lang}'"

    raise SubtitleFetchError(
        "Failed to fetch transcript in any available language",
        details=last_details,
        attempted_languages=attempts,
    )


def _format_upload_date(raw_date: str) -> str:
    if len(raw_date) == 8:
        return f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
    return raw_date


def fetch_metadata(video_id: str) -> dict[str, Any]:
    """Fetch video metadata via `yt-dlp --dump-json`. Raises MetadataFetchError on failure."""
    cmd = [
        *YTDLP_CMD,
        "--extractor-args",
        "youtube:player_client=default",
        "--dump-json",
        "--skip-download",
        _video_url(video_id),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=YTDLP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise MetadataFetchError("Timeout fetching metadata") from None

    if result.returncode != 0 or not result.stdout.strip():
        raise MetadataFetchError("Failed to fetch metadata", details=result.stderr)

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MetadataFetchError("Failed to parse metadata JSON", details=str(exc)) from exc

    return {
        "id": data.get("id"),
        "title": data.get("title"),
        "description": data.get("description"),
        "upload_date": _format_upload_date(data.get("upload_date", "")),
        "channel": data.get("channel") or data.get("uploader"),
        "channel_id": data.get("channel_id"),
        "channel_url": data.get("channel_url"),
        "duration": data.get("duration"),
        "duration_string": data.get("duration_string"),
        "view_count": data.get("view_count"),
        "like_count": data.get("like_count"),
        "tags": data.get("tags", []),
        "categories": data.get("categories", []),
        "thumbnail": data.get("thumbnail"),
        "webpage_url": data.get("webpage_url"),
    }
