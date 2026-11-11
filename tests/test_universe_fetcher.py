"""Exchange-directory regressions; no provider calls in these tests."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pandas as pd
import pytest
import requests

from src.data.universe_fetcher import USStockUniverseFetcher


def row(symbol, name, **fields):
    return {"symbol": symbol, "name": name, "Test Issue": "N", "ETF": "N", **fields}


def test_common_equities_are_not_rejected_by_suffix_or_company_name(tmp_path):
    fetcher = USStockUniverseFetcher(tmp_path)
    frame = pd.DataFrame(
        [
            row("PLTR", "Palantir Technologies Inc. - Class A Common Stock"),
            row("MU", "Micron Technology, Inc. - Common Stock"),
            row("SCHW", "Charles Schwab Corporation Common Stock"),
            row("TRU", "TransUnion Common Stock"),
            row("BRK.B", "Berkshire Hathaway Inc. New Common Stock"),
            row("BF.B", "Brown Forman Inc Class B Common Stock"),
            row("NTRS", "Northern Trust Corporation - Common Stock"),
            row(
                "TSM",
                "Taiwan Semiconductor Manufacturing Company Ltd. American Depositary Shares",
            ),
            row("VNO", "Vornado Realty Trust Common Shares of Beneficial Interest"),
        ]
    )
    selected = fetcher._filter_stocks(frame)
    assert set(selected.symbol) == {
        "PLTR",
        "MU",
        "SCHW",
        "TRU",
        "BRK-B",
        "BF-B",
        "NTRS",
        "TSM",
        "VNO",
    }
    assert fetcher.audit["excluded"] == 0
    assert (
        next(d for d in fetcher.audit["decisions"] if d["symbol"] == "BRK-B")[
            "directory_symbol"
        ]
        == "BRK.B"
    )


@pytest.mark.parametrize(
    "security,reason",
    [
        (row("FND", "Neutral description", ETF="Y"), "exchange_traded_fund"),
        (row("ABC", "Example - Common Stock", **{"Test Issue": "Y"}), "test_issue"),
        (row("ABCW", "Example - Warrants to purchase Common Stock"), "warrant"),
        (row("ABCR", "Example - Rights to receive Common Stock"), "right"),
        (
            row("ABCU", "Example - Units consisting of Common Stock and warrants"),
            "warrant",
        ),
        (row("ABCU", "Example - Units"), "unit"),
        (
            row("ABCPA", "Example - Depositary Shares representing Preferred Stock"),
            "preferred",
        ),
        (row("ABCN", "Example - Senior Notes due 2030"), "debt"),
        (
            row("CEF", "Example Income Fund Common Shares of Beneficial Interest"),
            "fund_by_name",
        ),
        (row("UNK", "Example unspecified instrument"), "ambiguous_security_type"),
        (row("ABC", "Example Common Stock", ETF=""), "unknown_etf_status"),
        (
            row("ABC", "Example Common Stock", **{"Test Issue": ""}),
            "unknown_test_status",
        ),
    ],
)
def test_non_common_and_unknown_instruments_have_auditable_reasons(
    tmp_path, security, reason
):
    fetcher = USStockUniverseFetcher(tmp_path)
    assert fetcher._filter_stocks(pd.DataFrame([security])).empty
    assert fetcher.audit["decisions"][0]["reason"] == reason
    assert fetcher.audit["decisions"][0]["included"] is False


def test_class_normalization_deduplicates_without_losing_audit(tmp_path):
    fetcher = USStockUniverseFetcher(tmp_path)
    selected = fetcher._filter_stocks(
        pd.DataFrame(
            [
                row("BRK.B", "Berkshire Common Stock"),
                row("BRK-B", "Berkshire Common Stock"),
            ]
        )
    )
    assert selected.symbol.tolist() == ["BRK-B"]
    assert fetcher.audit["reason_counts"] == {"common_equity": 1, "duplicate_symbol": 1}


@pytest.fixture
def directory_get(monkeypatch):
    nasdaq = (
        "Symbol|Security Name|Test Issue|ETF|NextShares\n"
        "PLTR|Palantir - Class A Common Stock|N|N|N\n"
        "QQQ|Invesco QQQ Trust|N|Y|N\n"
        "ZTEST|Test - Common Stock|Y|N|N\n"
        "File Creation Time: 11112026||||\n"
    )
    other = (
        "ACT Symbol|Security Name|Test Issue|ETF\n"
        "BRK.B|Berkshire Hathaway Common Stock|N|N\n"
        "File Creation Time: 11112026|||\n"
    )

    def response(url, **kwargs):
        return Mock(text=nasdaq if url.endswith("nasdaqlisted.txt") else other)

    get = Mock(side_effect=response)
    monkeypatch.setattr("src.data.universe_fetcher.requests.get", get)
    return get


def test_directory_parsing_cache_and_audit_round_trip(tmp_path, directory_get):
    fetcher = USStockUniverseFetcher(tmp_path)
    assert fetcher.fetch_universe() == ["BRK-B", "PLTR"]
    assert fetcher.audit["complete"] is True
    assert fetcher.audit["excluded"] == 2
    assert len(fetcher.audit["decisions"]) == 4
    cached = USStockUniverseFetcher(tmp_path)
    assert cached.fetch_universe() == ["BRK-B", "PLTR"]
    assert directory_get.call_count == 2
    assert cached.audit == fetcher.audit
    assert cached.get_universe_info()["count"] == 2


@pytest.mark.parametrize(
    "fault", ["old_time", "future_time", "old_policy", "malformed"]
)
def test_cache_freshness_and_policy_do_not_trust_file_mtime(
    tmp_path, directory_get, fault
):
    fetcher = USStockUniverseFetcher(tmp_path)
    fetcher.fetch_universe()
    cached = json.loads(fetcher.cache_file.read_text())
    if fault == "old_time":
        cached["fetch_date"] = (
            datetime.now(timezone.utc) - timedelta(days=2)
        ).isoformat()
    elif fault == "future_time":
        cached["fetch_date"] = (
            datetime.now(timezone.utc) + timedelta(days=2)
        ).isoformat()
    elif fault == "old_policy":
        cached["policy_version"] = "suffix-v1"
    fetcher.cache_file.write_text(
        "{corrupt" if fault == "malformed" else json.dumps(cached)
    )
    assert fetcher.fetch_universe() == ["BRK-B", "PLTR"]
    assert directory_get.call_count == 4


def test_legacy_pickle_is_not_loaded(tmp_path, directory_get):
    (tmp_path / "us_stock_universe.pkl").write_bytes(b"obsolete or untrusted pickle")
    assert USStockUniverseFetcher(tmp_path).fetch_universe() == ["BRK-B", "PLTR"]
    assert directory_get.call_count == 2


@pytest.mark.parametrize("bad_source", ["network", "schema"])
def test_partial_exchange_coverage_is_explicit_and_not_cached(
    tmp_path, directory_get, bad_source
):
    good_response = directory_get.side_effect

    def response(url, **kwargs):
        if url.endswith("otherlisted.txt"):
            if bad_source == "network":
                raise requests.Timeout("test timeout")
            return Mock(text="ACT Symbol|Security Name\nABC|Common Stock\n")
        return good_response(url, **kwargs)

    directory_get.side_effect = response
    fetcher = USStockUniverseFetcher(tmp_path)
    assert fetcher.fetch_universe() == ["PLTR"]
    assert fetcher.audit["complete"] is False
    assert fetcher.audit["sources"]["otherlisted"]["status"] == "error"
    assert not fetcher.cache_file.exists()
    fetcher.fetch_universe()
    assert directory_get.call_count == 4


def test_total_provider_failure_returns_empty_auditable_universe(
    tmp_path, directory_get
):
    directory_get.side_effect = requests.Timeout("test timeout")
    fetcher = USStockUniverseFetcher(tmp_path)
    assert fetcher.fetch_universe() == []
    assert fetcher.audit["complete"] is False
    assert not fetcher.cache_file.exists()


@pytest.mark.parametrize(
    "symbol,name",
    [
        ("GOOG", "Alphabet Inc. - Class C Capital Stock"),
        ("ASML", "ASML Holding N.V. - New York Registry Shares"),
        ("API", "Agora, Inc. - ADS"),
        ("NBTX", "Nanobiotix S.A. - ADSs"),
        ("SHOP", "Shopify Inc. - Class A Subordinate Voting Shares"),
        ("LOGI", "Logitech International S.A. - Registered Shares"),
        ("GOOD", "Gladstone Commercial Corporation - Real Estate Investment Trust"),
        ("GAIN", "Gladstone Investment Corporation - Business Development Company"),
    ],
)
def test_alternative_equity_descriptions_seen_in_live_directories(
    tmp_path, symbol, name
):
    fetcher = USStockUniverseFetcher(tmp_path)
    assert fetcher._filter_stocks(
        pd.DataFrame([row(symbol, name)])
    ).symbol.tolist() == [symbol]


@pytest.mark.parametrize(
    "symbol,name",
    [
        ("PSNYW", "Polestar Automotive Holding UK Limited - Class C-1 ADS (ADW)"),
        ("AFJKR", "Aimei Health Technology Co., Ltd - Right"),
        ("CDZIP", "Cadiz, Inc. - Depositary Shares"),
        (
            "FNGD",
            "MicroSectors FANG Index -3X Inverse Leveraged ETNs due January 8, 2038",
        ),
    ],
)
def test_derivative_and_unspecified_depositary_descriptions_are_not_equity(
    tmp_path, symbol, name
):
    fetcher = USStockUniverseFetcher(tmp_path)
    assert fetcher._filter_stocks(pd.DataFrame([row(symbol, name)])).empty


def test_issuer_only_description_remains_explicitly_unclassified(tmp_path):
    fetcher = USStockUniverseFetcher(tmp_path)
    assert fetcher._filter_stocks(
        pd.DataFrame([row("TSM", "Taiwan Semiconductor Manufacturing Company Ltd.")])
    ).empty
    assert fetcher.audit["reason_counts"] == {"ambiguous_security_type": 1}
