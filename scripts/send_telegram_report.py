#!/usr/bin/env python3
"""Send the current scan as one Telegram document with a Hebrew summary."""

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

if __package__:
    from .report_utils import load_current_report
else:
    from report_utils import load_current_report


class DeliveryError(Exception):
    """A safe error message that never includes the bot token or request URL."""


@dataclass(frozen=True)
class TelegramConfig:
    token: str = field(repr=False)
    chat_id: str = field(repr=False)

    @classmethod
    def from_env(cls):
        names = ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID')
        missing = [name for name in names if not os.getenv(name, '').strip()]
        if missing:
            raise ValueError('Missing GitHub Actions secrets: ' + ', '.join(missing))
        token = os.environ['TELEGRAM_BOT_TOKEN'].strip()
        chat_id = os.environ['TELEGRAM_CHAT_ID'].strip()
        if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token):
            raise ValueError('TELEGRAM_BOT_TOKEN must be the token from BotFather')
        if not re.fullmatch(r'[1-9][0-9]{0,15}', chat_id):
            raise ValueError('TELEGRAM_CHAT_ID must be your numeric private-chat ID')
        return cls(token, chat_id)


def make_caption(report: str, manifest: dict, run_url: str) -> str:
    """Keep the summary inside Telegram's document-caption limit."""
    prefix = '[TEST] בדיקת חמש מניות' if os.getenv('SCAN_MODE') == 'smoke' else 'דוח מניות יומי'
    errors = float(manifest.get('error_rate', 0))
    lines = [
        f"{prefix} — {manifest['generated_at'][:10]}",
        f"נותחו {manifest['analyzed']:,} מתוך {manifest['universe']:,} מניות ברשימת הסריקה.",
        f"איתותי קנייה: {manifest['buy_signals']} | איתותי מכירה: {manifest['sell_signals']}",
        f"שיעור שגיאות: {errors:.1%}",
        'תאריכי נתוני המחיר: ' + ', '.join(manifest.get('price_dates', [])),
    ]
    if errors >= 0.1:
        lines.append('דוח חלקי: שיעור שגיאות גבוה.')
    for side, label in (('BUY', 'איתותי קנייה מובילים'), ('SELL', 'איתותי מכירה מובילים')):
        tickers = re.findall(r'\b' + side + r' #\d+: ([A-Z0-9.^-]{1,16})\s+\|', report)
        if tickers:
            lines.append(label + ': ' + ', '.join(tickers[:5]))
    lines.extend([
        'הקובץ המצורף מכיל את הדוח המלא באנגלית.',
        'אלו איתותי הסורק; נתוני יסוד עשויים להיות שמורים מהרצות קודמות.',
        run_url,
    ])
    # UTF-16 bounding is conservative for Telegram, including supplementary emoji.
    return '\n'.join(lines).encode('utf-16-le')[:2000].decode('utf-16-le', errors='ignore')


def deliver(config: TelegramConfig, report: str, manifest: dict, run_url: str) -> int:
    """Upload once; only retry explicit flood-control rejections, not ambiguous timeouts."""
    document = report.encode('utf-8')
    if len(document) > 49_000_000:
        raise DeliveryError('Report exceeds the Telegram upload size limit')
    filename = f"stock-scan-{manifest['generated_at'][:10]}.txt"
    url = f'https://api.telegram.org/bot{config.token}/sendDocument'
    for attempt in range(3):
        try:
            response = requests.post(
                url,
                data={'chat_id': config.chat_id, 'caption': make_caption(report, manifest, run_url)},
                files={'document': (filename, document, 'text/plain; charset=utf-8')},
                timeout=(10, 60),
                allow_redirects=False,
            )
        except requests.RequestException:
            raise DeliveryError('Telegram network error; delivery status is unknown. Check the chat before retrying.') from None
        try:
            payload = response.json()
        except ValueError:
            raise DeliveryError('Telegram returned an invalid response; no delivery was confirmed') from None
        if not isinstance(payload, dict):
            raise DeliveryError('Telegram returned an unexpected response')
        if response.status_code == 429 or payload.get('error_code') == 429:
            delay = payload.get('parameters', {}).get('retry_after')
            if attempt < 2 and isinstance(delay, int) and 0 < delay <= 30:
                time.sleep(delay)
                continue
            raise DeliveryError('Telegram rate limit reached; retry later')
        if response.status_code in (401, 404) or payload.get('error_code') in (401, 404):
            raise DeliveryError('Telegram rejected TELEGRAM_BOT_TOKEN; check the token from BotFather')
        if response.status_code == 403 or payload.get('error_code') == 403:
            raise DeliveryError('Telegram blocked delivery; open your bot, press Start and ensure it is not blocked')
        if response.status_code != 200 or payload.get('ok') is not True:
            raise DeliveryError('Telegram rejected the report; check TELEGRAM_CHAT_ID and press Start in your bot')
        result = payload.get('result', {})
        chat = result.get('chat', {})
        if (str(chat.get('id')) != config.chat_id or chat.get('type') != 'private'
                or not isinstance(result.get('message_id'), int) or not result.get('document')):
            raise DeliveryError('Telegram response did not confirm a document in the configured private chat')
        return result['message_id']
    raise DeliveryError('Telegram did not confirm delivery')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-config', action='store_true')
    parser.add_argument('--report', type=Path, default=Path('data/daily_scans/latest_optimized_scan.txt'))
    parser.add_argument('--manifest', type=Path, default=Path('data/daily_scans/latest_scan_manifest.json'))
    args = parser.parse_args()
    try:
        config = TelegramConfig.from_env()
        if args.check_config:
            print('Telegram configuration is present; token and chat access are verified during delivery.')
            return 0
        report, manifest = load_current_report(args.report, args.manifest)
        run_url = (f"{os.getenv('GITHUB_SERVER_URL', 'https://github.com')}/"
                   f"{os.getenv('GITHUB_REPOSITORY', '')}/actions/runs/{os.getenv('GITHUB_RUN_ID', '')}")
        deliver(config, report, manifest, run_url)
        print('Telegram confirmed the report document in the configured private chat.')
        return 0
    except (ValueError, FileNotFoundError, DeliveryError) as exc:
        print(f'Telegram delivery stopped: {exc}', file=sys.stderr)
    except Exception as exc:
        # HTTP exception text can contain the token embedded in the URL. Never log it.
        print(f'Telegram delivery failed ({type(exc).__name__}); no delivery was confirmed.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
