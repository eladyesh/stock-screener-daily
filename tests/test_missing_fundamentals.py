"""Regressions for unavailable Yahoo metrics observed in the live smoke scan."""

import pytest

from src.data.fundamentals_fetcher import (
    analyze_fundamentals_for_signal,
    create_fundamental_snapshot,
)


@pytest.mark.parametrize("missing", [None, float("nan"), float("inf")])
def test_missing_metrics_remain_unknown_and_report_is_renderable(missing):
    data = dict.fromkeys([
        "revenue_yoy_change", "revenue_qoq_change", "eps_yoy_change",
        "eps_qoq_change", "gross_margin", "margin_change",
        "inventory_qoq_change", "inventory_to_sales_ratio",
    ], missing)

    assessment = analyze_fundamentals_for_signal(data)
    assert assessment["revenue_trend"] == "unknown"
    assert assessment["eps_trend"] == "unknown"
    assert assessment["inventory_signal"] == "unknown"
    assert assessment["supports_breakout"] is False
    report = create_fundamental_snapshot("TEST", data)
    assert "Insufficient revenue/EPS data" in report
    assert "SUPPORT technical breakout" not in report
    assert "DETERIORATING" not in report


def test_missing_inventory_does_not_drop_an_otherwise_analyzable_stock():
    data = {
        "revenue_yoy_change": 20, "revenue_qoq_change": 5,
        "eps_yoy_change": 15, "inventory_qoq_change": None,
    }
    result = analyze_fundamentals_for_signal(data)
    assert result["revenue_trend"] == "accelerating"
    assert result["inventory_signal"] == "unknown"
    assert result["penalty_points"] == 0
    assert "Revenue: Growing well" in create_fundamental_snapshot("TEST", data)


def test_known_zero_is_flat_not_missing():
    result = analyze_fundamentals_for_signal({
        "revenue_yoy_change": 0, "eps_yoy_change": 0,
        "revenue_qoq_change": 0, "inventory_qoq_change": 0,
    })
    assert result["revenue_trend"] == "flat"
    assert result["eps_trend"] == "flat"
    assert result["inventory_signal"] == "neutral"


def test_missing_margin_change_and_inventory_ratio_are_labeled():
    report = create_fundamental_snapshot("TEST", {
        "gross_margin": 25, "margin_change": None,
        "inventory_qoq_change": 20, "inventory_to_sales_ratio": None,
    })
    assert "QoQ change not available" in report
    assert "ratio: N/A" in report
    assert "inventory building rapidly" in report
