#!/usr/bin/env python3
"""Deliver the optimized scanner's actual report using authenticated, TLS SMTP."""

import argparse
import html
import os
import re
import smtplib
import ssl
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path

if __package__:
    from .report_utils import load_current_report
else:
    from report_utils import load_current_report


@dataclass(frozen=True)
class MailConfig:
    """SMTP configuration; password is deliberately excluded from repr."""

    sender: str
    recipient: str
    password: str = field(repr=False)
    host: str = 'smtp.gmail.com'
    port: int = 587

    @classmethod
    def from_env(cls):
        """Validate a single explicit recipient and encrypted SMTP settings."""
        names = ('EMAIL_FROM', 'EMAIL_TO', 'EMAIL_PASSWORD')
        missing = [name for name in names if not os.getenv(name, '').strip()]
        if missing:
            raise ValueError('Missing GitHub Actions secrets: ' + ', '.join(missing))
        sender = os.environ['EMAIL_FROM'].strip()
        recipient = os.environ['EMAIL_TO'].strip()
        for address in (sender, recipient):
            if not re.fullmatch(r'[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+', address):
                raise ValueError('EMAIL_FROM and EMAIL_TO must each contain one plain email address')
        host = os.getenv('EMAIL_SMTP_SERVER', 'smtp.gmail.com').strip()
        port = int(os.getenv('EMAIL_SMTP_PORT', '587'))
        if not host or port not in (465, 587):
            raise ValueError('Use a valid SMTP host and TLS port 465 or 587')
        password = os.environ['EMAIL_PASSWORD'].strip()
        if host == 'smtp.gmail.com':
            password = password.replace(' ', '')
        return cls(sender, recipient, password, host, port)



def make_message(config: MailConfig, report: str, manifest: dict, run_url: str) -> EmailMessage:
    """Build a readable preview plus the complete UTF-8 text attachment."""
    date = manifest['generated_at'][:10]
    errors = float(manifest.get('error_rate', 0))
    prefix = '[TEST] ' if os.getenv('SCAN_MODE') == 'smoke' else ''
    warning = ' [PARTIAL: elevated errors]' if errors >= 0.1 else ''
    subject = (f'{prefix}[Stock Screener] {date}{warning} | '
               f"{manifest['buy_signals']} buy / {manifest['sell_signals']} sell signals")
    dates = ', '.join(manifest.get('price_dates', [])) or 'Unavailable'
    intro = (
        f"Generated (UTC): {manifest['generated_at']}\n"
        f"Analyzed: {manifest['analyzed']} of {manifest['universe']} universe entries.\n"
        f"Request error rate: {errors:.1%}. Filtered/missing-data stocks are not analyzed.\n"
        f"Latest price-bar dates: {dates}\n"
        'Weekends/holidays can reuse the last trading session. Fundamental data may be cached.\n'
        'Signals are the upstream algorithm\'s research output, not executed trades.\n'
        f'Run details: {run_url}\n\n'
    )
    preview = report[:9000]
    if len(report) > len(preview):
        preview += '\n\n[Preview shortened. The attached file contains the full report.]'
    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = config.sender
    message['To'] = config.recipient
    message['Date'] = format_datetime(datetime.now(timezone.utc))
    message['Message-ID'] = make_msgid()
    message.set_content(intro + preview)
    message.add_alternative(
        '<html><body><h2>Daily stock screening</h2>'
        '<pre style="white-space:pre-wrap;font-family:monospace">'
        + html.escape(intro + preview) + '</pre></body></html>', subtype='html'
    )
    message.add_attachment(report, subtype='plain', filename=f'stock-scan-{date}.txt')
    return message


def deliver(config: MailConfig, message: EmailMessage) -> None:
    """Require verified TLS before authentication, and fail on refused delivery."""
    context = ssl.create_default_context()
    if config.port == 465:
        connection = smtplib.SMTP_SSL(config.host, config.port, timeout=45, context=context)
    else:
        connection = smtplib.SMTP(config.host, config.port, timeout=45)
    with connection as server:
        server.ehlo()
        if config.port == 587:
            server.starttls(context=context)
            server.ehlo()
        server.login(config.sender, config.password)
        refused = server.send_message(message, from_addr=config.sender, to_addrs=[config.recipient])
        if refused:
            raise RuntimeError('SMTP refused the configured recipient')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-config', action='store_true')
    parser.add_argument('--report', type=Path, default=Path('data/daily_scans/latest_optimized_scan.txt'))
    parser.add_argument('--manifest', type=Path, default=Path('data/daily_scans/latest_scan_manifest.json'))
    args = parser.parse_args()
    try:
        config = MailConfig.from_env()
        if args.check_config:
            print('Email configuration is present and structurally valid; authentication is checked at delivery.')
            return 0
        report, manifest = load_current_report(args.report, args.manifest)
        run_url = (f"{os.getenv('GITHUB_SERVER_URL', 'https://github.com')}/"
                   f"{os.getenv('GITHUB_REPOSITORY', '')}/actions/runs/{os.getenv('GITHUB_RUN_ID', '')}")
        deliver(config, make_message(config, report, manifest, run_url))
        print('SMTP server accepted the daily report for the configured recipient.')
        return 0
    except (ValueError, FileNotFoundError) as exc:
        # These messages are ours, never an SMTP server's response or credentials.
        print(f'Email delivery stopped: {exc}', file=sys.stderr)
    except smtplib.SMTPAuthenticationError:
        print('SMTP authentication failed. Check EMAIL_FROM and the Gmail App Password in EMAIL_PASSWORD.', file=sys.stderr)
    except Exception as exc:
        print(f'Email delivery failed ({type(exc).__name__}); inspect SMTP configuration. No success was recorded.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
