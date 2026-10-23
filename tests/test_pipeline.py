"""Unit / equivalence tests for the Python locomotor pipeline."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from pipeline.analyze import analyze_locomotor, get_period
from pipeline.preprocess import fmt_clock, preprocess_locomotor, to_sec_of_day

ROOT = Path(__file__).resolve().parents[1]
SAMPLE2 = ROOT / "sample2.xlsx"


@pytest.mark.skipif(not SAMPLE2.exists(), reason="sample2.xlsx not present")
def test_preprocess_sample2_shape():
    pre = preprocess_locomotor(SAMPLE2)
    assert pre["n_bins"] == 288
    assert pre["bin_minutes"] == 5
    assert pre["first_clock"] == "19:05:00"
    assert pre["last_clock"] == "19:00:00"
    assert pre["n_midnight_wraps"] == 1
    assert len(pre["animal_meta"]) == 8
    assert pre["suggested_start_date"] == "2025-02-12"


@pytest.mark.skipif(not SAMPLE2.exists(), reason="sample2.xlsx not present")
def test_analyze_sample2_episode_counts():
    pre = preprocess_locomotor(SAMPLE2)
    result = analyze_locomotor(
        pre,
        start_date="2025-02-12",
        thresholds=100,
        min_episode_minutes=10,
    )
    assert result["df_long"].groupby("animal_id").size().min() == 288
    # Known reference from R pipeline on sample2 (threshold=100, min=10)
    assert result["n_episodes_before_filter"] == 222
    assert result["n_episodes_after_filter"] == 139
    assert (result["episodes_all"]["duration_minutes"] >= 10).all()
    assert result["xlsx_bytes"] is not None
    assert len(result["summary_records"]) == 16  # 8 animals × 2 periods


def test_excel_serial_time_not_clock_hm():
    # Classic bug: "1.25" must be Excel day-fraction, not 01:25
    assert to_sec_of_day("1.25") == pytest.approx(0.25 * 86400, abs=1)
    assert fmt_clock(to_sec_of_day("0.7951388888888888")) == "19:05:00"


def test_get_period_boundaries():
    from datetime import datetime

    assert get_period(datetime(2025, 2, 12, 19, 5)) == "Day"
    assert get_period(datetime(2025, 2, 12, 19, 40)) == "Night"
    assert get_period(datetime(2025, 2, 13, 0, 0)) == "Night"
    assert get_period(datetime(2025, 2, 13, 7, 35)) == "Day"


@pytest.mark.skipif(not SAMPLE2.exists(), reason="sample2.xlsx not present")
def test_midnight_rollover():
    pre = preprocess_locomotor(SAMPLE2)
    result = analyze_locomotor(pre, start_date="2025-02-12", thresholds=100, min_episode_minutes=10)
    times = list(result["final_full"]["time_fixed"])
    # Find first midnight crossing
    cross = next(
        i
        for i in range(1, len(times))
        if times[i].date() > times[i - 1].date()
    )
    assert times[cross - 1].strftime("%H:%M") == "23:55"
    assert times[cross].strftime("%Y-%m-%d %H:%M") == "2025-02-13 00:00"
