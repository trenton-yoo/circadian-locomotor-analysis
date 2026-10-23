"""
Public Python entry points for the locomotor analysis pipeline.

The scientific logic lives in ``pipeline/``; this module re-exports the
main callables for convenience and backwards compatibility with ``analysis.py``.
"""

from pipeline.analyze import analyze_locomotor, build_summary_payload, detect_episodes, get_period
from pipeline.preprocess import build_preview_payload, preprocess_locomotor

__all__ = [
    "preprocess_locomotor",
    "analyze_locomotor",
    "build_preview_payload",
    "build_summary_payload",
    "detect_episodes",
    "get_period",
]
