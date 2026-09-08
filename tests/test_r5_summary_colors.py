from __future__ import annotations

from processing_signals.processing.cvd_volume_orderflow.cvd_volume_orderflow_processor import CvdVolumeOrderflowProcessor


def test_cvd_native_summary_emits_css_color_not_semantic_word() -> None:
    positive = CvdVolumeOrderflowProcessor._native_indicator(
        "x", [1, 2], {"value": [0.0, 0.8]}, section="flow", label="X"
    )["summary"]
    negative = CvdVolumeOrderflowProcessor._native_indicator(
        "x", [1, 2], {"value": [0.0, -0.8]}, section="flow", label="X"
    )["summary"]
    neutral = CvdVolumeOrderflowProcessor._native_indicator(
        "x", [1, 2], {"value": [0.0, 0.1]}, section="flow", label="X"
    )["summary"]
    assert positive["signal_color"] == "#20d05c"
    assert negative["signal_color"] == "#ff3d55"
    assert neutral["signal_color"] == "#ffab00"


def test_etf_processor_source_contains_css_signal_palette() -> None:
    from pathlib import Path
    import processing_signals.processing.etf_exchange_flows.etf_exchange_flows_processor as module
    source = Path(module.__file__).read_text(encoding="utf-8")
    assert '"positive": "#20d05c"' in source
    assert '"negative": "#ff3d55"' in source
    assert '"neutral": "#ffab00"' in source
