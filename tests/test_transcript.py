from __future__ import annotations

import json

import pytest

from youtube_mcp import transcript as t
from youtube_mcp.transcript import Cue, Segment

from .helpers import generate_rolling_vtt

# ---------------------------------------------------------------------------
# VTT parsing
# ---------------------------------------------------------------------------


def test_parse_vtt_cues_basic():
    vtt = """WEBVTT

00:00:01.000 --> 00:00:02.500
Hello there

00:00:02.500 --> 00:00:04.000
General Kenobi
"""
    cues = t.parse_vtt_cues(vtt)
    assert len(cues) == 2
    assert cues[0] == Cue(start_s=1.0, end_s=2.5, text="Hello there")
    assert cues[1].start_s == 2.5
    assert cues[1].text == "General Kenobi"


def test_parse_vtt_cues_empty_input():
    vtt = "WEBVTT\n\n"
    assert t.parse_vtt_cues(vtt) == []


# ---------------------------------------------------------------------------
# Dedup: real fixture (rolling auto-subs)
# ---------------------------------------------------------------------------


def test_dedup_real_fixture_no_consecutive_duplicates(rolling_fixture_vtt):
    cues = t.parse_vtt_cues(rolling_fixture_vtt)
    segments = t.dedup_cues(cues)

    for prev, cur in zip(segments, segments[1:], strict=False):
        assert t._normalize(prev.text) != t._normalize(cur.text)


def test_dedup_real_fixture_char_reduction(rolling_fixture_vtt):
    cues = t.parse_vtt_cues(rolling_fixture_vtt)
    segments = t.dedup_cues(cues)

    raw_chars = sum(len(cue.text) for cue in cues)
    dedup_chars = sum(len(segment.text) for segment in segments)

    # Spec: deduped chars should be "roughly a third" of raw cue chars.
    # Measured on the real fixture this is ~0.334; use a slightly loosened
    # bound so the assertion is robust rather than brittle to the exact ratio.
    assert dedup_chars <= raw_chars * 0.4


def test_dedup_real_fixture_first_segment(rolling_fixture_vtt):
    cues = t.parse_vtt_cues(rolling_fixture_vtt)
    segments = t.dedup_cues(cues)
    assert segments[0].text == "Pour suivre cette vidéo, tu n'as besoin"


# ---------------------------------------------------------------------------
# Dedup: synthetic / edge cases
# ---------------------------------------------------------------------------


def test_dedup_manual_non_rolling_subs_pass_through():
    cues = [
        Cue(0.0, 2.0, "First line."),
        Cue(2.0, 4.0, "Second line."),
        Cue(4.0, 6.0, "Third line."),
    ]
    segments = t.dedup_cues(cues)
    assert [s.text for s in segments] == ["First line.", "Second line.", "Third line."]
    assert [(s.start_s, s.end_s) for s in segments] == [(0.0, 2.0), (2.0, 4.0), (4.0, 6.0)]


def test_dedup_manual_multiline_cue_joined_with_space():
    cues = [Cue(0.0, 2.0, "Line one\nLine two")]
    segments = t.dedup_cues(cues)
    assert len(segments) == 1
    assert segments[0].text == "Line one Line two"


def test_dedup_empty_cues_are_dropped():
    cues = [
        Cue(0.0, 1.0, ""),
        Cue(1.0, 2.0, "  \n  "),
        Cue(2.0, 3.0, "Real line"),
    ]
    segments = t.dedup_cues(cues)
    assert len(segments) == 1
    assert segments[0].text == "Real line"


def test_dedup_html_entities_decoded_and_musique_tag_kept():
    cues = [Cue(0.0, 2.0, "Tom &amp; Jerry [Musique]")]
    segments = t.dedup_cues(cues)
    assert segments[0].text == "Tom & Jerry [Musique]"


def test_dedup_rolling_hold_cue_extends_previous_segment_end():
    cues = [
        Cue(0.0, 0.01, "A"),
        Cue(0.01, 2.0, "A\nB"),
        Cue(2.0, 2.01, "B"),  # hold cue: dropped, but extends segment B's end
    ]
    segments = t.dedup_cues(cues)
    assert [s.text for s in segments] == ["A", "B"]
    # "A" also appears as the first line of the second cue, so its end extends to that cue's end.
    assert segments[0].end_s == 2.0
    assert segments[1].end_s == 2.01  # extended by the hold cue


def test_dedup_synthetic_generator_recovers_all_lines():
    vtt, expected_lines = generate_rolling_vtt(num_lines=50)
    cues = t.parse_vtt_cues(vtt)
    segments = t.dedup_cues(cues)
    assert [s.text for s in segments] == expected_lines


# ---------------------------------------------------------------------------
# Paragraph formatting
# ---------------------------------------------------------------------------


def test_format_timestamp_mm_ss():
    assert t.format_timestamp(5, use_hours=False) == "00:05"
    assert t.format_timestamp(65, use_hours=False) == "01:05"
    assert t.format_timestamp(3599, use_hours=False) == "59:59"


def test_format_timestamp_h_mm_ss():
    assert t.format_timestamp(3661, use_hours=True) == "1:01:01"
    assert t.format_timestamp(5, use_hours=True) == "0:00:05"


def test_build_paragraph_units_buckets_by_60s_and_labels_first_start():
    segments = [
        Segment(1.0, 3.0, "hello"),
        Segment(4.0, 6.0, "world"),
        Segment(65.0, 67.0, "next minute"),
    ]
    units = t.build_paragraph_units(segments, video_duration=200.0)
    assert len(units) == 2
    assert units[0].rendered == "[00:01] hello world"
    assert units[0].start_s == 1.0
    assert units[0].end_s == 6.0
    assert units[1].rendered == "[01:05] next minute"


def test_build_paragraph_units_uses_hms_over_an_hour():
    segments = [Segment(3601.0, 3602.0, "late")]
    units = t.build_paragraph_units(segments, video_duration=3700.0)
    assert units[0].rendered == "[1:00:01] late"


# ---------------------------------------------------------------------------
# Segment formatting
# ---------------------------------------------------------------------------


def test_build_segment_units_shape():
    segments = [Segment(1.005, 2.009, "hi")]
    units = t.build_segment_units(segments)
    assert units[0].payload == {"start": 1.0, "end": 2.01, "text": "hi"}
    assert units[0].start_s == 1.005


# ---------------------------------------------------------------------------
# select_segments (start/end filtering)
# ---------------------------------------------------------------------------


def test_select_segments_filters_by_start_and_end():
    segments = [Segment(0.0, 1.0, "a"), Segment(10.0, 11.0, "b"), Segment(20.0, 21.0, "c")]
    assert [s.text for s in t.select_segments(segments, start=10.0, end=None)] == ["b", "c"]
    assert [s.text for s in t.select_segments(segments, start=None, end=15.0)] == ["a", "b"]
    assert [s.text for s in t.select_segments(segments, start=5.0, end=15.0)] == ["b"]
    assert [s.text for s in t.select_segments(segments, start=None, end=None)] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_paginate_units_respects_budget_and_always_returns_one():
    segments = [Segment(float(i), float(i) + 1, "x" * 100) for i in range(10)]
    units = t.build_segment_units(segments)

    selected, next_start = t.paginate_units(units, max_chars=2000)
    assert len(selected) >= 1
    total = sum(len(u.rendered) + 1 for u in selected)
    assert total <= 2000


def test_paginate_units_always_includes_at_least_one_huge_unit():
    segments = [Segment(0.0, 1.0, "y" * 500_000)]
    units = t.build_segment_units(segments)
    selected, next_start = t.paginate_units(units, max_chars=2000)
    assert len(selected) == 1
    assert next_start is None


def test_paginate_units_clamps_max_chars():
    assert t.clamp_max_chars(1) == t.MIN_MAX_CHARS
    assert t.clamp_max_chars(10_000_000) == t.MAX_MAX_CHARS
    assert t.clamp_max_chars(5000) == 5000


def test_paginate_next_start_continuity_segments():
    segments = [Segment(float(i) * 10, float(i) * 10 + 5, "word " * 50) for i in range(30)]
    units = t.build_segment_units(segments)

    # Full call, no pagination limit big enough to hold everything.
    full_selected, full_next = t.paginate_units(units, max_chars=200_000)
    assert full_next is None
    full_texts = [u.payload["text"] for u in full_selected]

    # Now page through with a small budget and confirm we reconstruct the same thing.
    collected: list[str] = []
    start: float | None = None
    guard = 0
    while True:
        guard += 1
        assert guard < 1000
        filtered = t.select_segments(segments, start, None)
        page_units = t.build_segment_units(filtered)
        selected, next_start = t.paginate_units(page_units, max_chars=500)
        collected.extend(u.payload["text"] for u in selected)
        if next_start is None:
            break
        start = next_start

    assert collected == full_texts


def test_paginate_next_start_continuity_paragraphs():
    segments = [Segment(float(i) * 5, float(i) * 5 + 4, f"word{i}") for i in range(40)]
    video_duration = 500.0

    full_units = t.build_paragraph_units(segments, video_duration)
    full_selected, _ = t.paginate_units(full_units, max_chars=200_000)
    full_text = "\n".join(u.rendered for u in full_selected)

    collected_lines: list[str] = []
    start: float | None = None
    guard = 0
    while True:
        guard += 1
        assert guard < 1000
        filtered = t.select_segments(segments, start, None)
        units = t.build_paragraph_units(filtered, video_duration)
        selected, next_start = t.paginate_units(units, max_chars=60)
        collected_lines.extend(u.rendered for u in selected)
        if next_start is None:
            break
        start = next_start

    assert "\n".join(collected_lines) == full_text


# ---------------------------------------------------------------------------
# build_transcript_payload (end to end, pure)
# ---------------------------------------------------------------------------


def test_build_transcript_payload_paragraphs_complete():
    vtt, _ = generate_rolling_vtt(num_lines=5, seconds_per_line=2.0)
    payload = t.build_transcript_payload(
        vtt_text=vtt,
        video_id="abc12345678",
        language="en",
        requested_language="en",
        fmt="paragraphs",
        max_chars=40_000,
    )
    assert payload["video_id"] == "abc12345678"
    assert payload["format"] == "paragraphs"
    assert "text" in payload and "segments" not in payload
    assert payload["coverage"]["complete"] is True
    assert payload["coverage"]["next_start"] is None
    assert "note" not in payload
    assert payload["stats"]["segments"] == 5


def test_build_transcript_payload_segments_format():
    vtt, _ = generate_rolling_vtt(num_lines=3, seconds_per_line=2.0)
    payload = t.build_transcript_payload(
        vtt_text=vtt,
        video_id="abc12345678",
        language="en",
        requested_language="en",
        fmt="segments",
    )
    assert "segments" in payload and "text" not in payload
    assert len(payload["segments"]) == 3
    assert set(payload["segments"][0].keys()) == {"start", "end", "text"}


def test_build_transcript_payload_partial_has_note_and_next_start():
    vtt, _ = generate_rolling_vtt(num_lines=200, seconds_per_line=2.0)
    payload = t.build_transcript_payload(
        vtt_text=vtt,
        video_id="abc12345678",
        language="en",
        requested_language="en",
        fmt="segments",
        max_chars=t.MIN_MAX_CHARS,
    )
    assert payload["coverage"]["complete"] is False
    assert payload["coverage"]["next_start"] is not None
    assert "note" in payload


def test_build_transcript_payload_invalid_format_raises():
    import pytest

    vtt, _ = generate_rolling_vtt(num_lines=1)
    with pytest.raises(ValueError):
        t.build_transcript_payload(
            vtt_text=vtt,
            video_id="abc12345678",
            language="en",
            requested_language="en",
            fmt="csv",  # type: ignore[arg-type]
        )


def test_build_transcript_payload_start_end_filtering():
    vtt, _ = generate_rolling_vtt(num_lines=20, seconds_per_line=2.0)
    payload = t.build_transcript_payload(
        vtt_text=vtt,
        video_id="abc12345678",
        language="en",
        requested_language="en",
        fmt="segments",
        start=10.0,
        end=20.0,
    )
    for seg in payload["segments"]:
        assert 10.0 <= seg["start"] < 20.0


# ---------------------------------------------------------------------------
# Video id extraction
# ---------------------------------------------------------------------------


def test_extract_video_id_variants():
    vid = "xjaqUJ_48k0"
    assert t.extract_video_id(f"https://www.youtube.com/watch?v={vid}") == vid
    assert t.extract_video_id(f"https://youtu.be/{vid}") == vid
    assert t.extract_video_id(f"https://www.youtube.com/embed/{vid}") == vid
    assert t.extract_video_id(f"https://www.youtube.com/shorts/{vid}") == vid
    assert t.extract_video_id(f"https://www.youtube.com/live/{vid}") == vid
    assert t.extract_video_id(vid) == vid
    assert t.extract_video_id("not a url") is None


def test_language_attempts_orders():
    assert t.language_attempts("en") == ["en", "fr", "es", "de", "pt", "ja", "ko"]
    assert t.language_attempts("fr") == ["fr", "fr-orig", "en", "es", "de"]
    assert t.language_attempts("de") == ["de", "en", "fr", "es"]


# ---------------------------------------------------------------------------
# Synthetic long videos: full retrieval via next_start with default max_chars
# ---------------------------------------------------------------------------


def _full_retrieval_via_pagination(vtt: str, expected_lines: list[str]) -> None:
    cues = t.parse_vtt_cues(vtt)
    all_segments = t.dedup_cues(cues)
    assert [s.text for s in all_segments] == expected_lines
    video_duration = max(c.end_s for c in cues)

    collected: list[dict] = []
    start: float | None = None
    guard = 0
    while True:
        guard += 1
        assert guard < 100_000
        filtered = t.select_segments(all_segments, start, None)
        units = t.build_segment_units(filtered)
        selected, next_start = t.paginate_units(units, max_chars=t.DEFAULT_MAX_CHARS)
        total_size = sum(len(u.rendered) + 1 for u in selected)
        assert total_size <= t.DEFAULT_MAX_CHARS or len(selected) == 1
        collected.extend(u.payload["text"] for u in selected)
        if next_start is None:
            break
        start = next_start

    assert collected == expected_lines
    assert video_duration > 0


def test_synthetic_40_minute_video_fully_retrievable():
    # ~40 minutes at 2s/line -> 1200 lines
    vtt, expected_lines = generate_rolling_vtt(num_lines=1200, seconds_per_line=2.0)
    _full_retrieval_via_pagination(vtt, expected_lines)


def test_synthetic_3_hour_video_fully_retrievable():
    # ~3 hours at 2s/line -> 5400 lines
    vtt, expected_lines = generate_rolling_vtt(num_lines=5400, seconds_per_line=2.0)
    _full_retrieval_via_pagination(vtt, expected_lines)


# --- Regression: resume token must round-trip through the real payload builder ---
# The real fixture has millisecond timestamps (e.g. 52.039). Rounding next_start
# to 2 decimals (52.04) made `start >= next_start` skip that segment forever.


def _page_through_payload(vtt_text: str, fmt: str, max_chars: int) -> list[dict]:
    pages: list[dict] = []
    start: float | None = None
    for _ in range(10_000):
        payload = t.build_transcript_payload(
            vtt_text=vtt_text,
            video_id="xjaqUJ_48k0",
            language="fr",
            requested_language="fr",
            fmt=fmt,
            start=start,
            max_chars=max_chars,
        )
        pages.append(payload)
        if payload["coverage"]["complete"]:
            return pages
        # Simulate a real client: the token goes through JSON.
        start = json.loads(json.dumps(payload))["coverage"]["next_start"]
    raise AssertionError("pagination did not terminate")


@pytest.mark.parametrize("max_chars", [2000, 3333, 5000])
def test_payload_pagination_segments_lossless_on_real_fixture(rolling_fixture_vtt, max_chars):
    pages = _page_through_payload(rolling_fixture_vtt, "segments", max_chars)
    single = _page_through_payload(rolling_fixture_vtt, "segments", 200_000)
    assert len(single) == 1
    paged = [seg for page in pages for seg in page["segments"]]
    assert paged == single[0]["segments"]
    assert len(pages) > 1


# The 3-min excerpt is ~3k chars of paragraphs, so only the minimum budget paginates it.
@pytest.mark.parametrize("max_chars", [2000])
def test_payload_pagination_paragraphs_lossless_on_real_fixture(rolling_fixture_vtt, max_chars):
    pages = _page_through_payload(rolling_fixture_vtt, "paragraphs", max_chars)
    single = _page_through_payload(rolling_fixture_vtt, "paragraphs", 200_000)
    paged = "\n".join(page["text"] for page in pages if page["text"])
    assert paged == single[0]["text"]
    assert len(pages) > 1
