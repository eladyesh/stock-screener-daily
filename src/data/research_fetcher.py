"""Bounded shortlist enrichment with provenance, independent failures and daily cache."""

import json
import logging
import os
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import numpy as np
import requests
import yfinance as yf

from .financial_metrics import growth, number, statement_metrics

logger = logging.getLogger(__name__)


def json_safe(value):
    """Replace non-finite cached numbers recursively before strict JSON storage."""
    if isinstance(value, np.generic):
        return json_safe(value.item())
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, float):
        return number(value)
    return value


def utc_date(value):
    """Parse a provider timestamp without treating unknown dates as today."""
    try:
        stamp = pd.Timestamp(value)
        if pd.isna(stamp):
            return None
        return stamp.date()
    except (TypeError, ValueError, OverflowError):
        return None


def cell(frame, row, column):
    if (
        isinstance(frame, pd.DataFrame)
        and row in frame.index
        and column in frame.columns
    ):
        return number(frame.loc[row, column])
    return None


def parse_news(items, today):
    """Classify dated headlines for review; keywords are not return predictions."""
    output, seen = [], set()
    categories = {
        "financing/dilution": r"\b(offering|dilution|convertible|capital raise)\b",
        "earnings/guidance": r"\b(earnings|guidance|outlook|results)\b",
        "corporate/regulatory": r"\b(acquisition|merger|contract|approval|lawsuit|investigation)\b",
    }
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        item = item.get("content", item)
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        link = item.get("canonicalUrl") or item.get("clickThroughUrl") or {}
        url = link.get("url", "") if isinstance(link, dict) else ""
        url = url or item.get("link", "")
        published = item.get("pubDate")
        if not published and item.get("providerPublishTime"):
            published = datetime.fromtimestamp(
                item["providerPublishTime"], timezone.utc
            ).isoformat()
        day = utc_date(published)
        if (
            not title
            or not url.startswith("https://")
            or title in seen
            or day is None
            or not 0 <= (today - day).days <= 14
        ):
            continue
        seen.add(title)
        output.append(
            {
                "title": title,
                "url": url,
                "published_at": str(published),
                "categories": [
                    key
                    for key, pattern in categories.items()
                    if re.search(pattern, title, re.I)
                ],
            }
        )
    return output[:8]


def parse_insiders(frame, today):
    """Keep only explicit vendor-labelled cash purchases; exclude grants/exercises."""
    output = []
    if not isinstance(frame, pd.DataFrame):
        return output
    for _, row in frame.iterrows():
        description = str(row.get("Text", ""))
        day = utc_date(row.get("Start Date"))
        value = number(row.get("Value"))
        if day is None or not 0 <= (today - day).days <= 90:
            continue
        if not re.match(r"^purchase\b", description, re.I) or re.search(
            r"grant|award|exercise|option|gift", description, re.I
        ):
            continue
        if value is None or value <= 0:
            continue
        output.append(
            {
                "name": str(row.get("Insider", "")),
                "date": day.isoformat(),
                "value": value,
                "description": description,
                "verification": "Yahoo transaction label; Form 4 not independently verified",
            }
        )
    return output


class ResearchFetcher:
    """Fetch optional evidence once per ticker/day and stop on provider throttling."""

    def __init__(
        self,
        cache_dir="data/research_cache",
        use_fmp=True,
        now=None,
        ticker_factory=None,
    ):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.now = now or datetime.now(timezone.utc)
        self.ticker_factory = ticker_factory or yf.Ticker
        self.fmp_key = os.getenv("FMP_API_KEY", "") if use_fmp else ""
        self.fmp_calls = 0
        self.fmp_limit = int(os.getenv("FMP_RESEARCH_MAX_CALLS", "50"))
        self.yahoo_blocked = False
        self.fmp_blocked = False

    def fetch(self, ticker, fallback=None, refresh=False):
        """Return a JSON-safe snapshot. Failures remain visible, never fabricated."""
        if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", ticker):
            raise ValueError("Invalid research ticker")
        path = self.cache_dir / f"{ticker}.json"
        if path.exists() and not refresh:
            try:
                cached = json.loads(path.read_text())
                age = (
                    self.now - datetime.fromisoformat(cached["retrieved_at"])
                ).total_seconds()
                if (
                    cached.get("schema_version") == 1
                    and 0 <= age < 20 * 3600
                    and cached.get("status", {}).get("info") == "ok"
                ):
                    return cached
            except (ValueError, KeyError, TypeError):
                pass
        result = {
            "schema_version": 1,
            "ticker": ticker,
            "retrieved_at": self.now.isoformat(),
            "metrics": {},
            "sources": {},
            "status": {},
            "news": [],
            "insider_purchases": [],
            "filings": [],
            "warnings": [],
            "sector": "Unknown",
            "industry": "Unknown",
        }
        stock = self.ticker_factory(ticker)
        root = f"https://finance.yahoo.com/quote/{ticker}"

        def read(name, method, default):
            if self.yahoo_blocked:
                result["status"][name] = "rate_limited"
                return default
            try:
                value = method()
                expected = (dict, list) if name == "filings" else type(default)
                if value is not None and not isinstance(value, expected):
                    result["status"][name] = "invalid_schema"
                    return default
                empty = (
                    value is None
                    or (hasattr(value, "empty") and value.empty)
                    or (isinstance(value, (dict, list)) and not value)
                )
                result["status"][name] = "missing" if empty else "ok"
                return default if empty else value
            except Exception as exc:
                if type(exc).__name__ == "YFRateLimitError":
                    self.yahoo_blocked = True
                    result["status"][name] = "rate_limited"
                else:
                    result["status"][name] = "unavailable"
                # No raw exceptions/URLs: provider request URLs may contain secrets.
                logger.warning(
                    "%s research %s: %s", ticker, name, result["status"][name]
                )
                return default

        def put(
            key, value, section, period=None, provider="Yahoo Finance", retrieved=None
        ):
            result["metrics"][key] = number(value)
            if number(value) is not None:
                result["sources"][key] = {
                    "provider": provider,
                    "url": root + section,
                    "retrieved_at": retrieved or self.now.isoformat(),
                    "period": period,
                }

        income = read("income", lambda: stock.quarterly_income_stmt, pd.DataFrame())
        balance = read("balance", lambda: stock.quarterly_balance_sheet, pd.DataFrame())
        cashflow = read("cashflow", lambda: stock.quarterly_cashflow, pd.DataFrame())
        statements = statement_metrics(income, balance, cashflow)
        if not statements:
            statements = fallback or {}
            result["warnings"].append(
                "Fresh statements unavailable; fallback statement dates retained"
            )
        result["statements"] = statements
        for key, value in statements.items():
            if (
                key not in ("schema_version", "inventory_breakdown_available")
                and number(value) is not None
            ):
                put(
                    key,
                    value,
                    "/financials/",
                    statements.get("report_period"),
                    retrieved=statements.get("fetch_date") or "unknown",
                )

        info = read("info", stock.get_info, {})
        result["sector"] = info.get("sector", "Unknown")
        result["industry"] = info.get("industry", "Unknown")
        result["currency"] = info.get("currency")
        result["financial_currency"] = info.get("financialCurrency")
        for target, source in (
            ("market_cap", "marketCap"),
            ("enterprise_value", "enterpriseValue"),
            ("forward_pe", "forwardPE"),
            ("trailing_pe", "trailingPE"),
            ("ev_ebitda", "enterpriseToEbitda"),
        ):
            put(target, info.get(source), "/key-statistics/")
        if info.get("currency") and info.get("currency") == info.get(
            "financialCurrency"
        ):
            fcf, cap = number(statements.get("free_cash_flow_ttm")), number(
                info.get("marketCap")
            )
            if fcf is not None and cap and cap > 0:
                put(
                    "fcf_yield",
                    fcf / cap * 100,
                    "/key-statistics/",
                    statements.get("report_period"),
                )
        else:
            result["warnings"].append(
                "FCF yield omitted: quote/statement currency match unverified"
            )

        earnings = read("estimates", stock.get_earnings_estimate, pd.DataFrame())
        revenue = read("revenue_estimates", stock.get_revenue_estimate, pd.DataFrame())
        for period, label in (("0y", "fy0"), ("+1y", "fy1")):
            put(f"eps_{label}", cell(earnings, period, "avg"), "/analysis/", period)
            put(f"revenue_{label}", cell(revenue, period, "avg"), "/analysis/", period)
            put(
                f"analysts_{label}",
                cell(earnings, period, "numberOfAnalysts"),
                "/analysis/",
                period,
            )
        put(
            "forward_eps_growth",
            growth(result["metrics"].get("eps_fy1"), result["metrics"].get("eps_fy0")),
            "/analysis/",
            "+1y vs 0y",
        )
        put(
            "forward_revenue_growth",
            growth(
                result["metrics"].get("revenue_fy1"),
                result["metrics"].get("revenue_fy0"),
            ),
            "/analysis/",
            "+1y vs 0y",
        )
        trend = read("revisions", stock.get_eps_trend, pd.DataFrame())
        for days in (30, 90):
            put(
                f"eps_revision_{days}d",
                growth(
                    cell(trend, "+1y", "current"), cell(trend, "+1y", f"{days}daysAgo")
                ),
                "/analysis/",
                "+1y",
            )
        history = read("earnings_history", stock.get_earnings_history, pd.DataFrame())
        if isinstance(history, pd.DataFrame) and not history.empty:
            eligible = [
                d
                for d in history.index
                if utc_date(d) and 0 <= (self.now.date() - utc_date(d)).days <= 180
            ]
            if eligible:
                last = max(eligible)
                put(
                    "eps_surprise_pct",
                    growth(
                        cell(history, last, "epsActual"),
                        cell(history, last, "epsEstimate"),
                    ),
                    "/analysis/",
                    str(last),
                )

        calendar = read("calendar", stock.get_calendar, {})
        dates = sorted(
            {
                d
                for d in (utc_date(x) for x in calendar.get("Earnings Date", []))
                if d is not None and d >= self.now.date()
            }
        )
        result["earnings_dates"] = [d.isoformat() for d in dates]
        result["earnings_date_status"] = "provider_estimate" if dates else "unknown"
        if dates:
            put("days_to_earnings", (dates[0] - self.now.date()).days, "/calendar/")
        news = read("news", lambda: stock.get_news(count=10), [])
        if not news and not self.yahoo_blocked:
            # The ticker stream can be empty while the documented search feed works.
            # Keep only explicit symbol matches, not arbitrary text search results.
            found = read(
                "news_search",
                lambda: yf.Search(
                    ticker, max_results=0, news_count=10, lists_count=0
                ).news,
                [],
            )
            news = [
                item
                for item in found
                if isinstance(item, dict) and ticker in item.get("relatedTickers", [])
            ]
        result["news"] = parse_news(news, self.now.date())
        result["insider_purchases"] = parse_insiders(
            read("insiders", stock.get_insider_transactions, pd.DataFrame()),
            self.now.date(),
        )
        result["insider_source"] = root + "/insider-transactions/"
        filings = read("filings", stock.get_sec_filings, [])
        if isinstance(filings, dict):
            filings = filings.get("filings", [])
        for item in filings if isinstance(filings, list) else []:
            if not isinstance(item, dict):
                continue
            day = utc_date(item.get("date"))
            url = item.get("edgarUrl") or item.get("url", "")
            if (
                day is not None
                and 0 <= (self.now.date() - day).days <= 90
                and url.startswith("https://")
            ):
                result["filings"].append(
                    {"date": day.isoformat(), "type": item.get("type"), "url": url}
                )
        if result["metrics"].get("eps_fy1") is None:
            self._fmp_estimates(ticker, result)
        result["fmp_configured"] = bool(self.fmp_key)
        self._valuation_history(ticker, result)
        result = json_safe(result)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(result, indent=2, allow_nan=False, default=str))
        temp.replace(path)
        return result

    def _valuation_history(self, ticker, result):
        """Compare only previously observed daily multiples; do not invent history."""
        folder = self.cache_dir / "valuation_history"
        folder.mkdir(exist_ok=True)
        path = folder / f"{ticker}.json"
        try:
            history = json.loads(path.read_text()) if path.exists() else []
            if not isinstance(history, list):
                history = []
        except (ValueError, OSError):
            history = []
        today = self.now.date().isoformat()
        # One observation per date; ignore future/invalid entries if a clock changes.
        prior = {
            r["date"]: r
            for r in history
            if isinstance(r, dict) and utc_date(r.get("date")) and r["date"] < today
        }
        record = {"date": today}
        for key in ("forward_pe", "ev_ebitda"):
            current = number(result["metrics"].get(key))
            if current is None or current <= 0:
                continue
            record[key] = current
            values = [number(r.get(key)) for r in prior.values()]
            values = [v for v in values if v is not None and v > 0]
            result.setdefault("valuation_history", {})[key] = {
                "prior_observations": len(values),
                "required": 20,
            }
            if len(values) >= 20:
                median = statistics.median(values)
                derived = key + "_historical_relative"
                result["metrics"][derived] = current / median
                result["valuation_history"][key]["median"] = median
                result["sources"][derived] = {
                    "provider": "Derived from archived daily Yahoo observations",
                    "url": f"https://finance.yahoo.com/quote/{ticker}/key-statistics/",
                    "retrieved_at": self.now.isoformat(),
                    "period": [min(prior), max(prior)],
                }
        if len(record) > 1:
            prior[today] = record
            temp = path.with_suffix(".tmp")
            temp.write_text(
                json.dumps([prior[d] for d in sorted(prior)[-400:]], allow_nan=False)
            )
            temp.replace(path)

    def _fmp_estimates(self, ticker, result):
        """Optional stable API fallback; never imply access from an API key alone."""
        if not self.fmp_key:
            result["status"]["fmp_estimates"] = "not_configured"
            return
        if self.fmp_blocked or self.fmp_calls >= self.fmp_limit:
            result["status"]["fmp_estimates"] = "budget_or_provider_limit"
            return
        endpoint = "https://financialmodelingprep.com/stable/analyst-estimates"
        self.fmp_calls += 1
        try:
            response = requests.get(
                endpoint,
                params={
                    "symbol": ticker,
                    "period": "annual",
                    "page": 0,
                    "limit": 10,
                    "apikey": self.fmp_key,
                },
                timeout=(5, 15),
            )
            if response.status_code in (401, 402, 403, 429):
                self.fmp_blocked = True
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError("Unexpected schema")
            rows = sorted(
                [
                    r
                    for r in rows
                    if isinstance(r, dict)
                    and utc_date(r.get("date"))
                    and utc_date(r["date"]) >= self.now.date()
                ],
                key=lambda r: r["date"],
            )
            # Only complete same-provider consecutive fiscal-year pairs enter growth.
            # Do not combine a Yahoo year with a different FMP fiscal-year definition.
            if (
                len(rows) < 2
                or not 330
                <= (utc_date(rows[1]["date"]) - utc_date(rows[0]["date"])).days
                <= 400
            ):
                result["status"]["fmp_estimates"] = "incomplete_fiscal_pair"
                return
            for row, label in zip(rows[:2], ("fy0", "fy1")):
                for metric, field in (
                    ("eps", "epsAvg"),
                    ("revenue", "revenueAvg"),
                    ("analysts", "numAnalystsEps"),
                ):
                    key = f"{metric}_{label}"
                    result["sources"].pop(key, None)
                    result["metrics"][key] = number(row.get(field))
                    if result["metrics"][key] is not None:
                        result["sources"][key] = {
                            "provider": "FMP",
                            "url": endpoint + f"?symbol={ticker}&period=annual",
                            "retrieved_at": self.now.isoformat(),
                            "period": row["date"],
                        }
            for metric in ("eps", "revenue"):
                key = f"forward_{metric}_growth"
                result["sources"].pop(key, None)
                result["metrics"][key] = growth(
                    result["metrics"].get(f"{metric}_fy1"),
                    result["metrics"].get(f"{metric}_fy0"),
                )
                if result["metrics"][key] is not None:
                    result["sources"][key] = {
                        "provider": "FMP",
                        "url": endpoint + f"?symbol={ticker}&period=annual",
                        "retrieved_at": self.now.isoformat(),
                        "period": [r["date"] for r in rows[:2]],
                    }
            result["status"]["fmp_estimates"] = "ok" if rows else "missing"
        except (requests.RequestException, ValueError, TypeError):
            result["status"]["fmp_estimates"] = "unavailable"
