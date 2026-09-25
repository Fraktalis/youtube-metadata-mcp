from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def rolling_fixture_vtt() -> str:
    return (FIXTURES_DIR / "rolling_autosub_excerpt.fr.vtt").read_text(encoding="utf-8")
