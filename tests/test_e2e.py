"""End-to-end test against a real YouTube video. Hits the network and yt-dlp.

Deselected by default (see pyproject.toml `addopts`); run explicitly with:
    uv run pytest -m network
"""

from __future__ import annotations

import pytest

from youtube_mcp import transcript, ytdlp

# A short, stable, long-lived video (YouTube's own "Me at the zoo").
SHORT_VIDEO_ID = "jNQXAC9IVRw"


@pytest.mark.network
def test_fetch_and_build_transcript_for_real_short_video():
    vtt_text, actual_lang = ytdlp.fetch_subtitles(SHORT_VIDEO_ID, "en")
    assert vtt_text.strip().startswith("WEBVTT")

    payload = transcript.build_transcript_payload(
        vtt_text=vtt_text,
        video_id=SHORT_VIDEO_ID,
        language=actual_lang,
        requested_language="en",
        fmt="paragraphs",
    )
    assert payload["video_id"] == SHORT_VIDEO_ID
    assert payload["coverage"]["complete"] is True
    assert payload["stats"]["raw_cues"] > 0


@pytest.mark.network
def test_fetch_metadata_for_real_short_video():
    metadata = ytdlp.fetch_metadata(SHORT_VIDEO_ID)
    assert metadata["id"] == SHORT_VIDEO_ID
    assert metadata["webpage_url"]
