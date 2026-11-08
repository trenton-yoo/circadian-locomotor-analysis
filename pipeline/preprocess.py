"""
Preprocess locomotor Excel → metadata preview + timed counts.

Port of pipeline/preprocess.R. Does not ask for user parameters and does
not detect wake episodes.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional, Union, cast

import pandas as pd
from openpyxl import load_workbook

PathLike = Union[str, Path]
FileInput = Union[PathLike, BinaryIO, bytes]


@dataclass
class AnimalMeta:
    excel_col: int
    excel_col_letter: str
    animal_id: int
    cage_code: Optional[str]
    animal_label: Optional[str]
    column_name: str


def cell_chr(x: Any) -> Optional[str]:
    if x is None:
        return None
    if isinstance(x, float) and math.isnan(x):
        return None
    s = re.sub(r"\s+", " ", str(x)).strip()
    return s if s else None


def is_blank_chr(x: Optional[str]) -> bool:
    if x is None:
        return True
    return x.strip().lower() in {"", "na", "null", "none"}


def is_summary_label(s: Optional[str]) -> bool:
    return s is not None and s.strip().lower() in {"dark", "light", "ratio", "cage"}


def excel_serial_to_sec(num: float) -> Optional[float]:
    if num is None or (isinstance(num, float) and math.isnan(num)) or num < 0:
        return None
    return round((num % 1) * 86400) % 86400


def to_sec_of_day(x: Any) -> Optional[float]:
    """Convert Excel time / datetime / character → seconds since midnight."""
    if x is None:
        return None
    if isinstance(x, float) and math.isnan(x):
        return None

    if isinstance(x, time) and not isinstance(x, datetime):
        return float(x.hour * 3600 + x.minute * 60 + x.second)

    if isinstance(x, datetime):
        return float(x.hour * 3600 + x.minute * 60 + x.second)

    if isinstance(x, timedelta):
        return float(x.total_seconds() % 86400)

    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return excel_serial_to_sec(float(x))

    s = cell_chr(x)
    if is_blank_chr(s) or is_summary_label(s):
        return None

    assert s is not None
    try:
        num = float(s)
        if num >= 0:
            return excel_serial_to_sec(num)
    except ValueError:
        pass

    # Human-readable clock strings
    s_up = s.upper().replace("\u00a0", " ")
    for fmt in (
        "%I:%M %p",
        "%I:%M%p",
        "%I:%M:%S %p",
        "%H:%M:%S",
        "%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ):
        try:
            parsed = datetime.strptime(s_up, fmt)
            return float(parsed.hour * 3600 + parsed.minute * 60 + parsed.second)
        except ValueError:
            continue
    return None


def extract_date(x: Any, fallback: Optional[date] = None) -> date:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        if fallback is None:
            raise ValueError("No date available")
        return fallback

    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x

    if isinstance(x, (int, float)) and not isinstance(x, bool):
        num = float(x)
        # Unix seconds
        if 1e9 < num < 2e10:
            return datetime.utcfromtimestamp(num).date()
        # Excel date serial
        if 20000 < num < 100000:
            return (datetime(1899, 12, 30) + timedelta(days=num)).date()

    s = cell_chr(x)
    if is_blank_chr(s):
        if fallback is None:
            raise ValueError("No date available")
        return fallback

    assert s is not None
    try:
        return extract_date(float(s), fallback=fallback)
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue

    if fallback is None:
        raise ValueError(f"Could not parse date: {s}")
    return fallback


def fmt_clock(sec: float) -> str:
    sec_i = int(round(sec)) % 86400
    h = sec_i // 3600
    m = (sec_i % 3600) // 60
    s = sec_i % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def statistical_mode(values: List[float]) -> Optional[float]:
    vals = [v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if not vals:
        return None
    counts = Counter(vals)
    return counts.most_common(1)[0][0]


def col_to_letter(n: int) -> str:
    """1-based column index → Excel letter(s)."""
    s = ""
    x = n
    while x > 0:
        r = (x - 1) % 26
        s = chr(ord("A") + r) + s
        x = (x - 1) // 26
    return s


_AMPM_TIME_RE = re.compile(
    r"^\s*\d{1,2}:\d{2}(:\d{2})?\s*(AM|PM)\s*$",
    re.IGNORECASE,
)


def _open_workbook(source: FileInput):
    if isinstance(source, (bytes, bytearray)):
        from io import BytesIO

        return load_workbook(BytesIO(source), data_only=True, read_only=True)
    if hasattr(source, "read"):
        return load_workbook(cast(BinaryIO, source), data_only=True, read_only=True)
    return load_workbook(str(source), data_only=True, read_only=True)


def list_sheet_names(source: FileInput) -> List[str]:
    """Return workbook sheet names in order."""
    wb = _open_workbook(source)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


def _looks_like_ampm_time(x: Any) -> bool:
    s = cell_chr(x)
    return bool(s and _AMPM_TIME_RE.match(s))


def _score_column_b_times(rows: List[List[Any]], col_time: int = 1, max_scan: int = 80) -> Dict[str, Any]:
    """
    Score a sheet by how much column B looks like a time series.

    Prefers AM/PM clock strings (e.g. 7:05 PM), but also counts Excel
    serials / datetime.time values used by many locomotor exports.
    """
    ampm = 0
    other_time = 0
    has_cage = False
    sample_times: List[str] = []

    limit = min(len(rows), max_scan)
    for r in range(limit):
        row = rows[r]
        if len(row) <= col_time:
            continue
        val = row[col_time]
        s = cell_chr(val)
        if s is not None and s.lower() == "cage":
            has_cage = True
        if _looks_like_ampm_time(val):
            ampm += 1
            if len(sample_times) < 3:
                sample_times.append(cast(str, s))
            continue
        sec = to_sec_of_day(val)
        if sec is not None and not is_summary_label(s):
            other_time += 1
            if len(sample_times) < 3:
                sample_times.append(fmt_clock(sec))

    # Weight AM/PM higher because that is the user-described format.
    score = ampm * 3 + other_time + (20 if has_cage else 0)
    return {
        "ampm_time_hits": ampm,
        "other_time_hits": other_time,
        "has_cage_label": has_cage,
        "score": score,
        "likely_locomotor": score >= 5 or (has_cage and (ampm + other_time) >= 2),
        "sample_times": sample_times,
    }


def inspect_workbook_sheets(source: FileInput) -> List[Dict[str, Any]]:
    """
    Scan every sheet and report which ones look analyzable.

    Looks primarily at column B for XX:XX AM/PM times (also accepts
    Excel time serials / clock values).
    """
    wb = _open_workbook(source)
    try:
        results: List[Dict[str, Any]] = []
        for idx, name in enumerate(wb.sheetnames):
            ws = wb[name]
            rows: List[List[Any]] = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                rows.append(list(row))
                if i >= 79:
                    break
            score_info = _score_column_b_times(rows)
            results.append(
                {
                    "index": idx,
                    "name": name,
                    **score_info,
                }
            )
        # Stable sort: likely first, then higher score, then original order
        results.sort(key=lambda s: (-int(s["likely_locomotor"]), -s["score"], s["index"]))
        return results
    finally:
        wb.close()


def _load_sheet_matrix(
    source: FileInput,
    sheet: Union[int, str] = 0,
) -> List[List[Any]]:
    """Load worksheet as a 0-indexed matrix of cell values (no header assumption)."""
    wb = _open_workbook(source)

    if isinstance(sheet, int):
        ws = wb.worksheets[sheet]
    else:
        ws = wb[sheet]

    rows: List[List[Any]] = []
    for row in ws.iter_rows(values_only=True):
        rows.append(list(row))
    wb.close()
    return rows


def preprocess_locomotor(
    input_xlsx: FileInput,
    sheet: Union[int, str] = 0,
    bin_minutes: Optional[int] = None,
    start_date_fallback: date = date(2020, 1, 1),
    tz_out: str = "UTC",
    input_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Preprocess a locomotor activity Excel file.

    Returns a JSON-serializable-friendly dict with animal_meta, keep_secs,
    counts (list of column dicts / DataFrame-ready), bin_minutes, etc.
    """
    if input_name is None:
        if isinstance(input_xlsx, (str, Path)):
            input_name = Path(input_xlsx).name
        else:
            input_name = "upload.xlsx"

    raw = _load_sheet_matrix(input_xlsx, sheet=sheet)
    n_rows = len(raw)
    if n_rows < 4:
        raise ValueError("File has fewer than 4 rows; expected metadata + data.")
    n_cols = max((len(r) for r in raw), default=0)
    if n_cols < 3:
        raise ValueError("File has fewer than 3 columns; expected date/time/animals.")

    # Pad short rows
    for r in raw:
        if len(r) < n_cols:
            r.extend([None] * (n_cols - len(r)))

    col_date = 0  # 0-based
    col_time = 1

    col_b = []
    for r in range(n_rows):
        s = cell_chr(raw[r][col_time])
        col_b.append(None if s is None else s.lower())

    try:
        row_cage = next(i for i, v in enumerate(col_b) if v == "cage")
    except StopIteration as exc:
        raise ValueError('Could not find "cage" label in column B. Check the Excel layout.') from exc

    if row_cage < 1:
        raise ValueError('Could not find "cage" label in column B. Check the Excel layout.')

    row_animal = row_cage - 1
    row_label = row_cage + 1
    row_data0 = row_cage + 2
    if row_data0 >= n_rows:
        raise ValueError("No data rows found below the metadata block.")

    meta: List[AnimalMeta] = []
    for c in range(2, n_cols):  # 0-based; Excel col C = index 2
        animal_raw = cell_chr(raw[row_animal][c])
        if is_blank_chr(animal_raw):
            continue
        try:
            animal_id = int(float(cast(str, animal_raw)))
        except (TypeError, ValueError):
            continue

        cage_raw = cell_chr(raw[row_cage][c])
        if cage_raw is not None and cage_raw.lower() == "cage":
            cage_raw = None
        label_raw = cell_chr(raw[row_label][c]) if row_label < n_rows else None
        if is_blank_chr(label_raw):
            label_raw = None
        if is_blank_chr(cage_raw):
            cage_raw = None

        excel_col_1based = c + 1
        meta.append(
            AnimalMeta(
                excel_col=excel_col_1based,
                excel_col_letter=col_to_letter(excel_col_1based),
                animal_id=animal_id,
                cage_code=cage_raw,
                animal_label=label_raw,
                column_name=f"animal_{animal_id}",
            )
        )

    if not meta:
        raise ValueError(
            "No animal columns found. Expected integer animal # values in the animal row, columns C+."
        )

    keep_secs: List[float] = []
    keep_row_idx: List[int] = []
    for r in range(row_data0, n_rows):
        sec = to_sec_of_day(raw[r][col_time])
        if sec is None:
            break
        keep_secs.append(float(sec))
        keep_row_idx.append(r)

    if not keep_secs:
        raise ValueError("No parseable time values found in column B from the data start row onward.")

    pos_diffs_min = [
        round((keep_secs[i] - keep_secs[i - 1]) / 60)
        for i in range(1, len(keep_secs))
        if keep_secs[i] - keep_secs[i - 1] > 0
    ]
    detected_bin = statistical_mode([float(x) for x in pos_diffs_min])

    if bin_minutes is None:
        if detected_bin is None or detected_bin <= 0:
            raise ValueError("Could not auto-detect bin size. Set bin_minutes manually.")
        bin_minutes = int(detected_bin)
    else:
        bin_minutes = int(bin_minutes)

    try:
        suggested_start_date = extract_date(raw[row_data0][col_date], fallback=start_date_fallback)
    except ValueError:
        suggested_start_date = start_date_fallback

    counts: Dict[str, List[Optional[float]]] = {}
    for m in meta:
        c = m.excel_col - 1  # back to 0-based
        vals: List[Optional[float]] = []
        for r in keep_row_idx:
            s = cell_chr(raw[r][c])
            if is_blank_chr(s):
                vals.append(None)
            else:
                try:
                    vals.append(float(cast(str, s)))
                except ValueError:
                    vals.append(None)
        counts[m.column_name] = vals

    n_wraps = sum(
        1 for i in range(1, len(keep_secs)) if keep_secs[i] < keep_secs[i - 1]
    )

    animal_meta_dicts = [asdict(m) for m in meta]
    counts_df = pd.DataFrame(counts)

    preview_n = min(10, len(keep_secs))
    preview_df = pd.DataFrame({"time": [fmt_clock(s) for s in keep_secs[:preview_n]]})
    preview_df = pd.concat([preview_df, counts_df.iloc[:preview_n].reset_index(drop=True)], axis=1)

    sheet_label: Any = sheet
    return {
        "input_xlsx": input_name,
        "sheet": sheet_label,
        "animal_meta": animal_meta_dicts,
        "keep_secs": keep_secs,
        "counts": {k: list(v) for k, v in counts.items()},
        "bin_minutes": bin_minutes,
        "suggested_start_date": suggested_start_date.isoformat(),
        "n_bins": len(keep_secs),
        "first_clock": fmt_clock(keep_secs[0]),
        "last_clock": fmt_clock(keep_secs[-1]),
        "n_midnight_wraps": n_wraps,
        "tz_out": tz_out,
        "preview_columns": list(preview_df.columns),
        "preview_rows": preview_df.where(pd.notnull(preview_df), None).to_dict(orient="records"),
    }


def build_preview_payload(pre: Dict[str, Any]) -> Dict[str, Any]:
    """Shape preprocess result for the web workspace (matches prior R preview.json)."""
    return {
        "status": "ok",
        "input_file": pre["input_xlsx"],
        "sheet": pre.get("sheet"),
        "bin_minutes": pre["bin_minutes"],
        "suggested_start_date": pre["suggested_start_date"],
        "n_bins": pre["n_bins"],
        "first_clock": pre["first_clock"],
        "last_clock": pre["last_clock"],
        "n_midnight_wraps": pre["n_midnight_wraps"],
        "animal_meta": pre["animal_meta"],
        "preview_columns": pre["preview_columns"],
        "preview_rows": pre["preview_rows"],
    }
