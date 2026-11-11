"""Build an auditable common-equity universe from Nasdaq symbol directories."""

import json
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Dict, List

import pandas as pd
import requests

from .security_profiles import SecurityProfileResolver

logger = logging.getLogger(__name__)
UNIVERSE_POLICY_VERSION = "common-equity-v3"


class USStockUniverseFetcher:
    """Select common/ordinary shares, ADRs and common REIT shares.

    Directory ETF/test flags take precedence over security-name classification.
    Ambiguous names require additional operating-company evidence. Unresolved
    instruments remain excluded, and every decision is retained in ``audit``.
    """

    def __init__(self, cache_dir: str = "./data/cache", profile_resolver=None) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # Deliberately ignore legacy pickle caches made by the suffix filter.
        self.cache_file = self.cache_dir / "us_stock_universe.json"
        self.source_status: Dict = {}
        self.audit: Dict = {}
        self.profile_resolver = profile_resolver

    def _fetch_directory(self, filename: str, symbol_column: str) -> pd.DataFrame:
        """Preserve directory fields used to classify securities and audit gaps."""
        try:
            response = requests.get(
                f"https://www.nasdaqtrader.com/dynamic/SymDir/{filename}.txt",
                timeout=45,
            )
            response.raise_for_status()
            df = pd.read_csv(StringIO(response.text), sep="|", dtype=str).fillna("")
            required = {symbol_column, "Security Name", "ETF", "Test Issue"}
            if not required.issubset(df.columns):
                raise ValueError("Missing required symbol-directory fields")
            df = df[~df[symbol_column].str.startswith("File Creation Time")]
            df = df[df[symbol_column].str.strip().ne("")].copy()
            if df.empty:
                raise ValueError("Empty symbol directory")
            df = df.rename(columns={symbol_column: "symbol", "Security Name": "name"})
            df["source"] = filename
            self.source_status[filename] = {"status": "ok", "rows": len(df)}
            return df
        except (requests.RequestException, ValueError, pd.errors.ParserError) as exc:
            logger.error("Could not read %s: %s", filename, exc)
            self.source_status[filename] = {"status": "error", "rows": 0}
            return pd.DataFrame()

    def _fetch_nasdaq_listed(self) -> pd.DataFrame:
        return self._fetch_directory("nasdaqlisted", "Symbol")

    def _fetch_other_listed(self) -> pd.DataFrame:
        return self._fetch_directory("otherlisted", "ACT Symbol")

    @staticmethod
    def _classification(row: Dict) -> str:
        """Return a stable inclusion/exclusion reason, never infer type by suffix."""
        if row.get("Test Issue") == "Y":
            return "test_issue"
        if row.get("Test Issue") != "N":
            return "unknown_test_status"
        if row.get("ETF") == "Y" or row.get("NextShares") == "Y":
            return "exchange_traded_fund"
        if row.get("ETF") != "N":
            return "unknown_etf_status"

        # Nasdaq separates issuer and security description with ' - '.
        # Other-listed names do not consistently provide this delimiter.
        name = str(row.get("name", "")).upper()
        description = name.partition(" - ")[2] or name
        exclusions = (
            (r"\bWARRANTS?\b|\bADW\b", "warrant"),
            (r"\bRIGHTS?\b", "right"),
            (r"\bUNITS?\b", "unit"),
            (r"\bPREFERRED\b|\bPREFERENCE\b|\bPFD\b", "preferred"),
            (r"\bNOTES?\b|\bBONDS?\b|\bDEBENTURES?\b", "debt"),
        )
        for pattern, reason in exclusions:
            if re.search(pattern, description):
                return reason
        # ETF=N also covers closed-end funds; keep an explicit name fallback.
        if re.search(r"\b(?:ETFS?|ETNS?|FUND|PORTFOLIO)\b", name):
            return "fund_by_name"
        if not re.fullmatch(r"[A-Z]{1,5}(?:[.-][A-Z])?", str(row.get("symbol", ""))):
            return "unsupported_symbol"
        if re.search(
            r"\b(?:COMMON|ORDINARY|ORD|CAPITAL)\s+(?:STOCK|SHARES?)\b"
            r"|\bAMERICAN\s+DEPOSIT[AO]RY\s+(?:SHARES?|RECEIPTS?)\b"
            r"|\b(?:ADRS?|ADSS?)\b|\bSHARES?\s+OF\s+BENEFICIAL\s+INTEREST\b"
            r"|\b(?:SUBORDINATE|LIMITED)\s+VOTING\s+SHARES\b"
            r"|\b(?:NEW YORK|NY)\s+REGISTRY\s+SHARES\b"
            r"|\bREGISTERED\s+SHARES\b|\bCLASS\s+[A-Z]\s+SHARES\b"
            r"|\bREIT\b|\bREAL ESTATE INVESTMENT TRUST\b|\bBUSINESS DEVELOPMENT COMPANY\b",
            description,
        ):
            return "common_equity"
        return "ambiguous_security_type"

    def _filter_stocks(self, df: pd.DataFrame) -> pd.DataFrame:
        """Classify each row, normalize share classes for Yahoo, and deduplicate."""
        accepted, decisions, seen = [], [], set()
        for row in df.to_dict("records"):
            original = str(row.get("symbol", "")).strip().upper()
            row["symbol"] = original
            reason = self._classification(row)
            symbol = original.replace(".", "-")
            initial_reason = reason
            profile = None
            if (
                reason == "ambiguous_security_type"
                and self.profile_resolver is not None
            ):
                profile = self.profile_resolver.resolve(
                    symbol, str(row.get("name", ""))
                )
                reason = profile["reason"]
            included = reason in {"common_equity", "profile_supported_equity"}
            if included and symbol in seen:
                reason = "duplicate_symbol"
                included = False
            decisions.append(
                {
                    "source": row.get("source", "unknown"),
                    "directory_symbol": original,
                    "symbol": symbol,
                    "name": str(row.get("name", "")),
                    "included": included,
                    "reason": reason,
                    "directory_reason": initial_reason,
                    **({"profile": profile} if profile is not None else {}),
                }
            )
            if included:
                seen.add(symbol)
                accepted.append({**row, "symbol": symbol})
        self.audit = {
            "policy_version": UNIVERSE_POLICY_VERSION,
            "mode": "exchange_directories",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "sources": self.source_status.copy(),
            "complete": len(self.source_status) == 2
            and all(s["status"] == "ok" for s in self.source_status.values()),
            "included": len(accepted),
            "excluded": len(decisions) - len(accepted),
            "reason_counts": dict(Counter(d["reason"] for d in decisions)),
            "resolved_by_profile": sum(
                d["included"] and d["reason"] == "profile_supported_equity"
                for d in decisions
            ),
            "unresolved_security_types": sum(
                d["reason"] == "ambiguous_security_type"
                or d.get("profile", {}).get("decision") == "unresolved"
                for d in decisions
            ),
            "profile_resolution": (
                self.profile_resolver.statistics()
                if self.profile_resolver is not None
                else {"enabled": False}
            ),
            "decisions": decisions,
        }
        return pd.DataFrame(accepted, columns=df.columns)

    def _read_cache(self) -> Dict:
        """Read a current-policy cache; freshness uses recorded time, not mtime."""
        try:
            cached = json.loads(self.cache_file.read_text(encoding="utf-8"))
            if (
                cached.get("policy_version") != UNIVERSE_POLICY_VERSION
                or not cached.get("audit", {}).get("complete")
                or not isinstance(cached.get("symbols"), list)
                or not all(isinstance(s, str) for s in cached["symbols"])
                or cached.get("count") != len(cached["symbols"])
                or set(cached["audit"].get("sources", {}))
                != {"nasdaqlisted", "otherlisted"}
                or not isinstance(cached["audit"].get("decisions"), list)
            ):
                return {}
            fetched = datetime.fromisoformat(cached["fetch_date"])
            age = datetime.now(timezone.utc) - fetched
            if not timedelta(0) <= age < timedelta(days=1):
                return {}
            return cached
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return {}

    def fetch_universe(self, force_refresh: bool = False) -> List[str]:
        """Fetch eligible symbols; never cache a partial exchange response."""
        cached = {} if force_refresh else self._read_cache()
        if cached:
            self.audit = cached["audit"]
            self.source_status = self.audit["sources"]
            return cached["symbols"]
        self.source_status = {}
        if self.profile_resolver is None:
            self.profile_resolver = SecurityProfileResolver()
        frames = [self._fetch_nasdaq_listed(), self._fetch_other_listed()]
        all_stocks = pd.concat(frames, ignore_index=True)
        filtered = self._filter_stocks(all_stocks)
        symbols = sorted(filtered["symbol"].tolist()) if not filtered.empty else []
        if (
            self.audit["complete"]
            and symbols
            and not self.audit["profile_resolution"].get("deferred")
        ):
            cached = {
                "policy_version": UNIVERSE_POLICY_VERSION,
                "symbols": symbols,
                "count": len(symbols),
                "fetch_date": self.audit["fetched_at"],
                "audit": self.audit,
            }
            temporary = self.cache_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(cached, indent=2), encoding="utf-8")
            temporary.replace(self.cache_file)
        else:
            logger.warning(
                "Incomplete exchange/profile evidence or empty universe; no universe cache written"
            )
        logger.info(
            "Universe: %s symbols; %s", len(symbols), self.audit["reason_counts"]
        )
        return symbols

    def get_universe_info(self) -> Dict:
        """Return metadata only for a fresh, complete, current-policy cache."""
        cached = self._read_cache()
        if not cached:
            return {"cached": False, "count": 0}
        age = datetime.now(timezone.utc) - datetime.fromisoformat(cached["fetch_date"])
        return {
            "cached": True,
            "count": cached["count"],
            "fetch_date": cached["fetch_date"],
            "cache_age_hours": age.total_seconds() / 3600,
            "metadata": cached["audit"],
        }
