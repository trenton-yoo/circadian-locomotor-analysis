"""
Relative-frequency histograms of wake episode lengths.
"""

from __future__ import annotations

import base64
import io
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

# Serverless-safe matplotlib setup (must set before pyplot import).
os.environ.setdefault("MPLCONFIGDIR", "/tmp")
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def _filter_durations(
    episodes: Sequence[Dict[str, Any]],
    animal_ids: Optional[Iterable[int]],
    period: str,
) -> List[float]:
    """period: 'Day' | 'Night' | 'All'."""
    wanted = None if animal_ids is None else {int(a) for a in animal_ids}
    out: List[float] = []
    for ep in episodes:
        if wanted is not None and int(ep["animal_id"]) not in wanted:
            continue
        if period in {"Day", "Night"} and ep.get("period") != period:
            continue
        out.append(float(ep["duration_minutes"]))
    return out


def _label_for_panel(
    animal_ids: Optional[Sequence[int]],
    period: str,
    animal_lookup: Dict[int, str],
) -> str:
    if animal_ids is None or len(animal_ids) == 0:
        animals = "all animals"
    else:
        parts = []
        for aid in animal_ids:
            lab = animal_lookup.get(int(aid), "")
            parts.append(f"{aid}" + (f" ({lab})" if lab else ""))
        animals = ", ".join(parts)
    period_txt = "entire period" if period == "All" else period
    return f"{animals} · {period_txt}"


def _shared_bins(
    groups: List[List[float]],
    bin_minutes: Optional[float] = None,
) -> np.ndarray:
    all_vals = [v for g in groups for v in g]
    if not all_vals:
        return np.array([0.0, 1.0])
    lo = 0.0
    hi = max(all_vals)
    if hi <= lo:
        hi = lo + (bin_minutes or 5.0)
    step = float(bin_minutes) if bin_minutes and bin_minutes > 0 else None
    if step is None:
        # Sturges-ish fallback
        n = max(len(all_vals), 1)
        k = max(int(np.ceil(np.log2(n) + 1)), 5)
        step = max((hi - lo) / k, 1.0)
    # Align edges to step
    n_bins = int(np.ceil(hi / step)) + 1
    return np.arange(0.0, step * n_bins + step * 0.5, step)


def build_relative_frequency_histograms(
    episodes: Sequence[Dict[str, Any]],
    panels: List[Dict[str, Any]],
    bin_minutes: Optional[float] = None,
    animal_lookup: Optional[Dict[int, str]] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Build one or two stacked relative-frequency histograms.

    Each panel dict:
      animal_ids: list[int] | None (None/empty = all)
      period: 'Day' | 'Night' | 'All'
      show_mean: bool

    Returns (png_base64, panel_stats).
    """
    animal_lookup = animal_lookup or {}
    group_data: List[List[float]] = []
    panel_stats: List[Dict[str, Any]] = []

    for i, panel in enumerate(panels):
        aids = panel.get("animal_ids")
        if aids is not None:
            aids = [int(a) for a in aids]
            if len(aids) == 0:
                aids = None
        period = panel.get("period", "All")
        if period not in {"Day", "Night", "All"}:
            period = "All"
        show_mean = bool(panel.get("show_mean", False))
        vals = _filter_durations(episodes, aids, period)
        group_data.append(vals)
        mean_val = float(np.mean(vals)) if vals else None
        panel_stats.append(
            {
                "index": i + 1,
                "n": len(vals),
                "mean": None if mean_val is None else round(mean_val, 2),
                "show_mean": show_mean,
                "period": period,
                "animal_ids": aids,
                "title": _label_for_panel(aids, period, animal_lookup),
            }
        )

    bins = _shared_bins(group_data, bin_minutes=bin_minutes)
    n_panels = len(panels)
    fig_h = 3.2 * n_panels + 0.6
    fig, axes = plt.subplots(n_panels, 1, figsize=(8.5, fig_h), sharex=True)
    if n_panels == 1:
        axes = [axes]

    for ax, vals, stats in zip(axes, group_data, panel_stats):
        if vals:
            weights = np.ones(len(vals)) / len(vals)
            ax.hist(
                vals,
                bins=bins,
                weights=weights,
                color="#6b7c5e",
                edgecolor="#333333",
                alpha=0.85,
            )
            if stats["show_mean"] and stats["mean"] is not None:
                ax.axvline(
                    stats["mean"],
                    color="#8b0000",
                    linestyle="--",
                    linewidth=1.5,
                    label=f"mean = {stats['mean']} min",
                )
                ax.legend(loc="upper right", fontsize=9)
        else:
            ax.text(
                0.5,
                0.5,
                "No episodes for this selection",
                ha="center",
                va="center",
                transform=ax.transAxes,
            )
        ax.set_ylabel("Relative frequency")
        ax.set_title(f"Panel {stats['index']}: {stats['title']} (n={stats['n']})")
        ax.set_ylim(bottom=0)

    axes[-1].set_xlabel("Episode length (minutes)")
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    plt.close(fig)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return b64, panel_stats


def animal_options_from_results(results: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Unique animals from episode/summary records for UI checkboxes."""
    seen = {}
    for row in results.get("episodes") or results.get("summary") or []:
        aid = int(row["animal_id"])
        if aid not in seen:
            seen[aid] = {
                "animal_id": aid,
                "cage_code": row.get("cage_code") or "",
                "animal_label": row.get("animal_label") or "",
            }
    return [seen[k] for k in sorted(seen)]
