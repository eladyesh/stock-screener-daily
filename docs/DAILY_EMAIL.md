# Daily scan and email in this fork

This is a separate fork of [RyanJHamby/stock-screener](https://github.com/RyanJHamby/stock-screener), initially based on `ee559be62ffc9106d802fcb421957b0d20d6590e`. The upstream MIT license and attribution are retained. It is independent of any skills repository.

## What runs

**Daily Stock Scan and Email** calls `run_optimized_scan.py --conservative --git-storage`. It downloads the Nasdaq Trader symbol directories over HTTPS, uses Yahoo Finance via yfinance, runs upstream trend/relative-strength/fundamental scoring, and generates the upstream text report. An explicit SMTP step sends a readable preview and the complete report as a UTF-8 attachment, including days with zero signals.

The universe uses upstream symbol/name heuristics and liquidity/history filters; it is not literally every listed stock. The report records successfully analyzed stocks, request errors, and price-bar dates. Filtered/missing-data stocks are not negative signals. Fundamentals use the upstream cache rules (typically 7 or 90 days). No paid API or brokerage login is required.

## Schedule

- Every calendar day at **07:17 UTC** (`17 7 * * *`): **10:17 Israel summer / 09:17 winter**.
- Email follows the scan, not exactly at the start time. Full runs can take tens of minutes or longer; the scan step is capped at 160 minutes.
- Weekends/US market holidays can repeat the last trading session's data.
- GitHub schedules can be delayed or dropped under heavy load. The schedule lives on `main`; no local computer needs to stay on.

## One-time email setup

Open **Settings → Secrets and variables → Actions** in this fork. Add repository secrets:

| Secret | Value |
| --- | --- |
| `EMAIL_FROM` | Gmail account used to send the report |
| `EMAIL_TO` | One recipient address |
| `EMAIL_PASSWORD` | An App Password for the sender account |

For Gmail, enable two-step verification and create an [App Password](https://myaccount.google.com/apppasswords). Enter it directly into the secret; never commit it or paste it into a chat. Some account policies do not support App Passwords; see [Google's instructions](https://support.google.com/accounts/answer/185833).

Optional repository **variables** `EMAIL_SMTP_SERVER` and `EMAIL_SMTP_PORT` default to `smtp.gmail.com` and `587`. Port 587 requires verified STARTTLS; 465 uses verified implicit TLS. The sender must be permitted by the SMTP provider.

Secrets do not transfer from the upstream or another repository. Missing secrets fail configuration checking by name. This check does not authenticate; successful delivery means SMTP accepted the message, not proof of inbox placement.

## Enable and verify

1. Enable workflows in **Actions** if GitHub displays the fork's Actions-disabled notice.
2. Select **Daily Stock Scan and Email → Run workflow**, branch `main`.
3. Select `smoke` for five stocks (AAPL, MSFT, NVDA, AMZN, GOOGL). Uncheck `send_email` while credentials are not configured. Smoke checks do not persist to Git and are not full-market scans.
4. After adding secrets, run `full` with email checked and confirm receipt. Smoke emails carry `[TEST]` in the subject.
5. Scheduled runs always use `full` with email enabled, regardless of prior manual inputs.

The job summary, **Email the complete report** step, and `stock-scan-<run>-<attempt>` artifact show outcomes. Full runs commit the cache and dated reports to this fork. Reports retain the original English text. No orders or Slack messages are sent.

## Failure handling

Each attempt removes the previous latest report first. A manifest binds the report checksum to the run ID and attempt. Delivery rejects stale/incomplete reports and scans with zero analyzed stocks. Zero signals with analyzed stocks is valid and is emailed. Elevated errors are flagged in the subject. Price-bar dates distinguish generation time from market-data time; a new report does not guarantee all inputs are current.

Scanning/sending failures mark the workflow failed and preserve available artifacts; no successful email is claimed. Re-running a successfully delivered report can send a duplicate. GitHub may disable public-repository schedules after 60 days without repository activity; full runs normally commit actual new reports/cache updates. See [GitHub's schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

## Checks

```bash
python -m unittest tests.test_daily_delivery -v
pytest tests/ --ignore=tests/test_email_full.py -q
```

Delivery tests use mocked SMTP and synthetic reports to check Unicode, escaping, complete attachments, zero-signal delivery, stale/incorrect-run rejection, TLS-before-login and delivery errors. They do not prove live credentials, market-data availability, or investment performance.
