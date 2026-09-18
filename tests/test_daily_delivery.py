"""Fresh report delivery checks; never contact SMTP or market APIs."""
import hashlib
import json
import os
import smtplib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.send_daily_report import MailConfig, deliver, load_current_report, make_message


class DailyDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.report_path = Path(self.directory.name) / 'report.txt'
        self.manifest_path = Path(self.directory.name) / 'manifest.json'
        self.report = '<script>untrusted company name</script>\nאפס איתותים\nEND OF SCAN'
        self.report_path.write_text(self.report, encoding='utf-8')
        self.manifest = {
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'github_run_id': '123', 'github_run_attempt': '2',
            'report_sha256': hashlib.sha256(self.report_path.read_bytes()).hexdigest(),
            'analyzed': 5, 'universe': 7, 'error_rate': 0.0,
            'buy_signals': 0, 'sell_signals': 0, 'price_dates': ['2026-09-17'],
        }
        self.env = patch.dict(os.environ, {
            'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2',
            'EMAIL_FROM': 'sender@example.com', 'EMAIL_TO': 'recipient@example.com',
            'EMAIL_PASSWORD': 'test-only-password',
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def load(self):
        self.manifest_path.write_text(json.dumps(self.manifest), encoding='utf-8')
        return load_current_report(self.report_path, self.manifest_path)

    def test_zero_signals_still_delivers_a_valid_report(self):
        report, manifest = self.load()
        msg = make_message(MailConfig.from_env(), report, manifest, 'https://example.com/run')
        self.assertIn('0 buy / 0 sell', msg['Subject'])
        self.assertEqual(msg['To'], 'recipient@example.com')
        self.assertEqual(next(msg.iter_attachments()).get_content().rstrip(), self.report)

    def test_real_scanner_report_round_trips_through_delivery_validation(self):
        import pandas as pd
        from run_optimized_scan import save_report

        results = {
            'total_processed': 1, 'total_analyzed': 1,
            'processing_time_seconds': 2, 'actual_tps': 0.5, 'error_rate': 0.0,
            'analyses': [{'price_data': pd.DataFrame({'Close': [100.0]}, index=pd.to_datetime(['2026-09-17']))}],
        }
        with patch('run_optimized_scan.format_benchmark_summary', return_value='SPY context'), patch('builtins.print'):
            save_report(results, [], [], {}, {}, output_dir=self.directory.name)
        folder = Path(self.directory.name)
        report, manifest = load_current_report(folder / 'latest_optimized_scan.txt', folder / 'latest_scan_manifest.json')
        self.assertEqual(manifest['price_dates'], ['2026-09-17'])
        self.assertEqual(manifest['analyzed'], 1)
        self.assertIn('END OF SCAN', report)

    def test_html_is_escaped_and_utf8_preserved(self):
        msg = make_message(MailConfig.from_env(), *self.load(), 'https://example.com/run')
        body = msg.get_body(preferencelist=('html',)).get_content()
        self.assertNotIn('<script>', body)
        self.assertIn('&lt;script&gt;', body)
        self.assertIn('אפס איתותים', body)

    def test_stale_report_is_rejected(self):
        self.manifest['generated_at'] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.load()

    def test_prior_run_is_rejected(self):
        self.manifest['github_run_id'] = '122'
        with self.assertRaisesRegex(ValueError, 'workflow'):
            self.load()

    def test_prior_attempt_is_rejected(self):
        self.manifest['github_run_attempt'] = '1'
        with self.assertRaisesRegex(ValueError, 'workflow'):
            self.load()

    def test_modified_report_is_rejected(self):
        self.report_path.write_text('old data\nEND OF SCAN')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.load()

    def test_no_analysis_is_not_a_successful_empty_screen(self):
        self.manifest['analyzed'] = 0
        with self.assertRaisesRegex(ValueError, 'no analyzed'):
            self.load()

    def test_missing_credentials_fail(self):
        del os.environ['EMAIL_TO']
        with self.assertRaisesRegex(ValueError, 'EMAIL_TO'):
            MailConfig.from_env()

    def test_recipient_injection_is_rejected(self):
        os.environ['EMAIL_TO'] = 'recipient@example.com\nBcc: other@example.com'
        with self.assertRaises(ValueError):
            MailConfig.from_env()

    def test_tls_precedes_login_and_exact_recipient_is_used(self):
        with patch('scripts.send_daily_report.smtplib.SMTP') as smtp:
            server = smtp.return_value.__enter__.return_value
            server.send_message.return_value = {}
            config = MailConfig.from_env()
            message = make_message(config, *self.load(), 'https://example.com/run')
            deliver(config, message)
            names = [call[0] for call in server.method_calls]
            self.assertLess(names.index('starttls'), names.index('login'))
            self.assertEqual(server.send_message.call_args.kwargs['to_addrs'], ['recipient@example.com'])

    def test_smtp_rejection_fails_delivery(self):
        with patch('scripts.send_daily_report.smtplib.SMTP') as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {'recipient@example.com': (550, b'No')}
            with self.assertRaisesRegex(RuntimeError, 'refused'):
                deliver(MailConfig.from_env(), MagicMock())

    def test_authentication_failure_is_not_swallowed(self):
        with patch('scripts.send_daily_report.smtplib.SMTP') as smtp:
            smtp.return_value.__enter__.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b'No')
            with self.assertRaises(smtplib.SMTPAuthenticationError):
                deliver(MailConfig.from_env(), MagicMock())


if __name__ == '__main__':
    unittest.main()
