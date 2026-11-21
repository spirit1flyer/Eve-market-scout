"""Offline connectivity scenarios for the IDE app's Ctrl+F12 checks.

All HTTP behavior is injected; these checks never use the production singleton,
live credentials, public probe sites or ESI.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import socket
import threading
from types import SimpleNamespace

import aiohttp
import requests

from core.connectivity import ConnectivityChecker, CHECKING, is_connection_failure


def _require(value, message):
    if not value:
        raise AssertionError(message)


def _dns_error():
    return requests.ConnectionError(socket.gaierror(11001, "synthetic DNS failure"))


class _Probe:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.closed = 0

    def request(self, url, **kwargs):
        self.calls.append((url, kwargs))
        _require(kwargs["allow_redirects"] is False and kwargs["stream"] is True,
                 "probe downloaded a body or followed redirects")
        _require(kwargs["timeout"] == (2, 2) and callable(kwargs["auth"]),
                 "probe lost timeouts or explicit anonymous auth")
        _require(set(kwargs["headers"]) == {"User-Agent"}, "probe copied EVE headers")
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply

        def close():
            self.closed += 1
        return SimpleNamespace(status_code=reply, close=close)


def connectivity_outcomes(_directory):
    for replies, expected in (
            ([requests.Timeout(), 204], "INTERNET_AVAILABLE"),
            ([_dns_error(), _dns_error()], "DNS_UNAVAILABLE"),
            ([requests.Timeout(), requests.ConnectionError()], "INTERNET_UNCONFIRMED"),
            ([403], "INTERNET_AVAILABLE"),
            ([ValueError("synthetic bug")], "CHECK_FAILED")):
        probe = _Probe(replies)
        checker = ConnectivityChecker(request=probe.request, log=lambda *_: None)
        result = checker.request_check(requests.ConnectionError()).result(timeout=2)
        _require(result.state == expected, f"incorrect diagnosis: {result.state}")
        _require(probe.closed == int(expected == "INTERNET_AVAILABLE"),
                 "probe response was not closed")


def connectivity_filtering(_directory):
    for error in (ValueError("invalid order"), requests.HTTPError("HTTP 503"),
                  aiohttp.ClientResponseError(None, (), status=429),
                  asyncio.CancelledError(), RuntimeError("event loop closed")):
        _require(not is_connection_failure(error), "non-network error triggered probes")
    for error in (requests.ConnectionError(), requests.Timeout(), TimeoutError(),
                  socket.gaierror(), aiohttp.ClientConnectionError()):
        _require(is_connection_failure(error), "transport error was missed")
    outer = RuntimeError("wrapped")
    outer.__cause__ = _dns_error()
    _require(is_connection_failure(outer), "wrapped transport error was missed")
    checker = ConnectivityChecker(request=lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("non-network failure reached HTTP")), log=lambda *_: None)
    _require(checker.request_check(ValueError()) is None, "invalid data scheduled a probe")


def connectivity_singleflight(_directory):
    entered, release = threading.Event(), threading.Event()
    probe = _Probe([200, 200])
    clock = [0.0]

    def request(*args, **kwargs):
        entered.set()
        _require(release.wait(3), "synthetic probe did not release")
        return probe.request(*args, **kwargs)

    checker = ConnectivityChecker(request=request, clock=lambda: clock[0], log=lambda *_: None)
    error = requests.ConnectionError()
    try:
        first = checker.request_check(error)
        _require(entered.wait(2), "background probe did not start")
        with ThreadPoolExecutor(max_workers=8) as workers:
            futures = list(workers.map(lambda _: checker.request_check(error), range(20)))
        _require(all(future is first for future in futures), "failures started duplicate checks")
        _require(checker.failure_message(error, wait_seconds=0) == CHECKING.message,
                 "pending check blocked caller or claimed an outage")
    finally:
        release.set()
    _require(first.result(timeout=2).state == "INTERNET_AVAILABLE", "probe failed")
    clock[0] = 29
    _require(checker.request_check(error) is first, "cooldown was bypassed")
    clock[0] = 31
    second = checker.request_check(error)
    _require(second is not first and second.result(timeout=2).state == "INTERNET_AVAILABLE",
             "checker did not recheck after cooldown")
    _require(len(probe.calls) == 2, "concurrent failures amplified probe traffic")


def connectivity_auto_status(_directory):
    from gui.gui_main_scan import MainScanMixin

    updates = []
    gui = SimpleNamespace(status_label=SimpleNamespace(configure=lambda **kw: updates.append(kw)))
    # This must return without constructing Tk or waiting for a modal error box.
    MainScanMixin._show_error(gui, "synthetic DNS failure", "Internet unconfirmed", True)
    _require(updates == [{"text": "Internet unconfirmed"}], "auto error lost diagnosis")
