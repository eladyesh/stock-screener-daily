"""Risk and report regressions using deterministic prices, with no live APIs."""

import json

import numpy as np
import pandas as pd
import pytest

from src.screening.phase_indicators import classify_phase
from src.screening.signal_engine import score_buy_signal, format_signal_output
from src.screening.signal_policy import (
    BUY_MAX_SCORE,
    BUY_SCORE_THRESHOLD,
    SELL_MAX_SCORE,
    SELL_SCORE_THRESHOLD,
    SIGNAL_POLICY_VERSION,
)
from src.screening.trade_risk import assess_trade_risk, calculate_stop_loss


@pytest.fixture
def prices():
    close = np.linspace(60, 100, 252)
    return pd.DataFrame(
        {
            "Close": close,
            "Open": close,
            "Low": close - 1,
            "High": close + 1,
            "Volume": 1_000_000,
        },
        index=pd.bdate_range(end="2026-11-10", periods=252),
    )


def signal_for(prices, phase_info=None):
    return score_buy_signal(
        "TEST",
        prices,
        100.0,
        phase_info or classify_phase(prices, 100.0),
        pd.Series(np.linspace(1, 1.3, len(prices)), index=prices.index),
        fundamentals={
            "revenue_yoy_change": 35,
            "eps_yoy_change": 65,
            "operating_margin_yoy_change": 5,
            "inventory_yoy_change": 5,
        },
    )


def test_new_high_has_no_fabricated_target_or_rr_bonus(prices):
    signal = signal_for(prices)
    assert signal["is_buy"]  # Other components may qualify; unknown is explicit.
    assert signal["score"] >= BUY_SCORE_THRESHOLD
    assert signal["risk_reward_ratio"] is None
    assert signal["reward_target"] is None
    assert signal["risk_status"] == "target_unavailable"
    assert signal["details"]["rr_score"] == 0
    assert signal["signal_policy_version"] == SIGNAL_POLICY_VERSION


def test_nearest_confirmed_resistance_is_used_with_its_observation_date(prices):
    prices.loc[prices.index[-40], "High"] = 130
    prices.loc[prices.index[-20], "High"] = 115
    risk = assess_trade_risk(prices, 100, {"sma_50": 95}, 2)
    assert risk["reward_target"] == 115
    assert risk["target_date"] == prices.index[-20].date().isoformat()
    assert risk["target_basis"] == "nearest_confirmed_prior_swing_high"
    assert risk["risk_reward_ratio"] == pytest.approx(15 / (100 - risk["stop_loss"]))
    assert risk["risk_eligible"]


@pytest.mark.parametrize("offset", [-1, -2, -3])
def test_entry_bar_and_unconfirmed_recent_highs_cannot_supply_target(prices, offset):
    prices.loc[prices.index[offset], "High"] = 130
    assert assess_trade_risk(prices, 100, {"sma_50": 95}, 2)["reward_target"] is None


def test_pivot_with_two_completed_confirmation_bars_is_allowed(prices):
    prices.loc[prices.index[-4], "High"] = 130
    assert assess_trade_risk(prices, 100, {"sma_50": 95}, 2)["reward_target"] == 130


def test_poor_observed_rr_vetoes_even_a_high_scoring_buy(prices):
    prices.loc[prices.index[-20], "High"] = 104
    signal = signal_for(prices)
    assert signal["score"] >= BUY_SCORE_THRESHOLD
    assert signal["risk_reward_ratio"] < 2
    assert signal["risk_status"] == "insufficient_reward_risk"
    assert signal["details"]["rr_score"] == 0
    assert not signal["is_buy"]


def test_wide_support_stop_is_preserved_and_vetoes_buy(prices):
    prices.loc[prices.index[-10:], "Low"] = 80
    info = classify_phase(prices, 100.0)
    info.update(sma_50=85, sma_150=80, sma_200=75)
    signal = signal_for(prices, info)
    assert signal["score"] >= BUY_SCORE_THRESHOLD
    assert signal["stop_loss"] == pytest.approx(84.15)
    assert signal["stop_loss"] < 90  # Old code silently moved it to 90.
    assert signal["risk_status"] == "stop_too_wide"
    assert not signal["is_buy"]


def test_minimum_stop_buffer_is_named_and_not_mistaken_for_observed_support(prices):
    prices.loc[prices.index[-10:], "Low"] = 99
    risk = assess_trade_risk(prices, 100, {"sma_50": 99}, 2)
    assert risk["stop_loss"] == 97
    assert "minimum_distance_buffer" in risk["stop_basis"]


@pytest.mark.parametrize("entry", [0, -1, float("nan"), float("inf")])
def test_invalid_entry_never_yields_risk_eligibility(prices, entry):
    risk = assess_trade_risk(prices, entry, {"sma_50": 95}, 2)
    assert not risk["risk_eligible"]
    assert risk["risk_reward_ratio"] is None


@pytest.mark.parametrize("low", [0, float("nan"), float("inf")])
def test_invalid_low_data_cannot_be_hidden_by_sma_stop(prices, low):
    prices.loc[prices.index[-2], "Low"] = low
    assert calculate_stop_loss(prices, 100, {"sma_50": 95}, 2) is None


def test_missing_and_unordered_price_evidence_are_safe(prices):
    assert not assess_trade_risk(pd.DataFrame(), 100, {}, 2)["risk_eligible"]
    prices.loc[prices.index[-20], "High"] = 120
    assert assess_trade_risk(prices.iloc[::-1], 100, {}, 2)["reward_target"] is None
    prices.loc[prices.index[-19], "High"] = np.nan
    assert assess_trade_risk(prices, 100, {}, 2)["reward_target"] is None


def test_report_round_trip_displays_missing_rr_and_consistent_thresholds(
    prices, tmp_path, monkeypatch
):
    import run_optimized_scan as scanner
    from scripts.report_utils import load_current_report

    monkeypatch.setattr(
        scanner, "format_benchmark_summary", lambda *args: "Benchmark fixture"
    )
    signal = signal_for(prices)
    signal["details"]["rs_slope"] = None
    results = {
        "total_processed": 1,
        "total_analyzed": 1,
        "processing_time_seconds": 1,
        "actual_tps": 1,
        "error_rate": 0,
        "analyses": [{"price_data": prices}],
    }
    audit = {
        "mode": "exchange_directories",
        "included": 1,
        "excluded": 2,
        "policy_version": "common-equity-v2",
        "complete": False,
        "sources": {"otherlisted": {"status": "error"}},
        "decisions": [],
    }
    scanner.save_report(
        results, [signal], [], {}, {}, output_dir=tmp_path, universe_audit=audit
    )
    report, manifest = load_current_report(
        tmp_path / "latest_optimized_scan.txt", tmp_path / "latest_scan_manifest.json"
    )
    assert f"TOP BUY SIGNALS (Score >= {BUY_SCORE_THRESHOLD})" in report
    assert f"TOP SELL SIGNALS (Score >= {SELL_SCORE_THRESHOLD})" in report
    assert f"{signal['score']}/{BUY_MAX_SCORE}" in report
    assert "Risk/Reward: unavailable" in report
    assert "incomplete exchange coverage" in report
    assert manifest["signal_policy_version"] == SIGNAL_POLICY_VERSION
    assert "decisions" not in manifest["universe_selection"]
    assert json.loads((tmp_path / "latest_universe_audit.json").read_text()) == audit
    assert f"/{BUY_MAX_SCORE}" in format_signal_output(signal)
    sell = dict(signal, severity="medium")
    assert f"/{SELL_MAX_SCORE}" in format_signal_output(sell, "sell")


def test_report_shows_target_basis_and_date(prices):
    prices.loc[prices.index[-20], "High"] = 120
    signal = signal_for(prices)
    assert signal["is_buy"]
    report = format_signal_output(signal)
    assert "Observed resistance: $120.00" in report
    assert prices.index[-20].date().isoformat() in report
    assert "not a forecast" in report


def test_forward_evaluation_preserves_policy_versions(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from scripts import evaluate_research

    monkeypatch.setattr(
        evaluate_research.yf,
        "Ticker",
        lambda *a, **k: pytest.fail("Unexpected live call"),
    )
    generated_at = datetime.now(timezone.utc).isoformat()
    for version in ("multifactor-v1", "multifactor-v2"):
        snapshot = {
            "generated_at": generated_at,
            "strategy_version": version,
            "rankings": {"weeks_to_months": [], "months_to_18m": []},
        }
        if version == "multifactor-v2":
            snapshot["signal_policy_version"] = SIGNAL_POLICY_VERSION
        (tmp_path / f"research_{version}.json").write_text(json.dumps(snapshot))
    output = tmp_path / "evaluation.json"
    monkeypatch.setattr(
        "sys.argv",
        ["evaluate_research", "--directory", str(tmp_path), "--output", str(output)],
    )
    evaluate_research.main()
    result = json.loads(output.read_text())
    assert result["snapshots"] == 2
    assert {r["strategy_version"] for r in result["cohorts"]} == {
        "multifactor-v1",
        "multifactor-v2",
    }
    for record in result["cohorts"]:
        assert record["signal_policy_version"] == (
            SIGNAL_POLICY_VERSION
            if record["strategy_version"] == "multifactor-v2"
            else None
        )
