"""Shared test helpers: a synthetic rolling-auto-sub VTT generator.

Mirrors the pattern found in the real fixture
(tests/fixtures/rolling_autosub_excerpt.fr.vtt): each new spoken line first
appears paired with the previous line in a long two-line cue, then gets a
lone 10ms "hold" cue once the previous line drops off, before the next line
arrives.
"""

from __future__ import annotations


def _format_timestamp(seconds: float) -> str:
    total_ms = round(seconds * 1000)
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def generate_rolling_vtt(
    num_lines: int,
    seconds_per_line: float = 2.0,
    hold: float = 0.01,
    start_offset: float = 1.75,
    language: str = "en",
) -> tuple[str, list[str]]:
    """Build a synthetic rolling-auto-sub VTT of arbitrary duration.

    Returns (vtt_text, lines) where `lines` is the list of distinct spoken
    lines the generated VTT encodes (what a correct dedup should recover,
    in order).
    """
    if num_lines < 1:
        raise ValueError("num_lines must be >= 1")

    lines = [f"line number {i:06d} some words of dummy speech content" for i in range(num_lines)]

    cues: list[tuple[float, float, str]] = []
    t = start_offset
    cues.append((t, t + hold, lines[0]))
    prev_line = lines[0]
    cur_t = t + hold

    for line in lines[1:]:
        end_t = cur_t + (seconds_per_line - hold)
        cues.append((cur_t, end_t, f"{prev_line}\n{line}"))
        hold_start, hold_end = end_t, end_t + hold
        cues.append((hold_start, hold_end, line))
        prev_line = line
        cur_t = hold_end

    body_lines = ["WEBVTT", "Kind: captions", f"Language: {language}", ""]
    for start, end, text in cues:
        timing = f"{_format_timestamp(start)} --> {_format_timestamp(end)} align:start position:0%"
        body_lines.append(timing)
        body_lines.append(text)
        body_lines.append("")

    return "\n".join(body_lines), lines
