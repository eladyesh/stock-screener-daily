# Universe selection and technical risk policy

The daily scanner uses policy versions `common-equity-v2` and
`observed-risk-v2`. Research snapshots now use `multifactor-v2`
because the eligible universe and short-horizon technical gate changed.
Historical files are not rewritten. Forward-evaluation rows retain the strategy
and signal-policy versions so different policies can be compared explicitly.

## Files to read

| File | Responsibility |
| --- | --- |
| `src/data/universe_fetcher.py` | Read exchange directories, classify securities, normalize class symbols, cache and audit decisions |
| `src/screening/signal_policy.py` | Shared thresholds, maximum scores and risk-budget assumptions |
| `src/screening/trade_risk.py` | Derive support stops and prior resistance, apply risk gates, format missing evidence |
| `src/screening/signal_engine.py` | Combine component scores with trend and risk gates |
| `run_optimized_scan.py` | Orchestrate the scan and save the report, manifest and universe audit |
| `tests/test_universe_fetcher.py` / `tests/test_signal_risk.py` | Deterministic regression examples |

## Universe

Source: Nasdaq Trader's
[symbol directories and field definitions](https://www.nasdaqtrader.com/Trader.aspx?id=SymbolDirDefs).

- Explicit ETF, NextShares and test-issue flags take precedence.
- Security descriptions identify common/ordinary shares, ADRs/ADSs, registered
  shares, common REIT shares and common voting classes. Explicit warrants,
  rights, units, preferred shares, debt and named funds are excluded.
- A final W, R or U is not evidence of a derivative: PLTR, MU and SCHW remain
  eligible when their descriptions identify common stock.
- Dot-separated share classes are converted to Yahoo format: BRK.B -> BRK-B.
  The original exchange symbol remains in the audit, including duplicate rows.
- Description classification is a heuristic, not an authoritative security
  master. Ambiguous descriptions are excluded with `ambiguous_security_type`.
  For example, an issuer-only name may omit the share type; this can exclude
  legitimate equities. Review these audit rows rather than silently assuming
  either that every ETF=N entry is common stock or that coverage is exhaustive.
- `latest_universe_audit.json` contains each inclusion/exclusion reason,
  source status, fetch time, counts and policy version. The report/manifest
  distinguish exchange selection from explicit `--tickers` runs and test subsets.
  This audit precedes the separate price/liquidity/data-availability filters.
- An unavailable exchange produces explicit partial coverage and no new cache.
  JSON caches expire 24 hours after their recorded UTC fetch time. Legacy pickle
  caches and incompatible policy versions are ignored.

## Stop and target

For a Stage 2 signal, the candidate stop is the higher valid level below entry of
the last ten bars' lowest low minus 0.5%, and SMA50 minus 1%. If it is closer than
3%, the existing minimum-distance buffer widens it to 3%; this adjustment is named
in the output. Invalid recent lows invalidate the risk assessment.

A support stop more than 10% below entry is preserved and blocks a buy. It is
never moved upward merely to pass the risk budget. This is a nominal stop
distance, not a guarantee of execution or maximum realized loss.

The target is the nearest confirmed prior swing high above entry in the previous
252 bars. A high must exceed the two neighboring highs on each side; all four
neighbors must be valid. The current/entry bar is excluded, including from pivot
confirmation. The target's observed date and basis are retained. Callers must
supply chronological completed bars up to the signal date, not a future history.

This is a historical resistance reference, not an analyst forecast or expected
return. No fixed 20%/30% upside is inserted when resistance is unavailable.

| Risk evidence | Effect |
| --- | --- |
| Invalid stop or stop distance above 10% | Block buy |
| Observed reward/risk below 2 | Block buy |
| Observed reward/risk at least 2 | Eligible; up to 15 R/R points |
| No confirmed higher resistance | Null target and R/R; zero R/R points; other components may still qualify |

Ratios and eligibility use unrounded values. Only display prices are rounded.
Unknown R/R is displayed as unavailable rather than 0:1. The report, manifest and
research snapshots identify the policy; the dashboard preserves share-class
tickers and displays unavailable ratios without a numeric value.

## Scores and validation

The existing buy threshold remains 60/125 (trend 40, fundamentals 40, R/R 15,
relative strength 10, volume 10, entry 5, VCP 5). The sell threshold remains 60/100.
The code and report headings import these values from one module. Passing the
score does not override the trend or risk gates. Research evidence scores have
their own 0–100 scale and configuration.

Run automated tests without the separate live-email script:

```bash
python -m pytest tests/ --ignore=tests/test_email_full.py -q
```

Regression tests cover legitimate ticker suffixes/share classes, excluded
instruments, source outages, cache age/version, new highs without targets,
confirmed pivot timing, wide stops, low R/R vetoes, invalid inputs, and report
serialization/delivery validation. These verify software behavior, not investment
performance. A historical portfolio backtest, position sizing and forward
performance evidence remain separate work.
