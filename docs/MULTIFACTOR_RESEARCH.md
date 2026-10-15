# Daily research for two holding horizons

The daily scan now prepends two ranked research lists to the existing report:

- `weeks_to_months`: technical trend, earnings revisions and near-term event risk.
- `months_to_18m`: business growth, cash generation, balance sheet and valuation.

These are heuristic research candidates, not calibrated probabilities, price
targets, an auto-trader or a demonstrated profitable strategy. No broker orders
are placed. The daily workflow keeps the existing Telegram destination; tests
and the live-smoke workflow never send notifications.

## Inputs and provenance

`src/data/financial_metrics.py` normalizes quarterly revenue/EPS growth,
operating/net margins, four-quarter free cash flow, net debt, leverage and
diluted share count. Comparisons match fiscal periods, including 52/53-week
years. Missing or negative comparison bases stay unavailable. A transition from
loss to profit is not labelled ordinary percentage EPS growth. TTM calculations
require four consecutive complete quarters; stale balance sheets are not mixed
into a newer quarter. Sequential growth remains visible but is not an automatic
penalty for seasonal companies.

`src/data/research_fetcher.py` enriches a bounded shortlist with Yahoo Finance
data through yfinance:

- Current and next fiscal-year EPS/revenue estimates, forward growth and analyst
  counts; provider-reported 30/90-day EPS estimate revisions.
- Forward P/E, EV/EBITDA and FCF yield. FCF yield requires matching quote and
  statement currencies. Financial Services/Real Estate do not receive generic
  corporate FCF/leverage/EV-EBITDA scoring.
- Latest earnings surprise, next earnings date/range, recent dated headlines,
  filing links and explicit vendor-labelled cash insider purchases. Insider
  grants, gifts and exercises are excluded. Purchase labels are not independently
  Form-4-verified; they and headline classifications are contextual, not automatic
  buy-score bonuses. Management guidance extraction is not automated.
- Each numeric observation has a provider, source URL, retrieval timestamp and
  a financial/forecast period where supplied. Provider failures and absent fields
  are explicit. Retrieval time is not represented as a filing publication time.

The optional **FMP_API_KEY** GitHub Actions secret enables the FMP **stable**
analyst-estimates fallback when Yahoo estimates are missing. This does not prove
subscription entitlement. Failed authorization, unsupported plans and throttling
are reported; no subscription is purchased. FMP calls are bounded by
`FMP_RESEARCH_MAX_CALLS` (default 50/run). No API key appears in stored URLs/logs.
FMP future fiscal-year-end pairs are explicitly dated in provenance. FMP
historical revisions are not fabricated when Yahoo revision data is absent.

References: [yfinance Ticker API](https://ranaroussi.github.io/yfinance/reference/api/yfinance.Ticker.html),
[FMP estimates](https://site.financialmodelingprep.com/developer/docs/stable/financial-estimates).

## Ranking and known scope limits

Edit `research.yaml` to change component weights, thresholds, shortlist size or
the event caution window. Weights must sum to 100 for each profile. Every
component shows its score and observed-data coverage. Unknown metrics earn no
evidence points. Scores are not renormalized upwards to hide absent inputs.

The original 40-point fundamental component now uses actual YoY revenue/EPS,
YoY operating-margin change and inventory growth relative to revenue growth.
The fixed margin bonus and half-score for unknown EPS are removed. Zero EPS
growth remains a real observation. Input scores are bounded.

The default shortlist interleaves the technical signal rank and a broad
fundamental rank (40 names total). Both start from the original liquidity,
price, drawdown and Stage 1/2 filters. The longer horizon can include basing
stocks without a legacy buy signal, but is not a whole-market value screen.
Peer valuations require at least five other same-industry/sector names in this
shortlist and are explicitly labelled as a selection-biased cohort comparison,
not a full-sector fair value. Historical valuation comparison activates after
20 prior daily observations of that multiple; it then compares with their median.
The cache keeps up to 400 daily observations. The first deployment explicitly
reports insufficient history instead of fabricating a historical benchmark.

Candidate gates require fresh dated statements/prices, sufficient data coverage
and the existing market-regime gate. Swing candidates additionally require a
legacy buy signal and a known earnings date outside the default seven-day caution
window. Longer-horizon candidates retain earnings warnings. Unknown dates are
not treated as confirmation that there is no upcoming event. Recent financing
headlines require review instead of automatic candidate status. Thresholds and
weights remain unvalidated assumptions.

US price bars from the current session are excluded before 16:15 New York time;
future bars are excluded. This is conservative for early closes. Broad-screen
fundamentals refresh at most weekly, with schema migration, while shortlisted
research refreshes after 20 hours. Off-cycle earnings can still fall between
refreshes; report dates and status are provided for review.

## Run and verify

```bash
python -m pip install -r requirements.txt
python -m pytest tests/ --ignore=tests/test_email_full.py -q
python run_optimized_scan.py --conservative --git-storage --tickers AAPL,MSFT,NVDA,AMZN,GOOGL --research-limit 5 --output-dir data/smoke
python -m scripts.validate_research_smoke data/smoke/latest_research.json
```

The live check requires actual financial and forward-EPS evidence, not merely a
zero exit code. Market-provider failures can fail this check independently of
offline tests. `.github/workflows/research_smoke.yml` runs this check on relevant
pull requests and manually. It preserves report/log artifacts without sending
messages, changing secrets or committing sample scans. A five-stock check
cannot validate whole-market coverage or runtime.

`--skip-research` preserves a legacy-only diagnostic run. `--output-dir` isolates
test reports. The existing daily workflow runs enrichment automatically and
stores `latest_research.json` plus timestamped snapshots alongside the text report.

## Test investment outcomes prospectively

```bash
python -m scripts.evaluate_research
```

This uses the saved recommendations as observed, with entry at the first trading
session close strictly after the observation and exits 21/63/126 or 126/252/378
sessions later. It compares adjusted-close returns with SPY and the archived
legacy ranking. The assumed round-trip trading cost defaults to 10 basis points
and can be changed with `--cost-bps`. These are overlapping equal-weight
observation cohorts, not portfolio returns; do not add or annualize them.
Missing/delisted prices invalidate a cohort average rather than silently removing
losers. Current data are never backfilled into past fundamentals. A new deployment
will correctly report **zero mature cohorts** until enough time has elapsed.
