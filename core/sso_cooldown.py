"""Shared SSO Retry-After and failed-grant suppression, without saved tokens."""

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
from pathlib import Path
import threading

import requests

from core.esi_cache_io import write_gzip_atomic
from core.esi_diagnostics import traced_request
from core.esi_public_cache import _utc
from core.sound_manager import get_data_dir


class SSOCooldownError(requests.RequestException):
    pass


class SSOCooldown:
    def __init__(self, directory, request=None, now=None):
        self.path = Path(directory) / 'sso_cooldowns.json.gz'
        self.requester = request or traced_request
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.lock = threading.Lock()
        self.deadline = self.now()
        self.failures = {}
        try:
            import gzip
            with gzip.open(self.path, 'rt', encoding='utf-8') as stream:
                saved = json.load(stream)
            if saved.get('schema') != 1:
                raise ValueError('unknown cooldown schema')
            self.deadline = _utc(saved['deadline'])
            self.failures = {key: _utc(value) for key, value in saved['failures'].items()}
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f'[SSO-Cooldown] decision=RESTORE_FAILED error_type={type(exc).__name__}')

    def _save(self):
        now = self.now()
        self.failures = {key: value for key, value in self.failures.items() if value > now}
        try:
            write_gzip_atomic(self.path, dict(schema=1, deadline=self.deadline.isoformat(),
                              failures={key: value.isoformat() for key, value in self.failures.items()}))
        except Exception as exc:
            print(f'[SSO-Cooldown] decision=SAVE_FAILED error_type={type(exc).__name__}')

    def _retry_after(self, headers):
        value = next((v for k, v in headers.items() if k.lower() == 'retry-after'), None)
        if value is None:
            return None
        try:
            return self.now() + timedelta(seconds=max(0, int(value)))
        except (ValueError, TypeError, OverflowError):
            try:
                parsed = parsedate_to_datetime(value)
                return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
            except (ValueError, TypeError, OverflowError):
                return None

    def request(self, method, url, **kwargs):
        if url not in ('https://login.eveonline.com/v2/oauth/token',
                       'https://login.eveonline.com/v2/oauth/verify'):
            raise ValueError('SSO cooldown accepts only the official token/verify endpoints')
        data, headers = kwargs.get('data') or {}, kwargs.get('headers') or {}
        # A different grant can still work after an invalid grant. Server-wide
        # throttles, by contrast, block both endpoints and all callers.
        identity = [method, url, data.get('client_id'), data.get('grant_type'),
                    data.get('refresh_token') or data.get('code') or headers.get('Authorization')]
        key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        with self.lock:
            now = self.now()
            until = max(self.deadline, self.failures.get(key, now))
            if until > now:
                seconds = max(1, int((until-now).total_seconds()) + 1)
                print(f'[SSO-Cooldown] decision=SKIP reason=cooldown next_in={seconds}s')
                raise SSOCooldownError(f'EVE sign-in is cooling down; retry in {seconds} seconds')
            try:
                response = self.requester(method, url, **kwargs)
            except requests.RequestException:
                self.failures[key] = self.now() + timedelta(seconds=30)
                self._save()
                print('[SSO-Cooldown] decision=FAILED reason=network_error cooldown=30s')
                raise
            now = self.now()
            retry_after = self._retry_after(response.headers)
            if retry_after and retry_after > now:
                self.deadline = max(self.deadline, retry_after)
            if response.status_code == 429:
                self.deadline = max(self.deadline, now + timedelta(seconds=1),
                                    retry_after or now + timedelta(seconds=60))
            if response.status_code != 200:
                self.failures[key] = now + timedelta(seconds=60 if response.status_code < 500 else 30)
                print(f'[SSO-Cooldown] decision=FAILED status={response.status_code} '
                      f'next_in={max(0, (max(self.deadline, self.failures[key])-now).total_seconds()):.0f}s')
            else:
                self.failures.pop(key, None)
            self._save()
            return response


_gate = None
_lock = threading.Lock()


def sso_request(method, url, **kwargs):
    global _gate
    with _lock:
        if _gate is None:
            _gate = SSOCooldown(get_data_dir())
    return _gate.request(method, url, **kwargs)
