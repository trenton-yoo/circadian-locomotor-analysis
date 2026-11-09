"""
Confirmed params + preprocessed data → wake episodes Excel / summary.

Port of pipeline/analyze.R.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

import numpy as np
import pandas as pd


def get_period(dt: datetime) -> Optional[str]:
    clock_min = dt.hour * 60 + dt.minute

    day1_start = 19 * 60 + 5
    day1_end = 19 * 60 + 35
    night_start = 19 * 60 + 40
    night_end = 7 * 60 + 30
    day2_start = 7 * 60 + 35
    day2_end = 19 * 60

    if day1_start <= clock_min <= day1_end:
        return "Day"
    if clock_min >= night_start or clock_min <= night_end:
        return "Night"
    if day2_start <= clock_min <= day2_end:
        return "Day"
    return None


def _build_time_fixed(
    keep_secs: Sequence[float],
    start_date: date,
) -> List[datetime]:
    times: List[datetime] = []
    day_offset = 0
    prev: Optional[float] = None
    base = datetime.combine(start_date, datetime.min.time())
    for sec in keep_secs:
        if prev is not None and sec < prev:
            day_offset += 1
        times.append(base + timedelta(days=day_offset, seconds=float(sec)))
        prev = sec
    return times


def detect_episodes(
    df: pd.DataFrame,
    threshold: float,
    bin_minutes: int,
) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(
            columns=[
                "animal",
                "animal_id",
                "cage_code",
                "animal_label",
                "period",
                "start_time",
                "end_time",
                "duration_bins",
                "duration_minutes",
                "total_counts",
            ]
        )

    d = df.sort_values("time_fixed").copy()
    d["active"] = d["counts"] >= threshold
    active = d["active"].to_numpy()
    period = d["period"].astype(object).to_numpy()

    n = len(d)
    new_run = np.ones(n, dtype=bool)
    if n > 1:
        new_run[1:] = (active[1:] != active[:-1]) | (period[1:] != period[:-1])
    d["run_id"] = np.cumsum(new_run)

    rows = []
    for _, g in d.groupby("run_id", sort=True):
        if not bool(g["active"].iloc[0]):
            continue
        rows.append(
            {
                "animal": g["animal"].iloc[0],
                "animal_id": int(g["animal_id"].iloc[0]),
                "cage_code": g["cage_code"].iloc[0],
                "animal_label": g["animal_label"].iloc[0],
                "period": g["period"].iloc[0],
                "start_time": g["time_fixed"].iloc[0],
                "end_time": g["time_fixed"].iloc[-1],
                "duration_bins": int(len(g)),
                "duration_minutes": int(len(g) * bin_minutes),
                "total_counts": float(g["counts"].fillna(0).sum()),
            }
        )
    return pd.DataFrame(rows)


def _parse_start_date(start_date: Union[str, date, datetime]) -> date:
    if isinstance(start_date, datetime):
        return start_date.date()
    if isinstance(start_date, date):
        return start_date
    s = str(start_date).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"`start_date` is missing or invalid: {start_date}")


def analyze_locomotor(
    preprocessed: Dict[str, Any],
    start_date: Union[str, date, datetime],
    thresholds: Union[float, Sequence[float]] = 100,
    min_episode_minutes: float = 10,
    animal_meta: Optional[List[Dict[str, Any]]] = None,
    bin_minutes: Optional[int] = None,
    tz_out: Optional[str] = None,
    output_xlsx: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """
    Analyze preprocessed locomotor data with confirmed parameters.

    If output_xlsx is provided, writes a multi-sheet workbook.
    Always returns structured results including summary tables and
    optional Excel bytes under key ``xlsx_bytes``.
    """
    if isinstance(thresholds, (int, float)):
        thresh_list = [float(thresholds)]
    else:
        thresh_list = [float(t) for t in thresholds]
    if not thresh_list or any(math.isnan(t) for t in thresh_list):
        raise ValueError("`thresholds` must be one or more numbers.")

    min_episode_minutes = float(min_episode_minutes)
    if math.isnan(min_episode_minutes) or min_episode_minutes < 0:
        raise ValueError("`min_episode_minutes` must be a non-negative number.")

    if bin_minutes is None:
        bin_minutes = int(preprocessed["bin_minutes"])
    else:
        bin_minutes = int(bin_minutes)

    start = _parse_start_date(start_date)
    meta = animal_meta if animal_meta is not None else list(preprocessed["animal_meta"])
    keep_secs = list(preprocessed["keep_secs"])
    counts = pd.DataFrame(preprocessed["counts"])

    time_fixed = _build_time_fixed(keep_secs, start)
    final_full = pd.concat(
        [pd.DataFrame({"time_fixed": time_fixed}), counts.reset_index(drop=True)],
        axis=1,
    )
    final_full["period"] = [get_period(t) for t in final_full["time_fixed"]]

    animal_cols = [c for c in final_full.columns if str(c).startswith("animal_")]
    df_long = final_full.melt(
        id_vars=["time_fixed", "period"],
        value_vars=animal_cols,
        var_name="animal",
        value_name="counts",
    )
    meta_df = pd.DataFrame(meta)[
        ["column_name", "animal_id", "cage_code", "animal_label"]
    ]
    df_long = df_long.merge(meta_df, left_on="animal", right_on="column_name", how="left")
    df_long = df_long.drop(columns=["column_name"])
    df_long = df_long.sort_values(["animal_id", "time_fixed"]).reset_index(drop=True)

    episode_frames = []
    for thresh in thresh_list:
        parts = []
        for _, sub in df_long.groupby("animal", sort=False):
            parts.append(detect_episodes(sub, threshold=thresh, bin_minutes=bin_minutes))
        eps = pd.concat(parts, ignore_index=True) if parts else detect_episodes(
            df_long.iloc[0:0], thresh, bin_minutes
        )
        if not eps.empty:
            eps["threshold"] = thresh
        episode_frames.append(eps)

    episodes_all = (
        pd.concat(episode_frames, ignore_index=True)
        if episode_frames
        else pd.DataFrame()
    )

    n_before = len(episodes_all)
    if not episodes_all.empty:
        episodes_all = episodes_all[
            episodes_all["duration_minutes"] >= min_episode_minutes
        ].copy()
    n_after = len(episodes_all)

    if not episodes_all.empty:
        episodes_all = episodes_all.sort_values(
            ["animal_id", "period", "threshold", "start_time"]
        )
        episodes_all["episode_id"] = (
            episodes_all.groupby(["animal_id", "period", "threshold"]).cumcount() + 1
        )
        episodes_all = episodes_all.sort_values(
            ["animal_id", "threshold", "start_time"]
        ).reset_index(drop=True)

    if episodes_all.empty:
        episodes_summary = pd.DataFrame(
            columns=[
                "animal",
                "animal_id",
                "cage_code",
                "animal_label",
                "period",
                "threshold",
                "total_wake_episodes",
                "total_wake_minutes",
                "avg_episode_length",
            ]
        )
    else:
        episodes_summary = (
            episodes_all.groupby(
                ["animal", "animal_id", "cage_code", "animal_label", "period", "threshold"],
                dropna=False,
            )
            .agg(
                total_wake_episodes=("duration_minutes", "size"),
                total_wake_minutes=("duration_minutes", "sum"),
                avg_episode_length=("duration_minutes", "mean"),
            )
            .reset_index()
        )

    animals = sorted(df_long["animal"].unique().tolist())
    periods = ["Day", "Night"]
    animal_lookup = df_long[
        ["animal", "animal_id", "cage_code", "animal_label"]
    ].drop_duplicates()

    grid = pd.MultiIndex.from_product(
        [animals, periods, thresh_list], names=["animal", "period", "threshold"]
    ).to_frame(index=False)
    grid = grid.merge(animal_lookup, on="animal", how="left")
    summary_cols = [
        "animal",
        "period",
        "threshold",
        "total_wake_episodes",
        "total_wake_minutes",
        "avg_episode_length",
    ]
    if episodes_summary.empty:
        join_right = pd.DataFrame(columns=summary_cols)
    else:
        join_right = episodes_summary[summary_cols]
    episodes_summary = grid.merge(
        join_right,
        on=["animal", "period", "threshold"],
        how="left",
    )
    for col in ("total_wake_episodes", "total_wake_minutes", "avg_episode_length"):
        episodes_summary[col] = episodes_summary[col].fillna(0)
    episodes_summary = episodes_summary.sort_values(
        ["animal_id", "period", "threshold"]
    ).reset_index(drop=True)

    overall_mean_lengths = (
        episodes_summary.groupby(["threshold", "period"], dropna=False)
        .agg(
            mean_avg_episode_length=("avg_episode_length", "mean"),
            sd_avg_episode_length=("avg_episode_length", "std"),
        )
        .reset_index()
    )
    counts_n = episodes_summary.groupby(["threshold", "period"]).size().rename("n")
    overall_mean_lengths = overall_mean_lengths.merge(
        counts_n, on=["threshold", "period"], how="left"
    )
    overall_mean_lengths["se_avg_episode_length"] = (
        overall_mean_lengths["sd_avg_episode_length"]
        / np.sqrt(overall_mean_lengths["n"].clip(lower=1))
    )
    overall_mean_lengths = overall_mean_lengths.drop(columns=["n"]).sort_values(
        ["threshold", "period"]
    )

    meta_out = pd.DataFrame(meta)[
        ["excel_col_letter", "animal_id", "cage_code", "animal_label"]
    ]

    xlsx_bytes: Optional[bytes] = None
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
        meta_out.to_excel(writer, sheet_name="animal_metadata", index=False)
        (
            episodes_all
            if not episodes_all.empty
            else pd.DataFrame(
                columns=[
                    "animal",
                    "animal_id",
                    "cage_code",
                    "animal_label",
                    "period",
                    "start_time",
                    "end_time",
                    "duration_bins",
                    "duration_minutes",
                    "total_counts",
                    "threshold",
                    "episode_id",
                ]
            )
        ).to_excel(writer, sheet_name="episodes", index=False)
        episodes_summary.to_excel(writer, sheet_name="summary", index=False)
        overall_mean_lengths.to_excel(writer, sheet_name="overall_means", index=False)
    xlsx_bytes = buffer.getvalue()

    if output_xlsx is not None:
        Path(output_xlsx).write_bytes(xlsx_bytes)

    summary_records = []
    for _, row in episodes_summary.iterrows():
        summary_records.append(
            {
                "animal_id": int(row["animal_id"]) if pd.notna(row["animal_id"]) else None,
                "cage_code": "" if pd.isna(row["cage_code"]) else str(row["cage_code"]),
                "animal_label": ""
                if pd.isna(row["animal_label"])
                else str(row["animal_label"]),
                "period": row["period"],
                "threshold": float(row["threshold"]),
                "total_wake_episodes": int(row["total_wake_episodes"]),
                "total_wake_minutes": float(row["total_wake_minutes"]),
                "avg_episode_length": round(float(row["avg_episode_length"]), 2),
            }
        )

    episode_records = []
    if not episodes_all.empty:
        for _, row in episodes_all.iterrows():
            episode_records.append(
                {
                    "animal_id": int(row["animal_id"]),
                    "cage_code": "" if pd.isna(row["cage_code"]) else str(row["cage_code"]),
                    "animal_label": ""
                    if pd.isna(row["animal_label"])
                    else str(row["animal_label"]),
                    "period": row["period"],
                    "threshold": float(row["threshold"]),
                    "episode_id": int(row["episode_id"]),
                    "start_time": pd.Timestamp(row["start_time"]).isoformat(sep=" "),
                    "end_time": pd.Timestamp(row["end_time"]).isoformat(sep=" "),
                    "duration_bins": int(row["duration_bins"]),
                    "duration_minutes": float(row["duration_minutes"]),
                    "total_counts": float(row["total_counts"]),
                }
            )

    # Separate workbooks for UI downloads
    summary_buf = BytesIO()
    with pd.ExcelWriter(summary_buf, engine="xlsxwriter") as writer:
        meta_out.to_excel(writer, sheet_name="animal_metadata", index=False)
        episodes_summary.to_excel(writer, sheet_name="summary", index=False)
        overall_mean_lengths.to_excel(writer, sheet_name="overall_means", index=False)
    summary_xlsx_bytes = summary_buf.getvalue()

    # Download-only table: simplified columns, Day then Night per animal.
    export_cols = [
        "animal",
        "cage_code",
        "animal_label",
        "period",
        "duration_minutes",
        "threshold",
    ]
    if episodes_all.empty:
        episodes_export = pd.DataFrame(columns=export_cols)
    else:
        episodes_export = episodes_all.copy()
        period_order = {"Day": 0, "Night": 1}
        episodes_export["_period_ord"] = episodes_export["period"].map(period_order)
        episodes_export = (
            episodes_export.sort_values(
                ["animal_id", "_period_ord", "threshold", "start_time"],
                kind="mergesort",
            )
            .loc[:, export_cols]
            .reset_index(drop=True)
        )

    episodes_buf = BytesIO()
    with pd.ExcelWriter(episodes_buf, engine="xlsxwriter") as writer:
        episodes_export.to_excel(writer, sheet_name="episodes", index=False)
    episodes_xlsx_bytes = episodes_buf.getvalue()

    return {
        "final_full": final_full,
        "df_long": df_long,
        "episodes_all": episodes_all,
        "episodes_summary": episodes_summary,
        "overall_mean_lengths": overall_mean_lengths,
        "animal_meta": meta,
        "output_xlsx": str(output_xlsx) if output_xlsx else None,
        "xlsx_bytes": xlsx_bytes,
        "summary_xlsx_bytes": summary_xlsx_bytes,
        "episodes_xlsx_bytes": episodes_xlsx_bytes,
        "thresholds": thresh_list,
        "min_episode_minutes": min_episode_minutes,
        "bin_minutes": bin_minutes,
        "start_date": start.isoformat(),
        "n_episodes_before_filter": n_before,
        "n_episodes_after_filter": n_after,
        "summary_records": summary_records,
        "episode_records": episode_records,
    }


def build_summary_payload(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "ok",
        "bin_minutes": result["bin_minutes"],
        "thresholds": result["thresholds"],
        "min_episode_minutes": result["min_episode_minutes"],
        "start_date": result["start_date"],
        "n_episodes": result["n_episodes_after_filter"],
        "summary": result["summary_records"],
        "episodes": result["episode_records"],
    }
