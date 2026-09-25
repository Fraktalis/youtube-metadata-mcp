"""Pure, I/O-free functions: VTT parsing, dedup, formatting and pagination.

Nothing in this module touches the filesystem, network or a subprocess. Every
function takes plain data in and returns plain data out, which is what makes
it straightforward to unit test against fixtures and synthetic input.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import webvtt

Format = Literal["paragraphs", "segments"]

MIN_MAX_CHARS = 2000
MAX_MAX_CHARS = 200_000
DEFAULT_MAX_CHARS = 40_000

_WHITESPACE_RE = re.compile(r"\s+")

_VIDEO_ID_PATTERNS = [
    re.compile(r"(?:youtube\.com/(?:watch\?v=|embed/|shorts/|live/))([A-Za-z0-9_-]{11})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
    re.compile(r"^([A-Za-z0-9_-]{11})$"),
]


@dataclass(frozen=True)
class Cue:
    """A single WebVTT cue after timestamp parsing, before dedup."""

    start_s: float
    end_s: float
    text: str  # raw text, possibly multiple lines joined by '\n'


@dataclass
class Segment:
    """A single spoken line, emitted once by the dedup algorithm."""

    start_s: float
    end_s: float
    text: str


@dataclass
class Unit:
    """One paginatable rendering unit (a paragraph line, or a segment dict)."""

    start_s: float
    end_s: float
    rendered: str
    payload: Any


def extract_video_id(url: str) -> str | None:
    """Extract an 11-char YouTube video id from a URL, or return it if already an id.

    Supports watch?v=, youtu.be/, embed/, shorts/, live/ and a raw 11-char id.
    """
    url = url.strip()
    for pattern in _VIDEO_ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return None


def language_attempts(lang: str) -> list[str]:
    """Return the ordered list of language codes to try for a requested language."""
    if lang == "en":
        return ["en", "fr", "es", "de", "pt", "ja", "ko"]
    if lang == "fr":
        return ["fr", "fr-orig", "en", "es", "de"]
    return [lang, "en", "fr", "es"]


def _decode_line(line: str) -> str:
    return html.unescape(line).strip()


def _normalize(line: str) -> str:
    return _WHITESPACE_RE.sub(" ", line).strip()


def _timestamp_to_seconds(ts: str) -> float:
    parts = ts.split(":")
    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours = "0"
        minutes, seconds = parts
    else:
        raise ValueError(f"Unrecognized VTT timestamp: {ts!r}")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def parse_vtt_cues(vtt_text: str) -> list[Cue]:
    """Parse raw WebVTT text into a list of Cue (start_s, end_s, text)."""
    parsed = webvtt.WebVTT.from_string(vtt_text)
    return [
        Cue(
            start_s=_timestamp_to_seconds(caption.start),
            end_s=_timestamp_to_seconds(caption.end),
            text=caption.text,
        )
        for caption in parsed.captions
    ]


def dedup_cues(cues: Sequence[Cue]) -> list[Segment]:
    """Collapse rolling auto-sub cues into one Segment per spoken line.

    Rolling auto-subs repeat the previous line then add a new one; 10ms "hold"
    cues repeat everything already emitted. Manual/non-rolling subtitles (no
    line repeats a previous one) pass straight through, one segment per cue,
    multi-line cues joined with a space.
    """
    segments: list[Segment] = []
    last_line_norm: str | None = None
    last_segment: Segment | None = None

    for cue in cues:
        lines = [_decode_line(line) for line in cue.text.split("\n")]
        lines = [line for line in lines if line]
        if not lines:
            continue

        start_idx = 0
        if last_line_norm is not None and _normalize(lines[0]) == last_line_norm:
            if last_segment is not None:
                last_segment.end_s = max(last_segment.end_s, cue.end_s)
            start_idx = 1

        new_lines = lines[start_idx:]
        if new_lines:
            text = " ".join(new_lines)
            segment = Segment(start_s=cue.start_s, end_s=cue.end_s, text=text)
            segments.append(segment)
            last_segment = segment
            last_line_norm = _normalize(new_lines[-1])

    return segments


def select_segments(
    segments: Sequence[Segment], start: float | None, end: float | None
) -> list[Segment]:
    """Filter segments whose start_s falls in [start, end)."""
    return [
        segment
        for segment in segments
        if (start is None or segment.start_s >= start) and (end is None or segment.start_s < end)
    ]


def format_timestamp(seconds: float, use_hours: bool) -> str:
    """Format a second count as mm:ss, or h:mm:ss when use_hours is True."""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if use_hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def build_paragraph_units(segments: Sequence[Segment], video_duration: float) -> list[Unit]:
    """Group segments into 60-second buckets, one Unit per non-empty bucket."""
    use_hours = video_duration >= 3600
    buckets: dict[int, list[Segment]] = {}
    order: list[int] = []
    for segment in segments:
        key = int(segment.start_s // 60)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(segment)

    units: list[Unit] = []
    for key in order:
        bucket_segments = buckets[key]
        text = " ".join(segment.text for segment in bucket_segments if segment.text)
        if not text:
            continue
        first_start = bucket_segments[0].start_s
        last_end = max(segment.end_s for segment in bucket_segments)
        line = f"[{format_timestamp(first_start, use_hours)}] {text}"
        units.append(Unit(start_s=first_start, end_s=last_end, rendered=line, payload=line))
    return units


def build_segment_units(segments: Sequence[Segment]) -> list[Unit]:
    """One Unit per segment, rendered as the JSON that will be returned."""
    units: list[Unit] = []
    for segment in segments:
        payload = {
            "start": round(segment.start_s, 2),
            "end": round(segment.end_s, 2),
            "text": segment.text,
        }
        rendered = json.dumps(payload, ensure_ascii=False)
        units.append(
            Unit(start_s=segment.start_s, end_s=segment.end_s, rendered=rendered, payload=payload)
        )
    return units


def clamp_max_chars(max_chars: int) -> int:
    return max(MIN_MAX_CHARS, min(MAX_MAX_CHARS, max_chars))


def paginate_units(
    units: Sequence[Unit], max_chars: int, *, sep_len: int = 1, overhead: int = 0
) -> tuple[list[Unit], float | None]:
    """Accumulate units in order under a character budget.

    Always returns at least one unit (a single huge unit is returned whole).
    The second element is the start_s of the first unit *not* returned, or
    None when every unit was included.
    """
    budget = clamp_max_chars(max_chars)
    selected: list[Unit] = []
    total = overhead
    for unit in units:
        size = len(unit.rendered) + (sep_len if selected else 0)
        if selected and total + size > budget:
            return selected, unit.start_s
        selected.append(unit)
        total += size
    return selected, None


def build_transcript_payload(
    *,
    vtt_text: str,
    video_id: str,
    language: str,
    requested_language: str,
    fmt: Format,
    start: float | None = None,
    end: float | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> dict[str, Any]:
    """Parse, dedup, format and paginate a VTT transcript into the response shape.

    Raises ValueError if fmt is not "paragraphs" or "segments".
    """
    if fmt not in ("paragraphs", "segments"):
        raise ValueError(f"Invalid format: {fmt!r}. Must be 'paragraphs' or 'segments'.")

    cues = parse_vtt_cues(vtt_text)
    all_segments = dedup_cues(cues)
    video_duration = max((cue.end_s for cue in cues), default=0.0)

    filtered = select_segments(all_segments, start, end)
    units = (
        build_paragraph_units(filtered, video_duration)
        if fmt == "paragraphs"
        else build_segment_units(filtered)
    )
    # paragraphs are joined by "\n"; segments are a JSON list: "[" + ", ".join + "]".
    if fmt == "paragraphs":
        selected, next_start = paginate_units(units, max_chars)
    else:
        selected, next_start = paginate_units(units, max_chars, sep_len=2, overhead=2)
    complete = next_start is None

    result: dict[str, Any] = {
        "video_id": video_id,
        "language": language,
        "requested_language": requested_language,
        "format": fmt,
    }

    if fmt == "paragraphs":
        text = "\n".join(unit.rendered for unit in selected)
        result["text"] = text
        returned_chars = len(text)
    else:
        segs = [unit.payload for unit in selected]
        result["segments"] = segs
        returned_chars = len(json.dumps(segs, ensure_ascii=False))

    range_start = selected[0].start_s if selected else None
    range_end = selected[-1].end_s if selected else None

    result["coverage"] = {
        "range_start": round(range_start, 2) if range_start is not None else None,
        "range_end": round(range_end, 2) if range_end is not None else None,
        "video_duration": round(video_duration, 2),
        "complete": complete,
        # Never round the resume token: rounding up past a segment start would drop it.
        "next_start": next_start,
    }
    result["stats"] = {
        "raw_cues": len(cues),
        "segments": len(all_segments),
        "returned_chars": returned_chars,
    }
    if not complete:
        result["note"] = (
            "Partial transcript: call get_transcript again with start=<next_start> to continue."
        )
    return result
