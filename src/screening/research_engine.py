"""Transparent, coverage-aware research ranks for two investment horizons."""

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

import yaml

from src.data.financial_metrics import number
from src.data.research_fetcher import ResearchFetcher, utc_date, json_safe
from .fundamental_score import scale, score_fundamentals

GROUPS = {"technical", "growth", "quality", "valuation", "revisions", "catalysts"}


def load_config(path="research.yaml"):
    """Reject mistyped weights rather than silently changing the strategy."""
    config = yaml.safe_load(Path(path).read_text())
    for name, weights in config["profiles"].items():
        if (
            set(weights) != GROUPS
            or any(number(x) is None or x < 0 for x in weights.values())
            or sum(weights.values()) != 100
        ):
            raise ValueError(f"Invalid research weights for {name}")
    if set(config["profiles"]) != {"weeks_to_months", "months_to_18m"}:
        raise ValueError("Both documented research horizons are required")
    if not 1 <= config["shortlist_limit"] <= 100 or not 1 <= config["top_n"] <= 50:
        raise ValueError("Research shortlist/top_n out of range")
    if (
        not 0 <= config["minimum_score"] <= 100
        or not 0 <= config["minimum_coverage"] <= 1
    ):
        raise ValueError("Invalid research thresholds")
    return config


def group_score(items):
    """Unobserved inputs earn zero evidence points and reduce coverage."""
    if not items:
        return {"score": 0.0, "coverage": 0.0, "inputs": {}}
    valid = [v for v in items.values() if number(v) is not None]
    return {
        "score": round(sum(valid) / len(items), 2),
        "coverage": len(valid) / len(items),
        "inputs": items,
    }


def technical_score(analysis):
    """A broad trend score also covers basing stocks for the longer horizon."""
    phase = analysis.get("phase_info", {})
    prices = analysis["price_data"]["Close"].dropna()
    current = number(analysis.get("current_price"))
    sma50, sma200 = number(phase.get("sma_50")), number(phase.get("sma_200"))
    rs = analysis.get("rs_series")
    rs_change = None
    if rs is not None:
        rs = rs.dropna()
        if len(rs) >= 21 and rs.iloc[-21] > 0:
            rs_change = (rs.iloc[-1] / rs.iloc[-21] - 1) * 100
    return group_score(
        {
            "above_200d": (
                scale((current / sma200 - 1) * 100, -15, 15)
                if current and sma200 and sma200 > 0
                else None
            ),
            "trend_50_vs_200": (
                scale((sma50 / sma200 - 1) * 100, -10, 10)
                if sma50 and sma200 and sma200 > 0
                else None
            ),
            "momentum_63d": (
                scale((prices.iloc[-1] / prices.iloc[-64] - 1) * 100, -15, 25)
                if len(prices) >= 64 and prices.iloc[-64] > 0
                else None
            ),
            "relative_strength_change": scale(rs_change, -10, 10),
        }
    )


def select_candidates(analyses, signals, limit):
    """Interleave technical and fundamental ranks so longer-term ideas can enter."""
    eligible = [a for a in analyses if a["phase_info"].get("phase") in (1, 2)]
    by_ticker = {a["ticker"]: a for a in eligible}
    technical = [
        by_ticker[s["ticker"]]
        for s in sorted(signals, key=lambda s: (-s["score"], s["ticker"]))
        if s["ticker"] in by_ticker
    ]
    technical += sorted(
        eligible, key=lambda a: (-technical_score(a)["score"], a["ticker"])
    )
    fundamental = sorted(
        eligible,
        key=lambda a: (
            -score_fundamentals(a.get("quarterly_data"))["score"],
            a["ticker"],
        ),
    )
    chosen = {}
    for left, right in zip(technical, fundamental):
        for item in (left, right):
            if len(chosen) < limit:
                chosen.setdefault(item["ticker"], item)
    return list(chosen.values())


def add_peer_context(records):
    """Use a disclosed shortlist comparison, never label it a whole-sector median."""
    for record in records:
        m = record["metrics"]
        for key in ("forward_pe", "ev_ebitda"):
            own = number(m.get(key))
            if own is None or own <= 0:
                continue
            for grouping in ("industry", "sector"):
                peers = [
                    number(r["metrics"].get(key))
                    for r in records
                    if r["ticker"] != record["ticker"]
                    and r.get(grouping) == record.get(grouping)
                    and r.get(grouping) not in (None, "Unknown")
                ]
                peers = [v for v in peers if v is not None and v > 0]
                if len(peers) >= 5:
                    median = statistics.median(peers)
                    m[key + "_relative"] = own / median
                    record.setdefault("peer_comparison", {})[key] = {
                        "grouping": grouping,
                        "count": len(peers),
                        "median": median,
                        "scope": "enriched shortlist only, selection-biased",
                    }
                    record["sources"][key + "_relative"] = {
                        "provider": "Derived shortlist comparison",
                        "retrieved_at": record["retrieved_at"],
                        "period": "current",
                        "url": record.get("sources", {}).get(key, {}).get("url"),
                    }
                    break


def score_record(analysis, record, signal, config, now, allow_buys=True):
    """Keep risk flags and data coverage separate from uncalibrated ranking points."""
    m = record["metrics"]

    def metric(key, low, high):
        return scale(m.get(key), low, high)

    quality = {
        "net_margin": metric("net_margin", -5, 25),
        "dilution": metric("shares_yoy_change", 10, -3),
    }
    if record.get("sector") not in ("Financial Services", "Real Estate"):
        quality.update(
            {
                "fcf_margin": metric("fcf_margin", -5, 25),
                "net_leverage": metric("net_debt_to_ebitda", 5, 0),
            }
        )
    valuation = {
        "forward_pe": (
            metric("forward_pe", 60, 10)
            if number(m.get("forward_pe")) is not None and m["forward_pe"] > 0
            else None
        )
    }
    if record.get("sector") not in ("Financial Services", "Real Estate"):
        valuation["fcf_yield"] = metric("fcf_yield", 0, 8)
        valuation["ev_ebitda"] = (
            metric("ev_ebitda", 40, 8)
            if number(m.get("ev_ebitda")) is not None and m["ev_ebitda"] > 0
            else None
        )
    for key in ("forward_pe", "ev_ebitda"):
        relative = metric(key + "_relative", 1.75, 0.75)
        if key in valuation and valuation[key] is not None and relative is not None:
            valuation[key] = (valuation[key] + relative) / 2
        historical = metric(key + "_historical_relative", 1.75, 0.75)
        if key in valuation and valuation[key] is not None and historical is not None:
            valuation[key] = (valuation[key] + historical) / 2
    groups = {
        "technical": technical_score(analysis),
        "growth": group_score(
            {
                "revenue_yoy": metric("revenue_yoy_change", 0, 30),
                "eps_yoy": metric("eps_yoy_change", -20, 60),
                "forward_revenue": metric("forward_revenue_growth", 0, 25),
                "forward_eps": metric("forward_eps_growth", 0, 30),
            }
        ),
        "quality": group_score(quality),
        "valuation": group_score(valuation),
        "revisions": group_score(
            {
                "eps_30d": metric("eps_revision_30d", -5, 5),
                "eps_90d": metric("eps_revision_90d", -10, 10),
            }
        ),
        "catalysts": group_score(
            {"earnings_surprise": metric("eps_surprise_pct", -10, 10)}
        ),
    }
    risks = list(record.get("warnings", []))
    price_date = utc_date(analysis["price_data"].index[-1])
    period = utc_date(record.get("statements", {}).get("report_period"))
    retrieved = utc_date(record.get("statements", {}).get("fetch_date"))
    data_fresh = (
        price_date is not None
        and 0 <= (now.date() - price_date).days <= config["max_price_age_days"]
    )
    data_fresh &= (
        period is not None
        and 0 <= (now.date() - period).days <= config["max_statement_age_days"]
    )
    data_fresh &= retrieved is not None and 0 <= (now.date() - retrieved).days <= 7
    if not data_fresh:
        risks.append("Price/statement evidence is stale, future-dated or undated")
    days = number(m.get("days_to_earnings"))
    near_earnings = days is not None and 0 <= days <= config["earnings_caution_days"]
    if near_earnings:
        risks.append(
            f"Earnings within {days:.0f} days (provider estimate; verify date)"
        )
    elif days is None:
        risks.append("Next earnings date unknown")
    if number(m.get("shares_yoy_change")) is not None and m["shares_yoy_change"] > 5:
        risks.append("Share count increased more than 5% YoY")
    if number(m.get("net_debt_to_ebitda")) is not None and m["net_debt_to_ebitda"] > 4:
        risks.append("Net debt exceeds 4x EBITDA")
    financing_news = any(
        "financing/dilution" in n["categories"] for n in record.get("news", [])
    )
    if financing_news:
        risks.append("Recent financing headline requires human verification")
    if not allow_buys:
        risks.append("Existing market-regime filter blocks new entries")
    profiles = {}
    for name, weights in config["profiles"].items():
        score = sum(weights[k] * groups[k]["score"] / 100 for k in weights)
        coverage = sum(weights[k] * groups[k]["coverage"] / 100 for k in weights)
        status = "watchlist"
        if not data_fresh or coverage < config["minimum_coverage"]:
            status = "insufficient_data"
        elif score >= config["minimum_score"] and allow_buys and not financing_news:
            if name == "weeks_to_months":
                if signal.get("is_buy") and days is not None and not near_earnings:
                    status = "candidate"
            elif (
                groups["quality"]["coverage"] >= 0.5
                and groups["quality"]["score"] >= 40
            ):
                status = "candidate"
        profiles[name] = {
            "score": round(score, 2),
            "coverage": round(coverage, 3),
            "status": status,
        }
    return {
        "ticker": analysis["ticker"],
        "price": float(analysis["current_price"]),
        "price_date": str(price_date),
        "phase": analysis["phase_info"]["phase"],
        "profiles": profiles,
        "components": groups,
        "risks": risks,
        "research": record,
        "technical_signal": {
            k: signal.get(k)
            for k in ("is_buy", "score", "stop_loss", "risk_reward_ratio")
        },
    }


def build_research(
    analyses,
    buy_signals,
    allow_buys=True,
    config_path="research.yaml",
    fetcher=None,
    limit=None,
    now=None,
):
    """Enrich a bounded union of candidates and score every selected name twice."""
    now = now or datetime.now(timezone.utc)
    config = load_config(config_path)
    cap = config["shortlist_limit"] if limit is None else limit
    if not 1 <= cap <= 100:
        raise ValueError("Research limit must be 1..100")
    selected = select_candidates(analyses, buy_signals, cap)
    fetcher = fetcher or ResearchFetcher(now=now)
    records = [
        fetcher.fetch(a["ticker"], fallback=a.get("quarterly_data")) for a in selected
    ]
    add_peer_context(records)
    signals = {s["ticker"]: s for s in buy_signals}
    rows = [
        score_record(a, r, signals.get(a["ticker"], {}), config, now, allow_buys)
        for a, r in zip(selected, records)
    ]
    rankings = {
        name: [
            r["ticker"]
            for r in sorted(
                rows, key=lambda r: (-r["profiles"][name]["score"], r["ticker"])
            )
            if r["profiles"][name]["status"] == "candidate"
        ][: config["top_n"]]
        for name in config["profiles"]
    }
    return {
        "schema_version": 1,
        "strategy_version": "multifactor-v1",
        "generated_at": now.isoformat(),
        "scope": "Liquidity/drawdown-filtered Stage 1/2 shortlist; not an unbiased whole-market rank",
        "score_meaning": "Heuristic research evidence, not probability or expected return; not backtested",
        "config": config,
        "enriched": len(rows),
        "rankings": rankings,
        "rows": rows,
        "technical_baseline": [s["ticker"] for s in buy_signals[: config["top_n"]]],
        "fmp_calls": fetcher.fmp_calls,
    }


def format_research(report):
    """Readable daily summary followed by auditable evidence for the shortlist."""
    lines = [
        "MULTI-FACTOR RESEARCH — TWO HOLDING HORIZONS",
        report["scope"],
        report["score_meaning"],
        f"Enriched: {report['enriched']} | FMP calls: {report['fmp_calls']}",
        "News/insider transactions are review context, not automatic buy points.",
    ]
    for name, tickers in report["rankings"].items():
        lines.append(
            f"\n{name}: "
            + (
                ", ".join(tickers)
                if tickers
                else "No candidates meet evidence/risk thresholds"
            )
        )
    for row in report["rows"]:
        lines.append(
            f"\n{row['ticker']} | ${row['price']:.2f} | price date {row['price_date']} | phase {row['phase']}"
        )
        for name, p in row["profiles"].items():
            lines.append(
                f"  {name}: {p['score']:.1f}/100 | data coverage {p['coverage']:.0%} | {p['status']}"
            )
        lines.append(
            "  Components: "
            + "; ".join(
                f"{k} {v['score']:.0f}/100 ({v['coverage']:.0%} covered)"
                for k, v in row["components"].items()
            )
        )
        for risk in row["risks"]:
            lines.append("  Review: " + risk)
        record = row["research"]
        for key in (
            "revenue_yoy_change",
            "eps_yoy_change",
            "fcf_margin",
            "net_debt_to_ebitda",
            "shares_yoy_change",
            "forward_pe",
            "fcf_yield",
            "forward_eps_growth",
            "eps_revision_30d",
            "eps_revision_90d",
            "days_to_earnings",
        ):
            value = number(record["metrics"].get(key))
            src = record["sources"].get(key, {})
            lines.append(
                f"  {key}: {value:.2f} | {src.get('provider')} | period {src.get('period')} | fetched {src.get('retrieved_at')}"
                if value is not None
                else f"  {key}: N/A"
            )
        for url in sorted(
            {s["url"] for s in record["sources"].values() if s.get("url")}
        ):
            lines.append("  Source: " + url)
        lines.append(
            "  Provider status: "
            + ", ".join(f"{k}={v}" for k, v in record["status"].items())
        )
        for key, context in record.get("valuation_history", {}).items():
            lines.append(
                f"  {key} history: {context['prior_observations']} prior daily observations (20 required)"
            )
        for item in record.get("news", [])[:3]:
            lines.append(
                f"  News ({item['published_at']}): {item['title']} | {item['url']}"
            )
        for item in record.get("insider_purchases", [])[:3]:
            lines.append(
                f"  Insider cash purchase label: {item['name']} | {item['date']} | value {item['value']:,.0f} | {item['verification']} | {record['insider_source']}"
            )
        for item in record.get("filings", [])[:3]:
            lines.append(f"  Filing: {item['type']} | {item['date']} | {item['url']}")
    return "\n".join(lines)


def save_research(report, directory):
    """Save exact observations for later forward evaluation; never backfill history."""
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        json_safe(report), indent=2, ensure_ascii=False, allow_nan=False
    )
    (folder / "latest_research.json").write_text(payload, encoding="utf-8")
    stamp = report["generated_at"].replace(":", "").replace("+", "_")
    (folder / f"research_{stamp}.json").write_text(payload, encoding="utf-8")
