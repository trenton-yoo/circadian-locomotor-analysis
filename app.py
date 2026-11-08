"""
Bare-bones web UI for locomotor analysis (Python pipeline).
Flow: upload xlsx → preprocess preview → set params → analyze → summary + download

Designed to run locally and on serverless hosts (e.g. Vercel):
- No R / Rscript dependency
- Job scratch files under the system temp directory (/tmp on Vercel)
- Preprocessed state also embedded in the confirm form so analyze works
  even if /tmp is not sticky across invocations
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import tempfile
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from pipeline.analyze import analyze_locomotor, build_summary_payload
from pipeline.plots import animal_options_from_results, build_relative_frequency_histograms
from pipeline.preprocess import (
    build_preview_payload,
    inspect_workbook_sheets,
    preprocess_locomotor,
)

ROOT = Path(__file__).resolve().parent

# Prefer /tmp (or OS temp) so Vercel's read-only project filesystem is avoided.
JOBS_DIR = Path(os.environ.get("LOCO_JOBS_DIR", tempfile.gettempdir())) / "loco_jobs"

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")
# Vercel request body limit is ~4.5 MB; keep a local ceiling too.
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", "4")) * 1024 * 1024


def ensure_dirs() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)


def job_path(job_id: str) -> Path:
    return JOBS_DIR / job_id


def save_json(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"status": "error", "message": f"Missing file: {path.name}"}
    return json.loads(path.read_text(encoding="utf-8"))


def encode_preprocessed(pre: Dict[str, Any]) -> str:
    """Gzip + base64 so confirm/analyze can restore state without sticky disk."""
    slim = {
        "input_xlsx": pre["input_xlsx"],
        "sheet": pre.get("sheet"),
        "animal_meta": pre["animal_meta"],
        "keep_secs": pre["keep_secs"],
        "counts": pre["counts"],
        "bin_minutes": pre["bin_minutes"],
        "suggested_start_date": pre["suggested_start_date"],
        "n_bins": pre["n_bins"],
        "first_clock": pre["first_clock"],
        "last_clock": pre["last_clock"],
        "n_midnight_wraps": pre["n_midnight_wraps"],
        "tz_out": pre["tz_out"],
        "preview_columns": pre["preview_columns"],
        "preview_rows": pre["preview_rows"],
    }
    raw = json.dumps(slim, separators=(",", ":"), default=str).encode("utf-8")
    return base64.b64encode(gzip.compress(raw, compresslevel=6)).decode("ascii")


def decode_preprocessed(token: str) -> Dict[str, Any]:
    raw = gzip.decompress(base64.b64decode(token.encode("ascii")))
    return json.loads(raw.decode("utf-8"))


def load_preprocessed(job_id: str, form_token: Optional[str] = None) -> Dict[str, Any]:
    path = job_path(job_id) / "preprocessed.json"
    if path.exists():
        return load_json(path)
    if form_token:
        return decode_preprocessed(form_token)
    raise FileNotFoundError(
        "Preprocessed data not found. On serverless hosts the temp job may have "
        "expired — please upload the file again."
    )


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


def _run_preprocess_for_job(
    job_id: str,
    file_bytes: bytes,
    filename: str,
    sheet: Any,
) -> None:
    """Preprocess one sheet and write preview artifacts into the job folder."""
    jdir = job_path(job_id)
    pre = preprocess_locomotor(
        file_bytes,
        sheet=sheet,
        bin_minutes=None,
        input_name=filename,
    )
    preview = build_preview_payload(pre)
    save_json(jdir / "preprocessed.json", pre)
    save_json(jdir / "preview.json", preview)
    token = encode_preprocessed(pre)
    (jdir / "preprocessed.b64.txt").write_text(token, encoding="utf-8")


@app.route("/upload", methods=["POST"])
def upload():
    ensure_dirs()
    f = request.files.get("xlsx_file")
    if f is None or not f.filename:
        flash("Please choose an .xlsx file.")
        return redirect(url_for("index"))

    filename = Path(f.filename).name
    if not filename.lower().endswith(".xlsx"):
        flash("File must be .xlsx")
        return redirect(url_for("index"))

    job_id = uuid.uuid4().hex[:12]
    jdir = job_path(job_id)
    jdir.mkdir(parents=True, exist_ok=True)

    file_bytes = f.read()
    if not file_bytes:
        return render_template(
            "workspace.html",
            job_id=job_id,
            error="Uploaded file was empty.",
            preview=None,
            results=None,
            phase="preview",
            view="summary",
            preprocessed_b64="",
        )

    (jdir / "input.xlsx").write_bytes(file_bytes)
    save_json(jdir / "meta.json", {"input_file": filename})

    try:
        sheets = inspect_workbook_sheets(file_bytes)
        save_json(jdir / "sheets.json", {"sheets": sheets, "input_file": filename})
    except Exception as exc:  # noqa: BLE001
        message = f"{type(exc).__name__}: {exc}"
        return render_template(
            "workspace.html",
            job_id=job_id,
            error=message,
            preview=None,
            results=None,
            phase="preview",
            view="summary",
            preprocessed_b64="",
        )

    # One sheet → preprocess immediately. Multiple → let user choose.
    if len(sheets) <= 1:
        sheet_ref: Any = sheets[0]["name"] if sheets else 0
        try:
            _run_preprocess_for_job(job_id, file_bytes, filename, sheet_ref)
        except Exception as exc:  # noqa: BLE001
            message = f"{type(exc).__name__}: {exc}"
            if os.environ.get("FLASK_DEBUG") or os.environ.get("VERCEL_ENV") == "development":
                message = message + "\n\n" + traceback.format_exc()
            return render_template(
                "workspace.html",
                job_id=job_id,
                error=message,
                preview=None,
                results=None,
                phase="preview",
                view="summary",
                preprocessed_b64="",
            )
        return redirect(url_for("workspace", job_id=job_id))

    return redirect(url_for("select_sheet", job_id=job_id))


@app.route("/job/<job_id>/select-sheet", methods=["GET", "POST"])
def select_sheet(job_id: str):
    ensure_dirs()
    jdir = job_path(job_id)
    input_path = jdir / "input.xlsx"
    sheets_path = jdir / "sheets.json"

    if not input_path.exists():
        flash("Upload not found or expired. Please upload again.")
        return redirect(url_for("index"))

    sheets_info = load_json(sheets_path) if sheets_path.exists() else {}
    sheets = sheets_info.get("sheets") or inspect_workbook_sheets(input_path.read_bytes())
    filename = sheets_info.get("input_file") or (load_json(jdir / "meta.json").get("input_file") if (jdir / "meta.json").exists() else "upload.xlsx")

    if request.method == "GET":
        return render_template(
            "select_sheet.html",
            job_id=job_id,
            input_file=filename,
            sheets=sheets,
            error=None,
        )

    sheet_name = request.form.get("sheet_name", "").strip()
    if not sheet_name:
        return render_template(
            "select_sheet.html",
            job_id=job_id,
            input_file=filename,
            sheets=sheets,
            error="Please choose a sheet.",
        )

    try:
        file_bytes = input_path.read_bytes()
        _run_preprocess_for_job(job_id, file_bytes, filename, sheet_name)
    except Exception as exc:  # noqa: BLE001
        message = f"{type(exc).__name__}: {exc}"
        return render_template(
            "select_sheet.html",
            job_id=job_id,
            input_file=filename,
            sheets=sheets,
            error=message,
        )

    return redirect(url_for("workspace", job_id=job_id))


def _histogram_context_from_request(results: Dict[str, Any]) -> Dict[str, Any]:
    """Build histogram plot + form defaults from GET query or POST form."""
    src = request.form if request.method == "POST" else request.args
    animals = animal_options_from_results(results)
    lookup = {
        int(a["animal_id"]): (a.get("animal_label") or "")
        for a in animals
    }

    period1 = src.get("period1", "All")
    if period1 not in {"Day", "Night", "All"}:
        period1 = "All"
    show_mean1 = src.get("show_mean1") in {"1", "on", "true", "True"}
    show_second = src.get("show_second") in {"1", "on", "true", "True"}

    # Animal checkboxes use animal1_<id> / animal2_<id>
    ids1 = []
    ids2 = []
    for key in src:
        if key.startswith("animal1_"):
            try:
                ids1.append(int(key.split("_", 1)[1]))
            except ValueError:
                pass
        elif key.startswith("animal2_"):
            try:
                ids2.append(int(key.split("_", 1)[1]))
            except ValueError:
                pass

    # Default: all animals selected when first opening the tab (no animal params).
    has_animal_params = any(k.startswith("animal1_") for k in src)
    if not has_animal_params:
        ids1 = [int(a["animal_id"]) for a in animals]

    period2 = src.get("period2", "All")
    if period2 not in {"Day", "Night", "All"}:
        period2 = "All"
    show_mean2 = src.get("show_mean2") in {"1", "on", "true", "True"}

    panels = [
        {
            "animal_ids": ids1 or None,
            "period": period1,
            "show_mean": show_mean1,
        }
    ]
    if show_second:
        panels.append(
            {
                "animal_ids": ids2 or None,
                "period": period2,
                "show_mean": show_mean2,
            }
        )

    bin_minutes = results.get("bin_minutes")
    try:
        plot_b64, panel_stats = build_relative_frequency_histograms(
            results.get("episodes") or [],
            panels=panels,
            bin_minutes=bin_minutes,
            animal_lookup=lookup,
        )
        hist_error = None
    except Exception as exc:  # noqa: BLE001
        plot_b64 = None
        panel_stats = []
        hist_error = f"{type(exc).__name__}: {exc}"

    return {
        "animals": animals,
        "selected1": set(ids1),
        "selected2": set(ids2),
        "period1": period1,
        "period2": period2,
        "show_mean1": show_mean1,
        "show_mean2": show_mean2,
        "show_second": show_second,
        "plot_b64": plot_b64,
        "panel_stats": panel_stats,
        "hist_error": hist_error,
    }


@app.route("/job/<job_id>", methods=["GET", "POST"])
def workspace(job_id: str):
    jdir = job_path(job_id)
    preview_path = jdir / "preview.json"
    token_path = jdir / "preprocessed.b64.txt"

    preview = load_json(preview_path) if preview_path.exists() else None
    token = token_path.read_text(encoding="utf-8") if token_path.exists() else ""

    # Phase is URL-only (not persisted). Default = preview. Analysis view
    # only when explicitly requested after a successful run.
    phase = request.values.get("phase", "preview")
    view = request.values.get("view", "summary")
    if view not in {"summary", "episodes", "histogram"}:
        view = "summary"

    results_path = jdir / "results.json"
    results = load_json(results_path) if results_path.exists() else None

    error = None
    if preview is None:
        error = "Job not found or expired. Please upload the file again."
    elif preview.get("status") != "ok":
        error = preview.get("message", "Preprocess error")
        preview = None

    hist = None
    if phase == "analysis":
        if results is None or results.get("status") != "ok":
            # No saved analysis UI state required — send user back to preview.
            return redirect(url_for("workspace", job_id=job_id))
        if view == "histogram":
            hist = _histogram_context_from_request(results)
    else:
        phase = "preview"
        results = None

    return render_template(
        "workspace.html",
        job_id=job_id,
        preview=preview,
        results=results,
        phase=phase,
        view=view,
        error=error,
        preprocessed_b64=token,
        hist=hist,
    )


@app.route("/job/<job_id>/analyze", methods=["POST"])
def analyze(job_id: str):
    ensure_dirs()
    jdir = job_path(job_id)
    jdir.mkdir(parents=True, exist_ok=True)

    form_token = request.form.get("preprocessed_b64", "")
    try:
        pre = load_preprocessed(job_id, form_token=form_token or None)
        # Refresh disk cache when restored from form (helps subsequent download).
        save_json(jdir / "preprocessed.json", pre)
        preview = build_preview_payload(pre)
        save_json(jdir / "preview.json", preview)
        if form_token:
            (jdir / "preprocessed.b64.txt").write_text(form_token, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return render_template(
            "workspace.html",
            job_id=job_id,
            error=str(exc),
            preview=None,
            results=None,
            phase="preview",
            view="summary",
            preprocessed_b64=form_token,
        )

    try:
        threshold = float(request.form.get("threshold", "100"))
        min_episode_minutes = float(request.form.get("min_episode_minutes", "10"))
        start_date = request.form.get("start_date", pre.get("suggested_start_date"))
    except ValueError:
        return render_template(
            "workspace.html",
            job_id=job_id,
            error="Threshold and minimum episode length must be numbers.",
            preview=preview,
            results=None,
            phase="preview",
            view="summary",
            preprocessed_b64=form_token or encode_preprocessed(pre),
        )

    labels_by_id = {}
    for animal in pre.get("animal_meta", []):
        aid = animal["animal_id"]
        labels_by_id[aid] = request.form.get(
            f"label_{aid}", animal.get("animal_label") or ""
        )

    animal_meta = []
    for animal in pre["animal_meta"]:
        a = dict(animal)
        val = labels_by_id.get(a["animal_id"], a.get("animal_label"))
        a["animal_label"] = None if val is None or str(val).strip() == "" else str(val)
        animal_meta.append(a)

    try:
        result = analyze_locomotor(
            preprocessed=pre,
            animal_meta=animal_meta,
            start_date=start_date,
            thresholds=threshold,
            min_episode_minutes=min_episode_minutes,
            bin_minutes=pre["bin_minutes"],
        )
        payload = build_summary_payload(result)
        # Temporary artifacts for this browser session/view only (not a saved "phase").
        save_json(jdir / "results.json", payload)
        if result.get("summary_xlsx_bytes"):
            (jdir / "analysis_summary.xlsx").write_bytes(result["summary_xlsx_bytes"])
        if result.get("episodes_xlsx_bytes"):
            (jdir / "all_episodes.xlsx").write_bytes(result["episodes_xlsx_bytes"])
    except Exception as exc:  # noqa: BLE001
        message = f"{type(exc).__name__}: {exc}"
        return render_template(
            "workspace.html",
            job_id=job_id,
            error=message,
            preview=preview,
            results=None,
            phase="preview",
            view="summary",
            preprocessed_b64=form_token or encode_preprocessed(pre),
        )

    return redirect(url_for("workspace", job_id=job_id, phase="analysis", view="summary"))


@app.route("/job/<job_id>/back", methods=["GET", "POST"])
def back_to_preview(job_id: str):
    """Return to phase 1 and discard phase-2 analysis artifacts."""
    jdir = job_path(job_id)
    for name in ("results.json", "analysis_summary.xlsx", "all_episodes.xlsx", "summary.json"):
        path = jdir / name
        if path.exists():
            path.unlink()
    return redirect(url_for("workspace", job_id=job_id))


@app.route("/job/<job_id>/download/summary", methods=["GET"])
def download_summary(job_id: str):
    path = job_path(job_id) / "analysis_summary.xlsx"
    if not path.exists():
        flash("Summary file not found. Run analysis first.")
        return redirect(url_for("workspace", job_id=job_id))
    return send_file(
        path,
        as_attachment=True,
        download_name=f"analysis_summary_{job_id}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/job/<job_id>/download/episodes", methods=["GET"])
def download_episodes(job_id: str):
    path = job_path(job_id) / "all_episodes.xlsx"
    if not path.exists():
        flash("Episodes file not found. Run analysis first.")
        return redirect(url_for("workspace", job_id=job_id))
    return send_file(
        path,
        as_attachment=True,
        download_name=f"all_episodes_{job_id}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# Vercel Python looking for a module-level WSGI/ASGI app named `app`.
# Local:
#   python3 app.py
if __name__ == "__main__":
    ensure_dirs()
    app.run(debug=True, port=int(os.environ.get("PORT", "5000")))
