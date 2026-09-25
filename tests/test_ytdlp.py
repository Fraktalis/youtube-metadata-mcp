from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from youtube_mcp import ytdlp


def _extract_output_dir_and_video_id(cmd: list[str]) -> tuple[Path, str]:
    o_index = cmd.index("-o")
    template = cmd[o_index + 1]
    out_dir = Path(template).parent
    url = next(arg for arg in cmd if arg.startswith("https://www.youtube.com/watch?v="))
    video_id = url.rsplit("=", 1)[-1]
    return out_dir, video_id


def _make_subtitle_run(
    lang_available: set[str] | None = None, cwd_writes: list[Path] | None = None
):
    """Build a fake subprocess.run that writes a .vtt into the -o directory.

    lang_available: if given, only these --sub-lang values "succeed" (write a file).
    cwd_writes: if given, also records Path.cwd() at call time (to prove nothing
    is written there).
    """

    def fake_run(cmd, capture_output, text, timeout):
        if cwd_writes is not None:
            cwd_writes.append(Path.cwd())
        sub_lang = cmd[cmd.index("--sub-lang") + 1]
        out_dir, video_id = _extract_output_dir_and_video_id(cmd)
        if lang_available is not None and sub_lang not in lang_available:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=f"no subs for {sub_lang}")
        vtt_path = out_dir / f"{video_id}.{sub_lang}.vtt"
        vtt_path.write_text(
            f"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello in {sub_lang}\n\n", encoding="utf-8"
        )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return fake_run


# ---------------------------------------------------------------------------
# Language fallback
# ---------------------------------------------------------------------------


def test_fetch_subtitles_uses_requested_language_first():
    fake_run = _make_subtitle_run(lang_available={"en"})
    with patch("subprocess.run", side_effect=fake_run):
        vtt_text, actual_lang = ytdlp.fetch_subtitles("vid00000001", "en")
    assert actual_lang == "en"
    assert "hello in en" in vtt_text


def test_fetch_subtitles_falls_back_in_order():
    # "en" unavailable, fallback order for en is en,fr,es,de,pt,ja,ko -> fr succeeds
    fake_run = _make_subtitle_run(lang_available={"fr"})
    with patch("subprocess.run", side_effect=fake_run):
        vtt_text, actual_lang = ytdlp.fetch_subtitles("vid00000001", "en")
    assert actual_lang == "fr"


def test_fetch_subtitles_fr_fallback_order_includes_fr_orig():
    fake_run = _make_subtitle_run(lang_available={"fr-orig"})
    with patch("subprocess.run", side_effect=fake_run):
        vtt_text, actual_lang = ytdlp.fetch_subtitles("vid00000001", "fr")
    assert actual_lang == "fr-orig"


def test_fetch_subtitles_all_languages_fail_raises_with_attempted_list():
    fake_run = _make_subtitle_run(lang_available=set())
    with (
        patch("subprocess.run", side_effect=fake_run),
        pytest.raises(ytdlp.SubtitleFetchError) as exc_info,
    ):
        ytdlp.fetch_subtitles("vid00000001", "de")
    err = exc_info.value
    assert err.attempted_languages == ["de", "en", "fr", "es"]
    assert err.details is not None


# ---------------------------------------------------------------------------
# Actual language parsed from filename
# ---------------------------------------------------------------------------


def test_actual_language_parsed_from_filename():
    fake_run = _make_subtitle_run(lang_available={"pt"})
    with patch("subprocess.run", side_effect=fake_run):
        _, actual_lang = ytdlp.fetch_subtitles("vid00000001", "en")
    assert actual_lang == "pt"


# ---------------------------------------------------------------------------
# Temp dir usage / cleanup / no CWD writes
# ---------------------------------------------------------------------------


def test_fetch_subtitles_writes_to_temp_dir_not_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    written_dirs: list[Path] = []

    def fake_run(cmd, capture_output, text, timeout):
        out_dir, video_id = _extract_output_dir_and_video_id(cmd)
        written_dirs.append(out_dir)
        sub_lang = cmd[cmd.index("--sub-lang") + 1]
        (out_dir / f"{video_id}.{sub_lang}.vtt").write_text("WEBVTT\n\n", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        ytdlp.fetch_subtitles("vid00000001", "en")

    assert list(tmp_path.iterdir()) == []  # nothing left in CWD
    assert written_dirs, "yt-dlp should have been invoked"
    for d in written_dirs:
        assert not d.exists()  # temp dir cleaned up after the call


def test_fetch_subtitles_temp_dir_removed_even_on_success_first_try():
    seen_dirs: list[Path] = []

    def fake_run(cmd, capture_output, text, timeout):
        out_dir, video_id = _extract_output_dir_and_video_id(cmd)
        seen_dirs.append(out_dir)
        (out_dir / f"{video_id}.en.vtt").write_text("WEBVTT\n\n", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        ytdlp.fetch_subtitles("vid00000001", "en")

    assert len(seen_dirs) == 1
    assert not seen_dirs[0].exists()


# ---------------------------------------------------------------------------
# Concurrency: two fetches for the same video_id don't interfere
# ---------------------------------------------------------------------------


def test_concurrent_fetches_same_video_id_do_not_interfere():
    results: dict[str, tuple[str, str]] = {}
    errors: list[Exception] = []

    def fake_run(cmd, capture_output, text, timeout):
        sub_lang = cmd[cmd.index("--sub-lang") + 1]
        out_dir, video_id = _extract_output_dir_and_video_id(cmd)
        # Simulate slow I/O to widen the race window.
        vtt_path = out_dir / f"{video_id}.{sub_lang}.vtt"
        content = f"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n{sub_lang} text\n\n"
        vtt_path.write_text(content, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def worker(lang: str) -> None:
        try:
            vtt_text, actual_lang = ytdlp.fetch_subtitles("sharedvidid", lang)
            results[lang] = (vtt_text, actual_lang)
        except Exception as exc:  # pragma: no cover - failure path surfaced via errors list
            errors.append(exc)

    # Patch once, outside the threads: unittest.mock.patch's enter/exit isn't
    # thread-safe, so patching per-thread races on un-patching subprocess.run.
    with patch("subprocess.run", side_effect=fake_run):
        threads = [threading.Thread(target=worker, args=(lang,)) for lang in ("en", "fr")]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

    assert not errors
    assert results["en"][1] == "en"
    assert "en text" in results["en"][0]
    assert results["fr"][1] == "fr"
    assert "fr text" in results["fr"][0]


# ---------------------------------------------------------------------------
# Timeout handling
# ---------------------------------------------------------------------------


def test_fetch_subtitles_timeout_falls_through_all_languages():
    def fake_run(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)

    with (
        patch("subprocess.run", side_effect=fake_run),
        pytest.raises(ytdlp.SubtitleFetchError) as exc_info,
    ):
        ytdlp.fetch_subtitles("vid00000001", "en")
    assert "Timeout" in exc_info.value.details


def test_fetch_metadata_timeout():
    def fake_run(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)

    with (
        patch("subprocess.run", side_effect=fake_run),
        pytest.raises(ytdlp.MetadataFetchError) as exc_info,
    ):
        ytdlp.fetch_metadata("vid00000001")
    assert "Timeout" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Metadata parsing
# ---------------------------------------------------------------------------


def _metadata_json(**overrides) -> str:
    data = {
        "id": "vid00000001",
        "title": "A Title",
        "description": "A description",
        "upload_date": "20240102",
        "channel": "A Channel",
        "channel_id": "UC123",
        "channel_url": "https://youtube.com/channel/UC123",
        "duration": 120,
        "duration_string": "2:00",
        "view_count": 1000,
        "like_count": 50,
        "tags": ["a", "b"],
        "categories": ["Education"],
        "thumbnail": "https://example.com/thumb.jpg",
        "webpage_url": "https://www.youtube.com/watch?v=vid00000001",
    }
    data.update(overrides)
    return json.dumps(data)


def test_fetch_metadata_success_and_upload_date_formatting():
    def fake_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout=_metadata_json(), stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        metadata = ytdlp.fetch_metadata("vid00000001")

    assert metadata["id"] == "vid00000001"
    assert metadata["upload_date"] == "2024-01-02"
    assert metadata["channel"] == "A Channel"
    assert metadata["tags"] == ["a", "b"]


def test_fetch_metadata_falls_back_to_uploader_when_no_channel():
    def fake_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(
            cmd, 0, stdout=_metadata_json(channel=None, uploader="Uploader Name"), stderr=""
        )

    with patch("subprocess.run", side_effect=fake_run):
        metadata = ytdlp.fetch_metadata("vid00000001")
    assert metadata["channel"] == "Uploader Name"


# ---------------------------------------------------------------------------
# Error shapes
# ---------------------------------------------------------------------------


def test_fetch_metadata_nonzero_returncode_raises():
    def fake_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="video unavailable")

    with (
        patch("subprocess.run", side_effect=fake_run),
        pytest.raises(ytdlp.MetadataFetchError) as exc_info,
    ):
        ytdlp.fetch_metadata("vid00000001")
    assert exc_info.value.details == "video unavailable"


def test_fetch_metadata_invalid_json_raises():
    def fake_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout="not json", stderr="")

    with patch("subprocess.run", side_effect=fake_run), pytest.raises(ytdlp.MetadataFetchError):
        ytdlp.fetch_metadata("vid00000001")
