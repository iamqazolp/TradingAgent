"""Unit tests for Classic Pivot Points calculation."""

from __future__ import annotations

import pandas as pd
import pytest

from indicators.engine import rows_to_frame
from indicators.pivots import classic_pivots
from tests.conftest import build_rows


def test_classic_pivots_basic():
    # Build rows with close prices
    closes = [30_000.0, 31_000.0, 30_500.0, 31_200.0, 30_800.0]
    rows = build_rows(close=closes)
    frame = rows_to_frame(rows)

    piv = classic_pivots(frame)
    assert "classic" in piv
    c = piv["classic"]
    assert c["R3"] > c["R2"] > c["R1"] > c["PP"] > c["S1"] > c["S2"] > c["S3"]
    assert "position" in piv
    assert "position_description" in piv
    assert len(piv["supports"]) == 3
    assert len(piv["resistances"]) == 3


def test_classic_pivots_with_explicit_high_low():
    df = pd.DataFrame(
        {
            "close": [30_800.0],
            "prev_close": [31_400.0],
            "high": [31_800.0],
            "low": [30_600.0],
        },
        index=["2026-09-07"],
    )
    piv = classic_pivots(df)
    assert piv["is_close_only_adapted"] is False
    c = piv["classic"]
    # PP = (31800 + 30600 + 30800) / 3 = 31067
    expected_pp = round((31_800 + 30_600 + 30_800) / 3.0, 0)
    assert c["PP"] == expected_pp
