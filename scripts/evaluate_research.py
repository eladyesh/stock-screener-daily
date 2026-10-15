"""Forward observation cohorts, using only archived recommendations and later closes.

This is not a historical fundamental backtest or a simulated portfolio. Daily
cohorts overlap; their returns must not be added or annualized as a strategy.
"""

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from src.data.financial_metrics import completed_daily_bars

HORIZONS = {"weeks_to_months": (21, 63, 126), "months_to_18m": (126, 252, 378)}


def cohort_returns(snapshot, histories, horizon, profile, cost_bps=10):
    """Enter at the first session close strictly after observation; match exit dates."""
    observed = (
        datetime.fromisoformat(snapshot["generated_at"])
        .astimezone(ZoneInfo("America/New_York"))
        .date()
    )
    benchmark = histories.get("SPY", pd.Series(dtype=float)).dropna().sort_index()
    sessions = benchmark[[d.date() > observed for d in benchmark.index]]
    if len(sessions) <= horizon:
        return {"status": "not_mature", "horizon_sessions": horizon}
    entry, exit_date = sessions.index[0], sessions.index[horizon]
    benchmark_return = float(sessions.iloc[horizon] / sessions.iloc[0] - 1)

    def returns(tickers):
        values, missing = {}, []
        for ticker in tickers:
            series = histories.get(ticker, pd.Series(dtype=float))
            if (
                entry not in series.index
                or exit_date not in series.index
                or pd.isna(series.get(entry))
                or pd.isna(series.get(exit_date))
                or series[entry] <= 0
            ):
                missing.append(ticker)
                continue
            values[ticker] = float(
                series[exit_date] / series[entry] - 1 - cost_bps / 10000
            )
        # Never improve the result by silently dropping delisted/missing names.
        mean = sum(values.values()) / len(values) if values and not missing else None
        return {
            "individual_returns": values,
            "missing": missing,
            "equal_weight_return": mean,
            "excess_vs_spy": mean - benchmark_return if mean is not None else None,
        }

    return {
        "status": "mature",
        "horizon_sessions": horizon,
        "entry_date": str(entry.date()),
        "exit_date": str(exit_date.date()),
        "spy_return": benchmark_return,
        "round_trip_cost_bps": cost_bps,
        "research": returns(snapshot["rankings"][profile]),
        "legacy_baseline": returns(snapshot.get("technical_baseline", [])),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("data/daily_scans"))
    parser.add_argument(
        "--output", type=Path, default=Path("data/daily_scans/forward_evaluation.json")
    )
    parser.add_argument("--cost-bps", type=float, default=10)
    args = parser.parse_args()
    if not 0 <= args.cost_bps <= 1000:
        parser.error("cost-bps must be between 0 and 1000")
    now = datetime.now(timezone.utc)
    snapshots = []
    seen = set()
    for path in sorted(args.directory.glob("research_*.json")):
        s = json.loads(path.read_text())
        # One observation per UTC day/version to avoid double-counting reruns.
        key = (s["generated_at"][:10], s["strategy_version"])
        if key not in seen:
            snapshots.append(s)
            seen.add(key)
    eligible = [
        s
        for s in snapshots
        if (now - datetime.fromisoformat(s["generated_at"])).days >= 21
    ]
    histories = {}
    if eligible:
        tickers = {"SPY"}
        for s in eligible:
            tickers.update(s.get("technical_baseline", []))
            for names in s["rankings"].values():
                tickers.update(names)
        start = min(s["generated_at"][:10] for s in eligible)
        for ticker in sorted(tickers):
            try:
                frame = completed_daily_bars(
                    yf.Ticker(ticker).history(start=start, auto_adjust=True), now
                )
                if not frame.empty:
                    close = frame["Close"].copy()
                    close.index = pd.to_datetime([x.date() for x in close.index])
                    histories[ticker] = close
            except Exception:
                pass  # Explicit missing prices, not zero returns or survivor-only averages.
    records = []
    for s in snapshots:
        for profile, horizons in HORIZONS.items():
            for horizon in horizons:
                records.append(
                    {
                        "observed_at": s["generated_at"],
                        "profile": profile,
                        **cohort_returns(s, histories, horizon, profile, args.cost_bps),
                    }
                )
    output = {
        "generated_at": now.isoformat(),
        "snapshots": len(snapshots),
        "mature_cohorts": sum(r["status"] == "mature" for r in records),
        "method": "Next-session adjusted-close entry; fixed trading-session horizon; SPY and legacy comparison; cost assumption in bps",
        "limitations": "Overlapping observation cohorts, not portfolio returns. No historical backfill, no stop/position simulation; missing/delisted prices invalidate cohort averages.",
        "cohorts": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, allow_nan=False))
    print(
        f"Saved {args.output}: {output['mature_cohorts']} mature cohorts from {len(snapshots)} snapshots"
    )


if __name__ == "__main__":
    main()
