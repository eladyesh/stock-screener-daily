"""Conservative secondary evidence for directory entries without a share type."""

import json
import logging
import math
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Optional

import yfinance as yf

logger = logging.getLogger(__name__)
PROFILE_SCHEMA_VERSION = 1
PROFILE_FIELDS = (
    "symbol",
    "quoteType",
    "longName",
    "shortName",
    "sector",
    "industry",
    "fullTimeEmployees",
    "marketCap",
    "sharesOutstanding",
    "category",
    "fundFamily",
    "legalType",
    "longBusinessSummary",
)


def positive_number(value: object) -> bool:
    """Reject missing, nonfinite, boolean and nonpositive financial fields."""
    if isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (ValueError, TypeError, OverflowError):
        return False


def _issuer_words(name: str) -> set:
    name = name.partition(" - ")[0]
    words = set(re.findall(r"[a-z0-9]+", name.lower()))
    return words - {
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "company",
        "co",
        "ltd",
        "limited",
        "plc",
        "the",
        "new",
        "nv",
        "sa",
    }


def classify_profile(symbol: str, directory_name: str, profile: Dict) -> Dict:
    """Require operating-company evidence; EQUITY alone also describes funds/debt."""
    result = {"decision": "unresolved", "reason": "profile_incomplete"}
    if profile.get("symbol") != symbol:
        return {**result, "reason": "profile_symbol_mismatch"}
    quote_type = profile.get("quoteType")
    if quote_type and quote_type != "EQUITY":
        return {"decision": "excluded", "reason": "profile_non_equity"}
    if quote_type != "EQUITY":
        return result
    names = " ".join(str(profile.get(k) or "") for k in ("longName", "shortName"))
    instrument = directory_name + " " + names
    if re.search(
        r"\b(?:warrants?|rights?|units?|preferred|preference|pfd|notes?|bonds?|"
        r"debentures?|PARRS|ZONES|STRATS|CorTS|certificates?)\b",
        instrument,
        re.I,
    ):
        return {"decision": "excluded", "reason": "profile_non_common_instrument"}
    # Generic depositary shares can be preferred securities with the issuer's
    # operating-company profile. Only explicit American receipts are eligible.
    if re.search(r"\bdeposit[ao]ry\s+shares?\b", instrument, re.I) and not re.search(
        r"\bAmerican\s+deposit[ao]ry\s+(?:shares?|receipts?)\b", instrument, re.I
    ):
        return {**result, "reason": "unspecified_depositary_security"}
    summary = str(profile.get("longBusinessSummary") or "")
    if (
        any(profile.get(k) for k in ("category", "fundFamily", "legalType"))
        or re.search(r"\b(?:ETFS?|ETNS?|fund)\b", names, re.I)
        or re.search(
            r"\b(?:closed[- ]end(?:ed)?|mutual|exchange[- ]traded)"
            r"[\w\s-]{0,40}\bfunds?\b",
            summary,
            re.I,
        )
    ):
        return {"decision": "excluded", "reason": "profile_fund"}
    issuer = _issuer_words(directory_name)
    provider = _issuer_words(str(profile.get("longName") or ""))
    if (
        not issuer
        or not provider
        or len(issuer & provider) / len(issuer | provider) < 0.6
    ):
        return {**result, "reason": "profile_name_mismatch"}
    if (
        not summary.strip()
        or not all(
            isinstance(profile.get(k), str) and profile[k].strip()
            for k in ("sector", "industry")
        )
        or not all(
            positive_number(profile.get(k))
            for k in ("fullTimeEmployees", "marketCap", "sharesOutstanding")
        )
    ):
        return result
    return {"decision": "equity", "reason": "profile_supported_equity"}


class SecurityProfileResolver:
    """Resolve ambiguous names with dated cached evidence and a request budget.

    The time budget is checked between provider calls; an in-flight yfinance
    call uses its own transport timeout. Stop new calls after a provider error.
    """

    def __init__(
        self,
        cache_dir: str = "data/universe_profiles",
        ticker_factory: Optional[Callable] = None,
        max_lookups: int = 100,
        time_budget_seconds: float = 180,
        request_interval_seconds: float = 1,
        now: Optional[datetime] = None,
        clock: Callable = time.monotonic,
        sleep: Callable = time.sleep,
    ) -> None:
        if (
            not 0 <= max_lookups <= 200
            or not 0 < time_budget_seconds <= 600
            or not 0 <= request_interval_seconds <= 10
        ):
            raise ValueError("Invalid security-profile request budget")
        self.cache_dir = Path(cache_dir)
        self.ticker_factory = ticker_factory or yf.Ticker
        self.max_lookups = max_lookups
        self.time_budget_seconds = time_budget_seconds
        self.request_interval_seconds = request_interval_seconds
        self.now = now or datetime.now(timezone.utc)
        self.clock = clock
        self.sleep = sleep
        self.started = None
        self.calls = 0
        self.cache_hits = 0
        self.blocked = False
        self.deferred = 0

    def _cached(self, path: Path, symbol: str, name: str) -> Optional[Dict]:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            age = self.now - datetime.fromisoformat(record["fetched_at"])
            if (
                record["schema_version"] == PROFILE_SCHEMA_VERSION
                and record["symbol"] == symbol
                and record["directory_name"] == name
                and isinstance(record["profile"], dict)
                and timedelta(0)
                <= age
                < timedelta(
                    days=(
                        1
                        if classify_profile(symbol, name, record["profile"])["decision"]
                        == "unresolved"
                        else 7
                    )
                )
            ):
                return record
        except (OSError, ValueError, TypeError, KeyError):
            pass
        return None

    def resolve(self, symbol: str, directory_name: str) -> Dict:
        """Return an auditable decision; failures never count as positive evidence."""
        if not re.fullmatch(r"[A-Z]{1,5}(?:-[A-Z])?", symbol):
            raise ValueError("Invalid profile symbol")
        source = {
            "source": "Yahoo Finance via yfinance",
            "source_url": f"https://finance.yahoo.com/quote/{symbol}/profile/",
        }
        path = self.cache_dir / f"{symbol}.json"
        cached = self._cached(path, symbol, directory_name)
        if cached is not None:
            self.cache_hits += 1
            return {
                **source,
                **classify_profile(symbol, directory_name, cached["profile"]),
                "status": "ok",
                "fetched_at": cached["fetched_at"],
                "cached": True,
                "evidence": cached["profile"],
            }
        if self.started is None:
            self.started = self.clock()
        if (
            self.calls
            and not self.blocked
            and self.calls < self.max_lookups
            and self.clock() - self.started < self.time_budget_seconds
            and self.request_interval_seconds
        ):
            self.sleep(self.request_interval_seconds)
        if (
            self.blocked
            or self.calls >= self.max_lookups
            or self.clock() - self.started >= self.time_budget_seconds
        ):
            self.deferred += 1
            return {
                **source,
                "decision": "unresolved",
                "reason": "profile_deferred",
                "status": "provider_blocked" if self.blocked else "budget_exhausted",
            }
        self.calls += 1
        try:
            raw = self.ticker_factory(symbol).get_info()
        except Exception as exc:
            logger.warning(
                "Security profile unavailable for %s (%s)", symbol, type(exc).__name__
            )
            self.blocked = True
            self.deferred += 1
            return {
                **source,
                "decision": "unresolved",
                "reason": "profile_unavailable",
                "status": "provider_unavailable",
            }
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("quoteType"), str)
            or not raw["quoteType"]
        ):
            self.deferred += 1
            return {
                **source,
                "decision": "unresolved",
                "reason": "profile_incomplete",
                "status": "invalid_profile",
            }
        # Invalid evidence for one symbol must not poison the rest of the batch.
        # Retain only fields used by the classifier and keep JSON numbers finite.
        profile = {
            key: raw.get(key) if isinstance(raw.get(key), str) else None
            for key in PROFILE_FIELDS
        }
        for key in ("fullTimeEmployees", "marketCap", "sharesOutstanding"):
            profile[key] = float(raw[key]) if positive_number(raw.get(key)) else None
        record = {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "symbol": symbol,
            "directory_name": directory_name,
            "fetched_at": self.now.isoformat(),
            "profile": profile,
        }
        # A cache-write problem must not discard valid provider evidence.
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(record, indent=2), encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            logger.warning(
                "Could not cache profile for %s (%s)", symbol, type(exc).__name__
            )
        return {
            **source,
            **classify_profile(symbol, directory_name, profile),
            "status": "ok",
            "fetched_at": record["fetched_at"],
            "cached": False,
            "evidence": profile,
        }

    def statistics(self) -> Dict:
        """Expose provider gaps and costs separately from exchange availability."""
        return {
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "deferred": self.deferred,
            "provider_blocked": self.blocked,
        }
