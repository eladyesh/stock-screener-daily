"""Telegram delivery contract; no live network requests or real credentials."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
import requests

from scripts.send_telegram_report import (
    DeliveryError, TelegramConfig, deliver, main, make_caption,
)


@pytest.fixture
def scan(monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', '123456:test_token_only')
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '987654321')
    monkeypatch.setenv('SCAN_MODE', 'smoke')
    report = 'BUY #1: AAPL | Score: 80\nאפס טעויות\nEND OF SCAN'
    manifest = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'analyzed': 5, 'universe': 5, 'error_rate': 0,
        'buy_signals': 1, 'sell_signals': 0, 'price_dates': ['2026-09-18'],
        'report_sha256': hashlib.sha256(report.encode('utf-8')).hexdigest(),
    }
    return report, manifest


def response(status=200, **payload):
    obj = Mock(status_code=status)
    obj.json.return_value = payload or {
        'ok': True,
        'result': {'message_id': 123, 'chat': {'id': 987654321, 'type': 'private'},
                   'document': {'file_id': 'test-document'}},
    }
    return obj


def test_complete_report_is_one_message_to_explicit_chat(scan, monkeypatch):
    post = Mock(return_value=response())
    monkeypatch.setattr('scripts.send_telegram_report.requests.post', post)
    assert deliver(TelegramConfig.from_env(), *scan, 'https://github.com/example/run') == 123
    post.assert_called_once()
    args, kwargs = post.call_args
    assert args[0].endswith('/sendDocument')
    assert kwargs['data']['chat_id'] == '987654321'
    assert kwargs['data']['caption'].startswith('[TEST]')
    assert 'AAPL' in kwargs['data']['caption']
    assert 'parse_mode' not in kwargs['data']
    assert kwargs['files']['document'][1] == scan[0].encode('utf-8')
    assert kwargs['allow_redirects'] is False
    assert kwargs['timeout'] == (10, 60)


def test_no_signal_caption_is_valid_and_bounded(scan):
    scan[1].update(buy_signals=0, error_rate=0.2)
    caption = make_caption('END OF SCAN', scan[1], 'https://example.com/' + '📈' * 2000)
    assert 'קנייה: 0' in caption
    assert 'דוח חלקי' in caption
    assert len(caption.encode('utf-16-le')) <= 2048


@pytest.mark.parametrize('variable,value', [
    ('TELEGRAM_BOT_TOKEN', ''), ('TELEGRAM_BOT_TOKEN', '123:token/other'),
    ('TELEGRAM_CHAT_ID', '@someone'), ('TELEGRAM_CHAT_ID', '-100123'),
    ('TELEGRAM_CHAT_ID', '0'),
])
def test_missing_or_ambiguous_recipient_credentials_are_rejected(scan, monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError, match=variable):
        TelegramConfig.from_env()


@pytest.mark.parametrize('status', [400, 401, 403, 500])
def test_api_rejection_never_claims_delivery(scan, monkeypatch, status):
    post = Mock(return_value=response(status, ok=False, error_code=status, description='sensitive server response'))
    monkeypatch.setattr('scripts.send_telegram_report.requests.post', post)
    with pytest.raises(DeliveryError) as exc:
        deliver(TelegramConfig.from_env(), *scan, '')
    assert 'sensitive server response' not in str(exc.value)
    post.assert_called_once()


def test_explicit_rate_limit_retries_after_requested_delay(scan, monkeypatch):
    post = Mock(side_effect=[response(429, ok=False, error_code=429, parameters={'retry_after': 2}), response()])
    sleep = Mock()
    monkeypatch.setattr('scripts.send_telegram_report.requests.post', post)
    monkeypatch.setattr('scripts.send_telegram_report.time.sleep', sleep)
    deliver(TelegramConfig.from_env(), *scan, '')
    sleep.assert_called_once_with(2)
    assert post.call_count == 2


def test_network_error_is_not_retried_or_exposed(scan, monkeypatch):
    token = TelegramConfig.from_env().token
    post = Mock(side_effect=requests.Timeout(f'https://api.telegram.org/bot{token}/sendDocument'))
    monkeypatch.setattr('scripts.send_telegram_report.requests.post', post)
    with pytest.raises(DeliveryError, match='unknown') as exc:
        deliver(TelegramConfig.from_env(), *scan, '')
    assert token not in str(exc.value)
    assert token not in repr(TelegramConfig.from_env())
    post.assert_called_once()


def test_wrong_recipient_response_is_not_accepted(scan, monkeypatch):
    reply = response()
    reply.json.return_value['result']['chat']['id'] = 111
    monkeypatch.setattr('scripts.send_telegram_report.requests.post', Mock(return_value=reply))
    with pytest.raises(DeliveryError, match='configured private chat'):
        deliver(TelegramConfig.from_env(), *scan, '')


def test_stale_report_never_reaches_telegram(scan, monkeypatch, tmp_path, capsys):
    report, manifest = scan
    manifest['generated_at'] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    (tmp_path / 'report.txt').write_text(report, encoding='utf-8')
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    monkeypatch.setattr('sys.argv', ['sender', '--report', str(tmp_path / 'report.txt'),
                                   '--manifest', str(tmp_path / 'manifest.json')])
    post = Mock()
    monkeypatch.setattr('scripts.send_telegram_report.requests.post', post)
    assert main() == 1
    post.assert_not_called()
    assert 'stale' in capsys.readouterr().err
