"""
Bare-bones local web UI for locomotor analysis.
Flow: upload xlsx → preprocess preview → set params → analyze → summary + download
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

ROOT = Path(__file__).resolve().parent
JOBS_DIR = ROOT / "data" / "jobs"
PIPELINE_DIR = ROOT / "pipeline"
RSCRIPT = shutil.which("Rscript") or "Rscript"

app = Flask(__name__)
app.secret_key = "dev-only-change-me"
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB


def ensure_dirs() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)


def run_rscript(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    cmd = [RSCRIPT, str(script), *args]
    env = os.environ.copy()
    env["LOCO_PROJECT_ROOT"] = str(ROOT)
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=env,
    )


def load_json(path: Path) -> dict:
    if not path.exists():
        return {"status": "error", "message": f"Missing file: {path.name}"}
    return json.loads(path.read_text(encoding="utf-8"))


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
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    input_path = job_dir / "input.xlsx"
    f.save(input_path)

    result = run_rscript(
        PIPELINE_DIR / "run_preprocess.R",
        str(input_path),
        str(job_dir),
    )

    preview = load_json(job_dir / "preview.json")
    if result.returncode != 0 or preview.get("status") != "ok":
        message = preview.get("message") or result.stderr or "Preprocess failed."
        return render_template(
            "workspace.html",
            job_id=job_id,
            error=message,
            preview=None,
            summary=None,
        )

    return redirect(url_for("workspace", job_id=job_id))


@app.route("/job/<job_id>", methods=["GET"])
def workspace(job_id: str):
    job_dir = JOBS_DIR / job_id
    if not job_dir.exists():
        flash("Job not found.")
        return redirect(url_for("index"))

    preview = load_json(job_dir / "preview.json")
    summary_path = job_dir / "summary.json"
    summary = load_json(summary_path) if summary_path.exists() else None

    error = None
    if preview.get("status") != "ok":
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
    )


@app.route("/job/<job_id>/analyze", methods=["POST"])
def analyze(job_id: str):
    job_dir = JOBS_DIR / job_id
    if not job_dir.exists():
        flash("Job not found.")
        return redirect(url_for("index"))

    preview = load_json(job_dir / "preview.json")
    if preview.get("status") != "ok":
        return render_template(
            "workspace.html",
            job_id=job_id,
            error=preview.get("message", "Cannot analyze: preprocess incomplete."),
            preview=None,
            summary=None,
        )

    try:
        threshold = float(request.form.get("threshold", "100"))
        min_episode_minutes = float(request.form.get("min_episode_minutes", "10"))
        start_date = request.form.get("start_date", preview.get("suggested_start_date"))
    except ValueError:
        return render_template(
            "workspace.html",
            job_id=job_id,
            error="Threshold and minimum episode length must be numbers.",
            preview=preview,
            summary=None,
        )

    labels = []
    for animal in preview.get("animal_meta", []):
        aid = animal["animal_id"]
        labels.append(
            {
                "animal_id": aid,
                "animal_label": request.form.get(f"label_{aid}", animal.get("animal_label", "")),
            }
        )

    params = {
        "threshold": threshold,
        "min_episode_minutes": min_episode_minutes,
        "start_date": start_date,
        "labels": labels,
    }
    params_path = job_dir / "params.json"
    params_path.write_text(json.dumps(params, indent=2), encoding="utf-8")

    result = run_rscript(
        PIPELINE_DIR / "run_analyze.R",
        str(job_dir),
        str(params_path),
    )
    summary = load_json(job_dir / "summary.json")

    if result.returncode != 0 or summary.get("status") != "ok":
        message = summary.get("message") or result.stderr or "Analyze failed."
        return render_template(
            "workspace.html",
            job_id=job_id,
            error=message,
            preview=preview,
            summary=None,
        )

    return redirect(url_for("workspace", job_id=job_id))


@app.route("/job/<job_id>/download", methods=["GET"])
def download(job_id: str):
    path = JOBS_DIR / job_id / "wake_episodes.xlsx"
    if not path.exists():
        flash("Output file not found. Run analysis first.")
        return redirect(url_for("workspace", job_id=job_id))
    return send_file(
        path,
        as_attachment=True,
        download_name=f"wake_episodes_{job_id}.xlsx",
    )


if __name__ == "__main__":
    ensure_dirs()
    app.run(debug=True, port=5000)
