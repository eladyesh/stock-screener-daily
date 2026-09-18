# Daily scan and Telegram delivery

This independent fork of [RyanJHamby/stock-screener](https://github.com/RyanJHamby/stock-screener) runs **Daily Stock Scan and Telegram**. The upstream MIT license and attribution are retained. It is separate from any skills repository.

## What you receive

One Telegram message containing a short Hebrew summary and the complete English report as a UTF-8 `.txt` attachment. The summary includes coverage, buy/sell signal counts, leading tickers, data dates and the GitHub run link. The same message carries both the summary and file. Days with no signals still produce a report. No email is sent by this workflow.

The scan calls `run_optimized_scan.py --conservative --git-storage`. It downloads Nasdaq Trader directories over HTTPS, fetches Yahoo Finance data through yfinance and uses upstream trend, relative-strength and fundamental scoring. Symbol/name heuristics and liquidity/history filters mean this is not literally every listed stock. Fundamentals can be cached for 7 or 90 days under upstream rules. Missing values remain unknown; they are not treated as zero growth.

## One-time setup for your private chat

1. Open the official [BotFather](https://t.me/BotFather), send `/newbot`, and choose a name and a unique username ending in `bot`. Save the **bot token** it gives you. This is the bot's credential, not your Telegram account password. [Official instructions](https://core.telegram.org/bots/tutorial#obtain-your-bot-token).
2. Open your newly created bot and press **Start** (or send `/start`). Telegram requires you to contact your bot before it can send you private messages.
3. Obtain your **numeric user/chat ID**. For a private conversation with a bot, this is your Telegram user ID. A convenient optional helper is [@userinfobot](https://t.me/userinfobot): press Start and copy the `Id` it returns. It is a third-party bot, not BotFather; never send it your bot token. Alternatively, use your own bot's official `getUpdates` API and read `message.chat.id` from your own private `/start` message; do not guess from another user's message. [API documentation](https://core.telegram.org/bots/api#getupdates).
4. Open this fork's [Actions secrets](https://github.com/eladyesh/stock-screener-daily/settings/secrets/actions) and add these **repository secrets**:

| Secret | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | The full token returned by BotFather |
| `TELEGRAM_CHAT_ID` | Your positive numeric private-chat ID |

Enter credentials directly into GitHub secrets. They are not committed to the repository. Your `@username`, phone number and bot ID are not substitutes for your chat ID. This configuration deliberately targets a private chat; groups/channels are not enabled.

The previous `EMAIL_*` secrets are not used. No Gmail connection is needed. A bot token can be revoked through BotFather if necessary.

## Schedule

- Every calendar day at **07:17 UTC** (`17 7 * * *`): **10:17 Israel summer / 09:17 winter**.
- Delivery follows the scan. Full scans can take tens of minutes or longer; the scan step is capped at 160 minutes.
- Weekends/US market holidays can repeat the last trading session's prices. A newly generated report does not imply fresh fundamentals.
- GitHub schedules may be delayed. The workflow runs from `main` without a local computer staying on. See [GitHub schedule behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

## Verify the connection

1. Open [Daily Stock Scan and Telegram](https://github.com/eladyesh/stock-screener-daily/actions/workflows/daily_screening_git_storage.yml) and choose **Run workflow**, branch `main`.
2. Select `smoke` to scan AAPL, MSFT, NVDA, AMZN and GOOGL. Leave **Send the report to your private Telegram chat** checked to test the actual connection. The caption starts with `[TEST]`. This is not a market-wide scan.
3. Before secrets are configured, uncheck delivery to test scanning and report generation alone.
4. Confirm that **Send the complete report to Telegram** succeeded and the file arrived in your bot chat. Then run `full` with delivery enabled.

Scheduled runs always use `full` with Telegram delivery enabled. Manual settings do not change the schedule. Full runs commit actual reports and cached fundamentals to the fork. Smoke runs only upload an artifact. The GitHub summary and `stock-scan-<run>-<attempt>` artifact expose scan outcomes, never the bot token or chat ID.

## Failure behavior

The sender validates a fresh report, checksum, run ID/attempt, and a positive analyzed-stock count. Old reports and incomplete scans cannot be delivered. A valid scan with zero signals is delivered. Error rates of at least 10% mark the summary as partial.

Missing secrets fail configuration checking by name. Invalid tokens, an incorrect chat ID, or a blocked bot fail delivery. Press Start in the **new bot**, not only in BotFather. Neither HTTP error text nor token-bearing URLs are logged.

Only explicit Telegram rate-limit rejections are retried, at most twice with bounded waits. Network timeouts have uncertain delivery status and are not retried automatically; inspect the chat before manually re-running to avoid duplicates. A Telegram success response confirms message creation, not that it was read.

Available artifacts remain downloadable when delivery fails. A full scan is preserved in Git before delivery. Public-repository schedules can be disabled by GitHub after 60 days without repository activity; normal full runs commit actual report/cache updates.

## Tests

```bash
python -m pytest tests/ --ignore=tests/test_email_full.py -q
```

Tests mock Telegram HTTP responses to verify the exact recipient, UTF-8 attachment, caption size, no-signal reports, rate limits, rejected credentials, stale report rejection and safe error logging. Live delivery still requires your bot token and chat ID.
