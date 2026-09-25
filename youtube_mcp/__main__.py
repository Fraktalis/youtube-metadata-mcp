"""Entry point: `python -m youtube_mcp` runs the server with uvicorn."""

from __future__ import annotations

import os

import uvicorn

from youtube_mcp.server import app


def main() -> None:
    port = int(os.environ.get("PORT", "5000"))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
