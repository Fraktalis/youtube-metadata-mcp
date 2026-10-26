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
import time
from pathlib import Path
from typing import Any

from youtube_mcp.transcript import language_attempts

YTDLP_TIMEOUT_S = 30
RATE_LIMIT_RETRY_DELAY_S = 3
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


def _clean_stderr(stderr: str | None) -> str:
    """Drop yt-dlp's impersonation warning noise so the real error stays readable."""
    lines = [
        line
        for line in (stderr or "").splitlines()
        if line.strip() and "impersonat" not in line.lower()
    ]
    return "\n".join(lines)


def _is_rate_limited(stderr: str | None) -> bool:
    return "429" in (stderr or "") or "too many requests" in (stderr or "").lower()


def _run_ytdlp(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run yt-dlp, retrying once (after a pause) when YouTube answers HTTP 429."""
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=YTDLP_TIMEOUT_S)
    if _is_rate_limited(result.stderr):
        time.sleep(RATE_LIMIT_RETRY_DELAY_S)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=YTDLP_TIMEOUT_S)
    return result


def _list_subtitle_languages(video_id: str) -> tuple[set[str], set[str], str | None]:
    """One yt-dlp extraction listing the subtitle languages YouTube offers for the video.

    Returns (manual, automatic, error_details).
    """
    cmd = [
        *YTDLP_CMD,
        "--extractor-args",
        "youtube:player_client=default",
        "--dump-json",
        "--skip-download",
        _video_url(video_id),
    ]
    try:
        result = _run_ytdlp(cmd)
    except subprocess.TimeoutExpired:
        return set(), set(), "Timeout listing available subtitles"

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        details = _clean_stderr(result.stderr) or "Could not list available subtitles"
        return set(), set(), details

    return set(data.get("subtitles") or {}), set(data.get("automatic_captions") or {}), None


def pick_subtitle_language(
    attempts: list[str], manual: set[str], automatic: set[str]
) -> str | None:
    """Pick the track to download, preferring real tracks over machine translations.

    YouTube lists every auto-caption language as a machine translation of the original
    track (plain `en`, `fr`...), next to the untranslated original (`en-orig`). Translated
    tracks are the ones YouTube answers HTTP 429 to, so real tracks (manual subtitles or
    `<lang>-orig`) win across the whole fallback order, and translations come last.
    """
    for candidate in attempts:
        if candidate in manual:
            return candidate
        if f"{candidate}-orig" in automatic:
            return f"{candidate}-orig"
        if candidate.endswith("-orig") and candidate in automatic:
            return candidate
    return next((candidate for candidate in attempts if candidate in automatic), None)


def fetch_subtitles(video_id: str, lang: str) -> tuple[str, str]:
    """Fetch a transcript's raw VTT text for video_id, with language fallback.

    Does one extraction to learn which languages exist, picks the best available language of
    the fallback order (real tracks before machine translations), then downloads that single
    track (instead of one full extraction per candidate language, which got the server
    rate-limited by YouTube).

    Returns (vtt_text, actual_language). Raises SubtitleFetchError if no
    subtitle could be fetched in any attempted language.
    """
    attempts = language_attempts(lang)

    def fail(details: str | None) -> SubtitleFetchError:
        return SubtitleFetchError(
            "Failed to fetch transcript in any available language",
            details=details,
            attempted_languages=attempts,
        )

    manual, automatic, error = _list_subtitle_languages(video_id)
    if error:
        raise fail(error)

    chosen = pick_subtitle_language(attempts, manual, automatic)
    if chosen is None:
        offered = ", ".join(sorted(manual | automatic)) or "none"
        raise fail(f"No subtitles in the attempted languages. Languages offered: {offered}")

    with tempfile.TemporaryDirectory(prefix=f"ytdlp-{video_id}-") as tmpdir:
        out_template = str(Path(tmpdir) / "%(id)s.%(ext)s")
        cmd = [
            *YTDLP_CMD,
            "--extractor-args",
            "youtube:player_client=default",
            "--write-sub",
            "--write-auto-sub",
            "--skip-download",
            "--sleep-subtitles",
            "1",
            "--sub-lang",
            chosen,
            _video_url(video_id),
            "-o",
            out_template,
        ]
        try:
            result = _run_ytdlp(cmd)
        except subprocess.TimeoutExpired:
            raise fail(f"Timeout fetching subtitles for '{chosen}'") from None

        # Ignore returncode: yt-dlp can exit non-zero even after writing subs.
        vtt_files = sorted(Path(tmpdir).glob(f"{video_id}*.vtt"))
        if vtt_files:
            vtt_path = vtt_files[0]
            return vtt_path.read_text(encoding="utf-8"), _lang_from_filename(vtt_path.name, chosen)

        details = _clean_stderr(result.stderr) or f"No subtitles for '{chosen}'"
        if _is_rate_limited(result.stderr):
            details = f"YouTube rate limit (HTTP 429) while downloading '{chosen}'. {details}"
        raise fail(details)


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
