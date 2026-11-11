"""Secondary classification must recover issuer-only equities without admitting funds."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pandas as pd
import pytest

from src.data.security_profiles import SecurityProfileResolver, classify_profile
from src.data.universe_fetcher import USStockUniverseFetcher

NOW = datetime(2026, 11, 11, 10, tzinfo=timezone.utc)


def operating_profile(
    symbol="TSM", name="Taiwan Semiconductor Manufacturing Company Limited"
):
    return {
        "symbol": symbol,
        "quoteType": "EQUITY",
        "longName": name,
        "shortName": name,
        "sector": "Technology",
        "industry": "Semiconductors",
        "fullTimeEmployees": 1000,
        "marketCap": 10_000_000_000,
        "sharesOutstanding": 100_000_000,
        "longBusinessSummary": f"{name} manufactures and sells products.",
    }


def resolver(tmp_path, profiles=None, **kwargs):
    profiles = profiles or {"TSM": operating_profile()}
    kwargs.setdefault("request_interval_seconds", 0)
    factory = Mock(
        side_effect=lambda symbol: Mock(get_info=Mock(return_value=profiles[symbol]))
    )
    return (
        SecurityProfileResolver(tmp_path, ticker_factory=factory, now=NOW, **kwargs),
        factory,
    )


@pytest.mark.parametrize(
    "symbol,name,long_name",
    [
        (
            "TSM",
            "Taiwan Semiconductor Manufacturing Company Ltd.",
            "Taiwan Semiconductor Manufacturing Company Limited",
        ),
        ("V", "Visa Inc.", "Visa Inc."),
        ("T", "AT&T Inc.", "AT&T Inc."),
        ("MO", "Altria Group, Inc.", "Altria Group, Inc."),
        ("NEM", "Newmont Corporation", "Newmont Corporation"),
    ],
)
def test_issuer_only_names_need_and_can_pass_company_evidence(symbol, name, long_name):
    profile = operating_profile(symbol, long_name)
    assert classify_profile(symbol, name, profile)["decision"] == "equity"
    assert (
        classify_profile(symbol, name, {"symbol": symbol, "quoteType": "EQUITY"})[
            "decision"
        ]
        == "unresolved"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("marketCap", None),
        ("marketCap", float("inf")),
        ("marketCap", 0),
        ("sharesOutstanding", None),
        ("fullTimeEmployees", None),
        ("fullTimeEmployees", True),
        ("sector", ""),
        ("industry", None),
        ("longBusinessSummary", ""),
    ],
)
def test_partial_or_invalid_profiles_do_not_resolve_ambiguity(field, value):
    profile = {**operating_profile(), field: value}
    assert (
        classify_profile(
            "TSM", "Taiwan Semiconductor Manufacturing Company Ltd.", profile
        )["decision"]
        == "unresolved"
    )


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"symbol": "OTHER"}, "profile_symbol_mismatch"),
        ({"longName": "Unrelated Company Inc."}, "profile_name_mismatch"),
        ({"quoteType": "ETF"}, "profile_non_equity"),
        ({"category": "Equity Income"}, "profile_fund"),
        ({"fundFamily": "Example Fund Manager"}, "profile_fund"),
        (
            {
                "longBusinessSummary": "Example Trust is a closed-ended equity mutual fund."
            },
            "profile_fund",
        ),
        ({"longName": "Example PARRS A 2029"}, "profile_non_common_instrument"),
        (
            {"shortName": "Example - Depositary Shares"},
            "unspecified_depositary_security",
        ),
    ],
)
def test_funds_debt_and_wrong_profiles_never_resolve_as_common_equity(changes, reason):
    decision = classify_profile(
        "TSM",
        "Taiwan Semiconductor Manufacturing Company Ltd.",
        {**operating_profile(), **changes},
    )
    assert decision["decision"] != "equity"
    assert decision["reason"] == reason


def test_closed_end_fund_mislabelled_equity_is_not_admitted():
    profile = operating_profile("BDJ", "BlackRock Enhanced Equity Dividend Trust")
    profile.update(
        fullTimeEmployees=None,
        longBusinessSummary="BlackRock Enhanced Equity Dividend Trust is a closed-ended equity mutual fund.",
    )
    assert (
        classify_profile("BDJ", profile["longName"], profile)["reason"]
        == "profile_fund"
    )


def test_ambiguous_depositary_shares_cannot_borrow_the_issuers_profile():
    profile = operating_profile("CDZIP", "Cadiz Inc.")
    profile["shortName"] = "Cadiz, Inc. - Depositary Shares"
    assert (
        classify_profile("CDZIP", "Cadiz, Inc. - Depositary Shares", profile)[
            "decision"
        ]
        == "unresolved"
    )


def test_profile_cache_retains_provenance_and_avoids_repeated_calls(tmp_path):
    lookup, factory = resolver(tmp_path)
    name = "Taiwan Semiconductor Manufacturing Company Ltd."
    first = lookup.resolve("TSM", name)
    second = lookup.resolve("TSM", name)
    assert first["decision"] == second["decision"] == "equity"
    assert first["cached"] is False and second["cached"] is True
    assert second["fetched_at"] == NOW.isoformat()
    assert second["source_url"] == "https://finance.yahoo.com/quote/TSM/profile/"
    assert factory.call_count == 1
    assert lookup.statistics()["cache_hits"] == 1


@pytest.mark.parametrize(
    "fault", ["old", "future", "new_name", "bad_json", "bad_schema"]
)
def test_stale_mismatched_or_corrupt_profiles_are_refetched(tmp_path, fault):
    lookup, factory = resolver(tmp_path)
    name = "Taiwan Semiconductor Manufacturing Company Ltd."
    lookup.resolve("TSM", name)
    path = tmp_path / "TSM.json"
    record = json.loads(path.read_text())
    if fault in {"old", "future"}:
        record["fetched_at"] = (
            NOW + timedelta(days=-8 if fault == "old" else 1)
        ).isoformat()
    elif fault == "new_name":
        record["directory_name"] = "Previous issuer"
    elif fault == "bad_schema":
        record["schema_version"] = 0
    path.write_text("corrupt" if fault == "bad_json" else json.dumps(record))
    assert lookup.resolve("TSM", name)["cached"] is False
    assert factory.call_count == 2


def test_provider_failure_stops_new_requests_and_does_not_poison_cache(tmp_path):
    lookup, factory = resolver(tmp_path)
    factory.side_effect = RuntimeError("provider unavailable")
    assert (
        lookup.resolve("TSM", "Taiwan Semiconductor Manufacturing Company Ltd.")[
            "status"
        ]
        == "provider_unavailable"
    )
    assert lookup.resolve("V", "Visa Inc.")["status"] == "provider_blocked"
    assert factory.call_count == 1
    assert not list(tmp_path.glob("*.json"))


def test_request_budget_defers_new_names_but_still_uses_cached_evidence(tmp_path):
    lookup, factory = resolver(tmp_path, max_lookups=1)
    name = "Taiwan Semiconductor Manufacturing Company Ltd."
    lookup.resolve("TSM", name)
    assert lookup.resolve("V", "Visa Inc.")["status"] == "budget_exhausted"
    assert lookup.resolve("TSM", name)["cached"]
    assert factory.call_count == 1


def test_elapsed_time_budget_prevents_another_provider_call(tmp_path):
    times = iter([0, 0, 181, 181])
    lookup, factory = resolver(tmp_path, clock=lambda: next(times))
    lookup.resolve("TSM", "Taiwan Semiconductor Manufacturing Company Ltd.")
    assert lookup.resolve("V", "Visa Inc.")["status"] == "budget_exhausted"
    assert factory.call_count == 1


def test_invalid_symbols_cannot_be_used_as_cache_paths(tmp_path):
    lookup, factory = resolver(tmp_path)
    with pytest.raises(ValueError):
        lookup.resolve("../TSM", "Name")
    factory.assert_not_called()


def test_universe_uses_secondary_evidence_only_for_ambiguous_types(tmp_path):
    lookup, factory = resolver(tmp_path / "profiles")
    fetcher = USStockUniverseFetcher(tmp_path / "universe", profile_resolver=lookup)
    rows = pd.DataFrame(
        [
            {
                "symbol": "TSM",
                "name": "Taiwan Semiconductor Manufacturing Company Ltd.",
                "ETF": "N",
                "Test Issue": "N",
            },
            {
                "symbol": "PLTR",
                "name": "Palantir - Common Stock",
                "ETF": "N",
                "Test Issue": "N",
            },
            {"symbol": "QQQ", "name": "ETF issuer", "ETF": "Y", "Test Issue": "N"},
            {
                "symbol": "ABCR",
                "name": "Example - Rights",
                "ETF": "N",
                "Test Issue": "N",
            },
        ]
    )
    assert set(fetcher._filter_stocks(rows)["symbol"]) == {"TSM", "PLTR"}
    assert factory.call_count == 1
    assert fetcher.audit["resolved_by_profile"] == 1
    assert fetcher.audit["unresolved_security_types"] == 0
    decision = next(d for d in fetcher.audit["decisions"] if d["symbol"] == "TSM")
    assert decision["directory_reason"] == "ambiguous_security_type"
    assert decision["profile"]["evidence"]["quoteType"] == "EQUITY"
    assert decision["profile"]["fetched_at"] == NOW.isoformat()


def test_unavailable_profiles_keep_unknown_security_out_and_prevent_complete_cache(
    tmp_path, monkeypatch
):
    lookup, factory = resolver(tmp_path / "profiles")
    factory.side_effect = RuntimeError("offline")
    fetcher = USStockUniverseFetcher(tmp_path / "universe", profile_resolver=lookup)
    frame = pd.DataFrame(
        [
            {
                "symbol": "TSM",
                "name": "Taiwan Semiconductor Manufacturing Company Ltd.",
                "ETF": "N",
                "Test Issue": "N",
            },
            {
                "symbol": "PLTR",
                "name": "Palantir - Common Stock",
                "ETF": "N",
                "Test Issue": "N",
            },
        ]
    )

    def directory(source):
        fetcher.source_status[source] = {"status": "ok", "rows": len(frame)}
        return frame.copy()

    monkeypatch.setattr(
        fetcher, "_fetch_nasdaq_listed", lambda: directory("nasdaqlisted")
    )
    monkeypatch.setattr(
        fetcher, "_fetch_other_listed", lambda: directory("otherlisted")
    )
    assert fetcher.fetch_universe() == ["PLTR"]
    assert fetcher.audit[
        "complete"
    ]  # Exchange completeness is separate from type evidence.
    assert fetcher.audit["unresolved_security_types"] == 2
    assert fetcher.audit["profile_resolution"]["deferred"] == 2
    assert not fetcher.cache_file.exists()


def test_missing_profile_does_not_block_other_symbols(tmp_path):
    profiles = {"MISS": {}, "TSM": operating_profile()}
    lookup, factory = resolver(tmp_path, profiles)
    assert lookup.resolve("MISS", "Missing Inc.")["status"] == "invalid_profile"
    assert (
        lookup.resolve("TSM", "Taiwan Semiconductor Manufacturing Company Ltd.")[
            "decision"
        ]
        == "equity"
    )
    assert factory.call_count == 2
    assert not lookup.blocked
    assert not (tmp_path / "MISS.json").exists()


def test_nonfinite_profile_fields_are_unresolved_without_blocking_other_symbols(
    tmp_path,
):
    bad = {**operating_profile("BAD", "Bad Inc."), "marketCap": float("nan")}
    lookup, factory = resolver(tmp_path, {"BAD": bad, "TSM": operating_profile()})
    result = lookup.resolve("BAD", "Bad Inc.")
    assert result["decision"] == "unresolved"
    assert result["evidence"]["marketCap"] is None
    assert (
        lookup.resolve("TSM", "Taiwan Semiconductor Manufacturing Company Ltd.")[
            "decision"
        ]
        == "equity"
    )
    assert not lookup.blocked


def test_unresolved_cached_profiles_are_retried_after_one_day(tmp_path):
    incomplete = {**operating_profile(), "fullTimeEmployees": None}
    lookup, factory = resolver(tmp_path, {"TSM": incomplete})
    name = "Taiwan Semiconductor Manufacturing Company Ltd."
    assert lookup.resolve("TSM", name)["decision"] == "unresolved"
    fresh_factory = Mock(
        return_value=Mock(get_info=Mock(return_value=operating_profile()))
    )
    fresh = SecurityProfileResolver(
        tmp_path, ticker_factory=fresh_factory, now=NOW + timedelta(days=2)
    )
    assert fresh.resolve("TSM", name)["decision"] == "equity"
    assert fresh_factory.call_count == 1


def test_new_profile_requests_are_paced_but_cache_hits_are_free(tmp_path):
    pause = Mock()
    profiles = {"TSM": operating_profile(), "V": operating_profile("V", "Visa Inc.")}
    lookup, factory = resolver(
        tmp_path, profiles, request_interval_seconds=1, sleep=pause
    )
    name = "Taiwan Semiconductor Manufacturing Company Ltd."
    lookup.resolve("TSM", name)
    lookup.resolve("TSM", name)
    pause.assert_not_called()
    lookup.resolve("V", "Visa Inc.")
    pause.assert_called_once_with(1)
    assert factory.call_count == 2
