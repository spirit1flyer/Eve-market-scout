"""Failure-triggered internet diagnosis, independent of EVE and its credentials.

One daemon worker checks two public HTTPS sites. A short shared cooldown keeps
failed market pages from starting a probe storm. The original request is never
retried or changed here, and a failed probe never proves that all internet is down.
"""

from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass
import socket
import threading
import time

import aiohttp
import requests

from core.config import ESI_USER_AGENT


PROBE_URLS = ("https://www.microsoft.com/", "https://www.cloudflare.com/")
RECHECK_SECONDS = 30


def _no_auth(request):
    return request  # Prevent requests from applying implicit .netrc credentials.


def _exceptions(error):
    """Walk wrapped requests/urllib3/aiohttp errors without printing their text."""
    pending, seen = [error], set()
    while pending and len(seen) < 64:
        item = pending.pop()
        if not isinstance(item, BaseException) or id(item) in seen:
            continue
        seen.add(id(item))
        yield item
        pending.extend((item.__cause__, item.__context__,
                        getattr(item, "reason", None), getattr(item, "os_error", None)))
        pending.extend(arg for arg in item.args if isinstance(arg, BaseException))


def is_connection_failure(error):
    """HTTP rejection, invalid data and cancellation are not internet failures."""
    return any(isinstance(item, (requests.ConnectionError, requests.Timeout,
                                aiohttp.ClientConnectionError, TimeoutError,
                                socket.gaierror)) for item in _exceptions(error))


@dataclass(frozen=True)
class ConnectivityResult:
    state: str
    message: str


CHECKING = ConnectivityResult(
    "CHECKING", "Internet access check is still running; see eve_scout.log for its result.")
UNKNOWN = ConnectivityResult(
    "CHECK_FAILED", "The internet access check could not finish; connection status is unknown.")


class ConnectivityChecker:
    def __init__(self, *, request=None, clock=None, log=print):
        self._request = request or requests.head
        self._clock = clock or time.monotonic
        self._log = log
        self._lock = threading.Lock()
        self._future = None
        self._completed_at = float("-inf")

    def request_check(self, error):
        """Return a shared future immediately; never perform network work here."""
        if not is_connection_failure(error):
            return None
        with self._lock:
            if self._future is not None and (
                    not self._future.done()
                    or self._clock() - self._completed_at < RECHECK_SECONDS):
                return self._future
            future = Future()
            self._future = future
            worker = threading.Thread(target=self._run, args=(future,),
                                      name="internet-access-check", daemon=True)
            try:
                worker.start()
            except Exception:
                self._completed_at = self._clock()
                future.set_result(UNKNOWN)
            return future

    def _probe(self):
        dns_failures = 0
        for url in PROBE_URLS:
            host = url.split("/")[2]
            try:
                # No EVE session, auth, cookies, redirects, response body or ESI
                # trace. Even an HTTP error establishes a reachable HTTPS host.
                response = self._request(url, headers={"User-Agent": ESI_USER_AGENT},
                                         timeout=(2, 2), allow_redirects=False, stream=True,
                                         auth=_no_auth)
                try:
                    status = response.status_code
                    if type(status) is not int or not 100 <= status <= 599:
                        raise ValueError("invalid probe HTTP status")
                finally:
                    response.close()
                self._log(f"[Connectivity] probe={host} result=HTTP_RESPONSE status={status}")
                return ConnectivityResult(
                    "INTERNET_AVAILABLE", "Internet access detected. The EVE connection failed.")
            except (requests.RequestException, OSError) as exc:
                dns = any(isinstance(item, socket.gaierror) for item in _exceptions(exc))
                dns_failures += int(dns)
                self._log(f"[Connectivity] probe={host} result=FAILED "
                          f"reason={'dns' if dns else 'connection'} error_type={type(exc).__name__}")
        if dns_failures == len(PROBE_URLS):
            return ConnectivityResult(
                "DNS_UNAVAILABLE", "Internet access could not be confirmed: DNS lookups failed "
                "for both independent test sites. Check your internet connection.")
        return ConnectivityResult(
            "INTERNET_UNCONFIRMED", "Internet access could not be confirmed. Your connection "
            "may be down, or network settings may be blocking the checks.")

    def _run(self, future):
        result = UNKNOWN
        try:
            result = self._probe()
        except Exception as exc:
            self._log(f"[Connectivity] result=CHECK_FAILED error_type={type(exc).__name__}")
        finally:
            with self._lock:
                self._completed_at = self._clock()
                future.set_result(result)
            self._log(f"[Connectivity] result={result.state} message={result.message}")

    def failure_message(self, error, wait_seconds=6):
        future = self.request_check(error)
        if future is None:
            return None
        # Requests' DNS resolution may outlast its socket timeout. Only this
        # daemon worker can remain pending; a scan/UI never waits indefinitely.
        try:
            return future.result(timeout=wait_seconds).message
        except FutureTimeout:
            return CHECKING.message


_checker = ConnectivityChecker()


def note_connection_failure(error):
    """Used by the shared ESI/SSO observer; diagnosis cannot mask its error."""
    try:
        _checker.request_check(error)
    except Exception as exc:
        print(f"[Connectivity] result=CHECK_FAILED error_type={type(exc).__name__}")


def connection_failure_message(error):
    """Called on the scan worker, never the Tk thread."""
    try:
        return _checker.failure_message(error)
    except Exception:
        return UNKNOWN.message if is_connection_failure(error) else None
