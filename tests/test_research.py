"""Regression tests for investment-relevant data errors and complete report flow."""

import json
from datetime import datetime, timezone
from unittest.mock import Mock

import pandas as pd
import pytest

from src.data.financial_metrics import (
    completed_daily_bars,
    growth,
    statement_metrics,
    ttm,
)
from src.data.research_fetcher import ResearchFetcher, parse_insiders, parse_news
from src.screening.fundamental_score import score_fundamentals
from src.screening.research_engine import (
    add_peer_context,
    build_research,
    format_research,
    load_config,
    save_research,
    score_record,
    select_candidates,
)
from scripts.evaluate_research import cohort_returns
import requests

NOW = datetime(2026, 10, 15, 8, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_live_search_in_unit_tests(monkeypatch):
    monkeypatch.setattr(
        "src.data.research_fetcher.yf.Search", Mock(return_value=Mock(news=[]))
    )


@pytest.fixture
def statements():
    dates = pd.to_datetime(
        ["2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"]
    )
    income = pd.DataFrame(
        {
            "Total Revenue": [100, 130, 140, 300, 125],
            "Diluted EPS": [1, 1.1, 1.2, 1.3, 1.5],
            "Operating Income": [10, 14, 17, 40, 20],
            "Gross Profit": [30, 40, 45, 95, 45],
            "Net Income": [8, 10, 12, 30, 15],
            "EBITDA": [12, 17, 21, 45, 25],
            "Diluted Average Shares": [100, 100, 100, 100, 102],
        },
        index=dates,
    ).T
    balance = pd.DataFrame(
        {
            "Total Debt": [20] * 5,
            "Cash And Cash Equivalents": [30] * 5,
            "Total Assets": [100] * 5,
            "Inventory": [10, 11, 12, 14, 11],
        },
        index=dates,
    ).T
    cash = pd.DataFrame(
        {"Operating Cash Flow": [20] * 5, "Capital Expenditure": [-5] * 5}, index=dates
    ).T
    return income, balance, cash


@pytest.fixture
def analysis():
    idx = pd.bdate_range(end="2026-10-14", periods=252)
    prices = pd.DataFrame({"Close": [100 + i / 2 for i in range(252)]}, index=idx)
    return {
        "ticker": "TEST",
        "current_price": prices.Close.iloc[-1],
        "price_data": prices,
        "phase_info": {"phase": 2, "sma_50": 200, "sma_200": 150},
        "rs_series": pd.Series([1 + i / 1000 for i in range(252)], index=idx),
        "quarterly_data": {"revenue_yoy_change": 30, "eps_yoy_change": 60},
    }


@pytest.fixture
def record():
    return {
        "ticker": "TEST",
        "retrieved_at": NOW.isoformat(),
        "sector": "Technology",
        "industry": "Software",
        "metrics": {
            "revenue_yoy_change": 30,
            "eps_yoy_change": 60,
            "forward_revenue_growth": 25,
            "forward_eps_growth": 30,
            "net_margin": 25,
            "shares_yoy_change": -3,
            "fcf_margin": 25,
            "net_debt_to_ebitda": 0,
            "forward_pe": 10,
            "fcf_yield": 8,
            "ev_ebitda": 8,
            "eps_revision_30d": 5,
            "eps_revision_90d": 10,
            "eps_surprise_pct": 10,
            "days_to_earnings": 30,
        },
        "statements": {"report_period": "2026-06-30", "fetch_date": NOW.isoformat()},
        "sources": {},
        "warnings": [],
        "status": {},
        "news": [],
        "insider_purchases": [],
        "filings": [],
    }


def test_quarters_align_and_seasonal_decline_is_not_yoy_deterioration(statements):
    data = statement_metrics(*statements)
    assert data["revenue_yoy_change"] == 25
    assert data["revenue_qoq_change"] < -50
    assert data["operating_margin_yoy_change"] == 6
    assert data["free_cash_flow_ttm"] == 60
    assert data["shares_yoy_change"] == pytest.approx(2)
    assert data["net_debt"] == -10
    score = score_fundamentals(data)
    data["revenue_qoq_change"] = 50
    assert score == score_fundamentals(data)


@pytest.mark.parametrize("value", [None, float("nan"), float("inf")])
def test_missing_eps_never_earns_neutral_bonus(value):
    assert score_fundamentals({"eps_yoy_change": value})["score"] == 0
    assert score_fundamentals({"eps_yoy_change": value})["coverage"] == 0


def test_zero_growth_is_a_real_observation():
    r = score_fundamentals({"eps_yoy_change": 0})
    assert r["coverage"] > 0
    assert r["score"] == 3.75


def test_no_margin_placeholder_and_score_is_bounded():
    assert score_fundamentals({})["score"] == 0
    assert (
        score_fundamentals(
            {
                "revenue_yoy_change": 1000,
                "eps_yoy_change": 1000,
                "operating_margin_yoy_change": 50,
                "inventory_yoy_change": -20,
            }
        )["score"]
        == 40
    )


@pytest.mark.parametrize("base", [0, -1, None, float("nan")])
def test_growth_does_not_mislabel_loss_to_profit_or_zero_base(base):
    assert growth(2, base) is None


def test_ttm_requires_four_consecutive_complete_quarters(statements):
    row = statements[2].loc["Operating Cash Flow"]
    assert ttm(row.drop(row.index[-2]), row.index[-1]) is None
    row.iloc[-2] = float("nan")
    assert ttm(row, row.index[-1]) is None


def test_no_substitution_of_old_balance_period(statements):
    income, balance, cash = statements
    result = statement_metrics(income, balance.drop(columns=balance.columns[-1]), cash)
    assert result["total_debt"] is None
    assert result.get("net_debt") is None


def test_partial_and_future_price_bars_are_removed():
    f = pd.DataFrame(
        {"Close": [1, 2, 3]},
        index=pd.to_datetime(["2026-10-14", "2026-10-15", "2026-10-16"]),
    )
    assert list(completed_daily_bars(f, NOW).Close) == [1]
    assert list(
        completed_daily_bars(f, datetime(2026, 10, 15, 21, tzinfo=timezone.utc)).Close
    ) == [1, 2]


def test_two_horizons_respond_to_different_evidence(analysis, record):
    record["metrics"]["eps_revision_30d"] = -5
    record["metrics"]["eps_revision_90d"] = -10
    result = score_record(analysis, record, {"is_buy": True}, load_config(), NOW)
    assert (
        result["profiles"]["months_to_18m"]["score"]
        > result["profiles"]["weeks_to_months"]["score"]
    )


def test_unknown_and_near_earnings_do_not_become_swing_candidates(analysis, record):
    for days in (None, 2):
        record["metrics"]["days_to_earnings"] = days
        result = score_record(analysis, record, {"is_buy": True}, load_config(), NOW)
        assert result["profiles"]["weeks_to_months"]["status"] == "watchlist"
        assert result["profiles"]["months_to_18m"]["status"] == "candidate"


def test_stale_statements_and_missing_inputs_are_visible(analysis, record):
    record["statements"]["fetch_date"] = "2026-09-01T00:00:00+00:00"
    out = score_record(analysis, record, {"is_buy": True}, load_config(), NOW)
    assert all(p["status"] == "insufficient_data" for p in out["profiles"].values())
    record["metrics"] = {}
    out = score_record(analysis, record, {}, load_config(), NOW)
    assert out["components"]["quality"]["score"] == 0
    assert out["profiles"]["months_to_18m"]["coverage"] < 0.2


def test_market_gate_applies_to_both_horizons(analysis, record):
    out = score_record(
        analysis, record, {"is_buy": True}, load_config(), NOW, allow_buys=False
    )
    assert all(p["status"] == "watchlist" for p in out["profiles"].values())


def test_long_horizon_candidate_does_not_require_legacy_buy(analysis, record):
    analysis["phase_info"]["phase"] = 1
    assert select_candidates([analysis], [], 5) == [analysis]
    out = score_record(analysis, record, {}, load_config(), NOW)
    assert out["profiles"]["months_to_18m"]["status"] == "candidate"
    assert out["profiles"]["weeks_to_months"]["status"] == "watchlist"


def test_insider_awards_and_sales_are_excluded():
    frame = pd.DataFrame(
        [
            {"Text": text, "Value": 1000, "Start Date": "2026-10-01", "Insider": "Name"}
            for text in [
                "Purchase at price 10",
                "Sale at price 10",
                "Stock Award",
                "Purchase: option exercise",
            ]
        ]
    )
    purchases = parse_insiders(frame, NOW.date())
    assert len(purchases) == 1
    assert "not independently verified" in purchases[0]["verification"]


def test_news_dates_deduplication_and_no_sentiment_fabrication():
    def article(day):
        return {
            "content": {
                "title": "Company announces offering",
                "pubDate": day,
                "canonicalUrl": {"url": "https://example.org/story"},
            }
        }

    result = parse_news(
        [
            article("2026-10-14"),
            article("2026-10-14"),
            article("2026-01-01"),
            article("2026-10-16"),
        ],
        NOW.date(),
    )
    assert len(result) == 1
    assert result[0]["categories"] == ["financing/dilution"]
    assert "sentiment" not in result[0]


def test_peer_context_requires_five_comparable_names(record):
    import copy

    rows = []
    for i in range(6):
        r = copy.deepcopy(record)
        r["ticker"] = f"T{i}"
        rows.append(r)
    add_peer_context(rows[:4])
    assert "forward_pe_relative" not in rows[0]["metrics"]
    add_peer_context(rows)
    assert rows[0]["metrics"]["forward_pe_relative"] == 1
    assert "selection-biased" in rows[0]["peer_comparison"]["forward_pe"]["scope"]


def fake_stock(statements):
    stock = Mock()
    (
        stock.quarterly_income_stmt,
        stock.quarterly_balance_sheet,
        stock.quarterly_cashflow,
    ) = statements
    stock.get_info.return_value = {
        "sector": "Technology",
        "industry": "Software",
        "currency": "USD",
        "financialCurrency": "USD",
        "marketCap": 1000,
        "forwardPE": 20,
    }
    stock.get_earnings_estimate.return_value = pd.DataFrame(
        {"avg": [2, 3], "numberOfAnalysts": [10, 10]}, index=["0y", "+1y"]
    )
    stock.get_revenue_estimate.return_value = pd.DataFrame(
        {"avg": [100, 125]}, index=["0y", "+1y"]
    )
    stock.get_eps_trend.return_value = pd.DataFrame(
        {"current": [3], "30daysAgo": [2.9], "90daysAgo": [2.8]}, index=["+1y"]
    )
    stock.get_earnings_history.return_value = pd.DataFrame()
    stock.get_calendar.return_value = {"Earnings Date": [pd.Timestamp("2026-11-01")]}
    stock.get_news.return_value = []
    stock.get_insider_transactions.return_value = pd.DataFrame()
    stock.get_sec_filings.return_value = []
    return stock


def test_fetcher_provenance_forecasts_currency_and_cache(tmp_path, statements):
    stock = fake_stock(statements)
    fetcher = ResearchFetcher(
        tmp_path, use_fmp=False, now=NOW, ticker_factory=lambda t: stock
    )
    r = fetcher.fetch("TEST")
    assert r["metrics"]["forward_eps_growth"] == 50
    assert r["metrics"]["fcf_yield"] == 6
    assert r["sources"]["eps_fy1"]["period"] == "+1y"
    fetcher.fetch("TEST")
    assert stock.get_info.call_count == 1
    stock.get_info.return_value["financialCurrency"] = "EUR"
    r = fetcher.fetch("TEST", refresh=True)
    assert r["metrics"].get("fcf_yield") is None


def test_failure_of_news_does_not_erase_financials(tmp_path, statements):
    stock = fake_stock(statements)
    stock.get_news.side_effect = RuntimeError("provider unavailable")
    r = ResearchFetcher(
        tmp_path, use_fmp=False, now=NOW, ticker_factory=lambda t: stock
    ).fetch("TEST")
    assert r["status"]["news"] == "unavailable"
    assert r["metrics"]["eps_fy1"] == 3


def test_rate_limit_stops_subsequent_yahoo_calls(tmp_path, statements):
    class YFRateLimitError(Exception):
        pass

    stock = fake_stock(statements)
    stock.get_info.side_effect = YFRateLimitError()
    r = ResearchFetcher(
        tmp_path, use_fmp=False, now=NOW, ticker_factory=lambda t: stock
    ).fetch("TEST")
    assert r["status"]["info"] == "rate_limited"
    stock.get_earnings_estimate.assert_not_called()
    assert r["metrics"].get("eps_fy1") is None


def test_complete_build_render_and_snapshot_roundtrip(tmp_path, analysis, statements):
    stock = fake_stock(statements)
    fetcher = ResearchFetcher(
        tmp_path / "cache", use_fmp=False, now=NOW, ticker_factory=lambda t: stock
    )
    report = build_research([analysis], [], fetcher=fetcher, now=NOW)
    save_research(report, tmp_path / "reports")
    saved = json.loads((tmp_path / "reports/latest_research.json").read_text())
    assert saved["enriched"] == 1
    assert set(saved["rows"][0]["profiles"]) == {"weeks_to_months", "months_to_18m"}
    assert "data coverage" in format_research(saved)
    assert "Yahoo Finance" in format_research(saved)
    assert len(list((tmp_path / "reports").glob("research_*.json"))) == 1


def test_evaluation_never_enters_at_the_signal_days_close():
    dates = pd.bdate_range("2026-10-15", periods=25)
    history = pd.Series([100, 200] + [220] * 23, index=dates)
    snapshot = {
        "generated_at": NOW.isoformat(),
        "rankings": {"weeks_to_months": ["TEST"]},
        "technical_baseline": ["TEST"],
    }
    result = cohort_returns(
        snapshot, {"SPY": history, "TEST": history}, 21, "weeks_to_months", cost_bps=10
    )
    assert result["entry_date"] == "2026-10-16"
    assert result["research"]["equal_weight_return"] == pytest.approx(0.099)
    assert result["research"]["excess_vs_spy"] == pytest.approx(-0.001)
    missing = cohort_returns(snapshot, {"SPY": history}, 21, "weeks_to_months")
    assert missing["research"]["equal_weight_return"] is None
    assert missing["research"]["missing"] == ["TEST"]
    assert (
        cohort_returns(snapshot, {"SPY": history.iloc[:5]}, 21, "weeks_to_months")[
            "status"
        ]
        == "not_mature"
    )


def test_fmp_uses_consecutive_same_provider_fiscal_pairs_without_leaking_key(
    tmp_path, statements, monkeypatch
):
    monkeypatch.setenv("FMP_API_KEY", "test-secret-key")
    stock = fake_stock(statements)
    stock.get_earnings_estimate.return_value = pd.DataFrame()
    response = Mock(status_code=200)
    response.json.return_value = [
        {"date": "2027-12-31", "epsAvg": 3, "revenueAvg": 150, "numAnalystsEps": 5},
        {"date": "2026-12-31", "epsAvg": 2, "revenueAvg": 100, "numAnalystsEps": 5},
    ]
    monkeypatch.setattr(
        "src.data.research_fetcher.requests.get", Mock(return_value=response)
    )
    fetcher = ResearchFetcher(tmp_path, now=NOW, ticker_factory=lambda t: stock)
    r = fetcher.fetch("TEST")
    assert r["metrics"]["forward_eps_growth"] == 50
    assert r["metrics"]["forward_revenue_growth"] == 50
    assert r["sources"]["eps_fy1"]["provider"] == "FMP"
    assert r["sources"]["eps_fy1"]["period"] == "2027-12-31"
    assert "test-secret-key" not in json.dumps(r)
    assert fetcher.fmp_calls == 1
    response.json.return_value = [{"date": "2026-12-31", "epsAvg": 2}]
    r = fetcher.fetch("TEST", refresh=True)
    assert r["status"]["fmp_estimates"] == "incomplete_fiscal_pair"
    assert r["metrics"]["eps_fy1"] is None


def test_fmp_authorization_failure_stops_further_requests(
    tmp_path, statements, monkeypatch
):
    monkeypatch.setenv("FMP_API_KEY", "test-key")
    stock = fake_stock(statements)
    stock.get_earnings_estimate.return_value = pd.DataFrame()
    response = Mock(status_code=403)
    response.raise_for_status.side_effect = requests.HTTPError("secret-bearing URL")
    request = Mock(return_value=response)
    monkeypatch.setattr("src.data.research_fetcher.requests.get", request)
    fetcher = ResearchFetcher(tmp_path, now=NOW, ticker_factory=lambda t: stock)
    assert fetcher.fetch("TEST")["status"]["fmp_estimates"] == "unavailable"
    assert (
        fetcher.fetch("NEXT")["status"]["fmp_estimates"] == "budget_or_provider_limit"
    )
    assert request.call_count == 1


def test_invalid_optional_schema_is_isolated(tmp_path, statements):
    stock = fake_stock(statements)
    stock.get_calendar.return_value = []
    r = ResearchFetcher(
        tmp_path, use_fmp=False, now=NOW, ticker_factory=lambda t: stock
    ).fetch("TEST")
    assert r["status"]["calendar"] == "invalid_schema"
    assert r["metrics"]["eps_fy1"] == 3


def test_old_nan_cache_is_json_safe_and_remains_undated(tmp_path, statements):
    stock = fake_stock(statements)
    stock.quarterly_income_stmt = pd.DataFrame()
    r = ResearchFetcher(
        tmp_path, use_fmp=False, now=NOW, ticker_factory=lambda t: stock
    ).fetch(
        "TEST",
        fallback={"quarterly_eps": {"2026-06-30": float("nan")}, "eps_yoy_change": 10},
    )
    assert r["statements"]["quarterly_eps"]["2026-06-30"] is None
    assert r["sources"]["eps_yoy_change"]["retrieved_at"] == "unknown"
    json.dumps(r, allow_nan=False)


def test_numeric_price_index_is_rejected_instead_of_becoming_1970():
    with pytest.raises(ValueError, match="DatetimeIndex"):
        completed_daily_bars(pd.DataFrame({"Close": [10, 11]}), NOW)


@pytest.mark.parametrize("dated", [False, True])
def test_checkout_mtime_does_not_validate_undated_or_old_price_cache(
    tmp_path, monkeypatch, dated
):
    from src.data.fetcher import YahooFinanceFetcher

    fetcher = YahooFinanceFetcher(cache_dir=str(tmp_path))
    cached = pd.DataFrame({"Close": [10, 11]})
    if dated:
        cached.index = pd.to_datetime(["2020-01-01", "2020-01-02"])
    fetcher._save_to_cache(cached, fetcher._get_cache_path("TEST", "prices_5y_1d"))
    stock = Mock()
    stock.info = {"symbol": "TEST"}
    fresh = pd.DataFrame(
        {"Close": [20, 21]},
        index=pd.date_range(end=pd.Timestamp.now().normalize(), periods=2),
    )
    stock.history.return_value = fresh
    factory = Mock(return_value=stock)
    monkeypatch.setattr("src.data.fetcher.yf.Ticker", factory)
    result = fetcher.fetch_price_history("TEST")
    assert result.Close.iloc[-1] == 21
    factory.assert_called_once()


def test_valuation_history_requires_prior_dates_and_does_not_double_count(
    tmp_path, statements
):
    from datetime import timedelta

    stock = fake_stock(statements)
    fetcher = ResearchFetcher(
        tmp_path, use_fmp=False, now=NOW, ticker_factory=lambda t: stock
    )
    first = fetcher.fetch("TEST")
    assert first["valuation_history"]["forward_pe"]["prior_observations"] == 0
    assert "forward_pe_historical_relative" not in first["metrics"]
    history_path = tmp_path / "valuation_history/TEST.json"
    rows = [
        {"date": (NOW.date() - timedelta(days=i)).isoformat(), "forward_pe": 10}
        for i in range(1, 21)
    ]
    history_path.write_text(json.dumps(rows))
    r = fetcher.fetch("TEST", refresh=True)
    assert r["metrics"]["forward_pe_historical_relative"] == 2
    fetcher.fetch("TEST", refresh=True)
    assert len(json.loads(history_path.read_text())) == 21


def test_real_numpy_signal_types_survive_snapshot_serialization(
    tmp_path, analysis, record
):
    import numpy as np

    signal = {
        "is_buy": np.bool_(True),
        "score": np.float64(90),
        "risk_reward_ratio": np.float64(3),
        "stop_loss": np.float64(200),
    }
    row = score_record(analysis, record, signal, load_config(), NOW)
    snapshot = {"generated_at": NOW.isoformat(), "rows": [row]}
    save_research(snapshot, tmp_path)
    saved = json.loads((tmp_path / "latest_research.json").read_text())
    assert saved["rows"][0]["technical_signal"]["is_buy"] is True
    assert saved["rows"][0]["technical_signal"]["stop_loss"] == 200


def test_news_search_fallback_requires_explicit_ticker_match(
    tmp_path, statements, monkeypatch
):
    items = [
        {
            "title": "Company wins contract",
            "link": "https://example.org/test",
            "relatedTickers": ["TEST"],
            "providerPublishTime": int(NOW.timestamp()),
        },
        {
            "title": "Other company offering",
            "link": "https://example.org/other",
            "relatedTickers": ["OTHER"],
            "providerPublishTime": int(NOW.timestamp()),
        },
    ]
    search = Mock(return_value=Mock(news=items))
    monkeypatch.setattr("src.data.research_fetcher.yf.Search", search)
    stock = fake_stock(statements)
    r = ResearchFetcher(
        tmp_path, use_fmp=False, now=NOW, ticker_factory=lambda t: stock
    ).fetch("TEST")
    assert r["status"]["news_search"] == "ok"
    assert len(r["news"]) == 1
    assert r["news"][0]["title"] == "Company wins contract"
    assert r["news"][0]["categories"] == ["corporate/regulatory"]
