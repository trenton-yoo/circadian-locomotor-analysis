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
from typing import Any, Dict, Optional

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
from pipeline.preprocess import build_preview_payload, preprocess_locomotor

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
            summary=None,
            preprocessed_b64="",
        )

    # Keep a copy for debugging locally; not required for analyze (state is embedded).
    (jdir / "input.xlsx").write_bytes(file_bytes)

    try:
        pre = preprocess_locomotor(
            file_bytes,
            sheet=0,
            bin_minutes=None,
            input_name=filename,
        )
        preview = build_preview_payload(pre)
        save_json(jdir / "preprocessed.json", pre)
        save_json(jdir / "preview.json", preview)
        token = encode_preprocessed(pre)
        (jdir / "preprocessed.b64.txt").write_text(token, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — surface to workspace
        message = f"{type(exc).__name__}: {exc}"
        if os.environ.get("FLASK_DEBUG") or os.environ.get("VERCEL_ENV") == "development":
            message = message + "\n\n" + traceback.format_exc()
        return render_template(
            "workspace.html",
            job_id=job_id,
            error=message,
            preview=None,
            summary=None,
            preprocessed_b64="",
        )

    return redirect(url_for("workspace", job_id=job_id))


@app.route("/job/<job_id>", methods=["GET"])
def workspace(job_id: str):
    jdir = job_path(job_id)
    preview_path = jdir / "preview.json"
    token_path = jdir / "preprocessed.b64.txt"

    preview = load_json(preview_path) if preview_path.exists() else None
    token = token_path.read_text(encoding="utf-8") if token_path.exists() else ""

    summary_path = jdir / "summary.json"
    summary = load_json(summary_path) if summary_path.exists() else None

    error = None
    if preview is None:
        error = "Job not found or expired. Please upload the file again."
    elif preview.get("status") != "ok":
        error = preview.get("message", "Preprocess error")
        preview = None

    if summary and summary.get("status") == "error":
        error = summary.get("message", "Analyze error")
        summary = None
    elif summary and summary.get("status") != "ok":
        summary = None

    return render_template(
        "workspace.html",
        job_id=job_id,
        preview=preview,
        summary=summary,
        error=error,
        preprocessed_b64=token,
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
            summary=None,
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
            summary=None,
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
            output_xlsx=jdir / "wake_episodes.xlsx",
        )
        summary = build_summary_payload(result)
        save_json(jdir / "summary.json", summary)
        if result.get("xlsx_bytes"):
            (jdir / "wake_episodes.xlsx").write_bytes(result["xlsx_bytes"])
    except Exception as exc:  # noqa: BLE001
        message = f"{type(exc).__name__}: {exc}"
        return render_template(
            "workspace.html",
            job_id=job_id,
            error=message,
            preview=preview,
            summary=None,
            preprocessed_b64=form_token or encode_preprocessed(pre),
        )

    return redirect(url_for("workspace", job_id=job_id))


@app.route("/job/<job_id>/download", methods=["GET"])
def download(job_id: str):
    path = job_path(job_id) / "wake_episodes.xlsx"
    if not path.exists():
        flash("Output file not found. Run analysis first.")
        return redirect(url_for("workspace", job_id=job_id))
    return send_file(
        path,
        as_attachment=True,
        download_name=f"wake_episodes_{job_id}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# Vercel Python looking for a module-level WSGI/ASGI app named `app`.
# Local:
#   python3 app.py
if __name__ == "__main__":
    ensure_dirs()
    app.run(debug=True, port=int(os.environ.get("PORT", "5000")))
