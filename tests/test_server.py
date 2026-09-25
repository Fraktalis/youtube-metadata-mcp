from __future__ import annotations

from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from youtube_mcp import server
from youtube_mcp.server import app, mcp

from .helpers import generate_rolling_vtt

# ---------------------------------------------------------------------------
# MCP-level: list_tools
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_tools_includes_get_transcript_and_get_metadata():
    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}
    assert {"get_transcript", "get_metadata"} <= names


@pytest.mark.anyio
async def test_get_transcript_params_shape():
    tools = await mcp.list_tools()
    tool = next(t for t in tools if t.name == "get_transcript")
    props = tool.input_schema["properties"]
    assert set(props.keys()) == {"url", "language", "format", "start", "end", "max_chars"}
    assert tool.input_schema["required"] == ["url"]


# ---------------------------------------------------------------------------
# MCP-level: call_tool for get_transcript
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_call_get_transcript_paragraphs():
    vtt, _ = generate_rolling_vtt(num_lines=10)
    with patch.object(server.ytdlp, "fetch_subtitles", return_value=(vtt, "en")):
        result = await mcp.call_tool(
            "get_transcript", {"url": "https://youtu.be/abc12345678", "format": "paragraphs"}
        )
    assert result.is_error is False
    payload = result.structured_content
    assert payload["format"] == "paragraphs"
    assert "text" in payload
    assert payload["coverage"]["complete"] is True


@pytest.mark.anyio
async def test_call_get_transcript_segments():
    vtt, _ = generate_rolling_vtt(num_lines=10)
    with patch.object(server.ytdlp, "fetch_subtitles", return_value=(vtt, "en")):
        result = await mcp.call_tool(
            "get_transcript", {"url": "https://youtu.be/abc12345678", "format": "segments"}
        )
    payload = result.structured_content
    assert payload["format"] == "segments"
    assert isinstance(payload["segments"], list)
    assert len(payload["segments"]) == 10


@pytest.mark.anyio
async def test_call_get_transcript_pagination():
    vtt, _ = generate_rolling_vtt(num_lines=500)
    with patch.object(server.ytdlp, "fetch_subtitles", return_value=(vtt, "en")):
        result = await mcp.call_tool(
            "get_transcript",
            {"url": "https://youtu.be/abc12345678", "format": "segments", "max_chars": 2000},
        )
    payload = result.structured_content
    assert payload["coverage"]["complete"] is False
    assert payload["coverage"]["next_start"] is not None
    assert "note" in payload


@pytest.mark.anyio
async def test_call_get_transcript_invalid_url():
    result = await mcp.call_tool("get_transcript", {"url": "not-a-url-at-all"})
    payload = result.structured_content
    assert payload["error"] == "Invalid YouTube URL"


@pytest.mark.anyio
async def test_call_get_transcript_invalid_format():
    # format is a Literal, so the SDK rejects bad values before the tool body runs
    # and the JSON schema advertises the enum to the LLM.
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="paragraphs"):
        await mcp.call_tool(
            "get_transcript", {"url": "https://youtu.be/abc12345678", "format": "xml"}
        )


@pytest.mark.anyio
async def test_get_transcript_schema_enumerates_formats():
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    fmt = tools["get_transcript"].input_schema["properties"]["format"]
    assert fmt["enum"] == ["paragraphs", "segments"]


@pytest.mark.anyio
async def test_call_get_transcript_fetch_failure_shape():
    from youtube_mcp import ytdlp

    err = ytdlp.SubtitleFetchError(
        "boom", details="no subs anywhere", attempted_languages=["en", "fr"]
    )
    with patch.object(server.ytdlp, "fetch_subtitles", side_effect=err):
        result = await mcp.call_tool("get_transcript", {"url": "https://youtu.be/abc12345678"})
    payload = result.structured_content
    assert payload["error"] == "boom"
    assert payload["details"] == "no subs anywhere"
    assert payload["attempted_languages"] == ["en", "fr"]


# ---------------------------------------------------------------------------
# MCP-level: call_tool for get_metadata
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_call_get_metadata_success():
    metadata = {"id": "abc12345678", "title": "T", "upload_date": "2024-01-02"}
    with patch.object(server.ytdlp, "fetch_metadata", return_value=metadata):
        result = await mcp.call_tool("get_metadata", {"url": "https://youtu.be/abc12345678"})
    assert result.structured_content == metadata


@pytest.mark.anyio
async def test_call_get_metadata_invalid_url():
    result = await mcp.call_tool("get_metadata", {"url": "nope"})
    assert result.structured_content["error"] == "Invalid YouTube URL"


@pytest.mark.anyio
async def test_call_get_metadata_fetch_failure():
    from youtube_mcp import ytdlp

    err = ytdlp.MetadataFetchError("Failed to fetch metadata", details="video unavailable")
    with patch.object(server.ytdlp, "fetch_metadata", side_effect=err):
        result = await mcp.call_tool("get_metadata", {"url": "https://youtu.be/abc12345678"})
    payload = result.structured_content
    assert payload["error"] == "Failed to fetch metadata"
    assert payload["details"] == "video unavailable"


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------------------
# HTTP-level: Starlette TestClient
# ---------------------------------------------------------------------------


def test_health_endpoint():
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_legacy_root_post_route_removed():
    client = TestClient(app)
    resp = client.post("/", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert resp.status_code in (404, 405)


def test_legacy_test_transcript_route_removed():
    client = TestClient(app)
    resp = client.get("/test_transcript", params={"videoId": "abc12345678"})
    assert resp.status_code in (404, 405)


def test_legacy_test_metadata_route_removed():
    client = TestClient(app)
    resp = client.get("/test_metadata", params={"videoId": "abc12345678"})
    assert resp.status_code in (404, 405)
