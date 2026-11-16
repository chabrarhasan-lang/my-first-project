"""Bounded, TLS-verified HTTP. Never follows redirects or logs bodies/tokens."""
import ipaddress
import json
import random
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .errors import BridgeFault

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def decode_json(raw):
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        return json.loads(text, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise BridgeFault("INVALID_JSON", 422) from None


def encode_json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":")).encode("utf-8")


def secret(value, minimum=1):
    if (not isinstance(value, str) or len(value) < minimum
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise BridgeFault("INVALID_SECRET_CONFIGURATION", 503)
    return value


def checked_url(value, *, allow_loopback=False, base=False):
    try:
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname or ""
        address = None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        local = host in {"localhost", "127.0.0.1", "::1"}
        if allow_loopback and local and parsed.scheme == "http":
            pass
        elif (parsed.scheme != "https" or parsed.port not in (None, 443)
              or not host or local or (address is not None and not address.is_global)):
            raise ValueError()
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError()
        if base and parsed.query:
            raise ValueError()
        if any(ord(c) < 33 for c in value):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise BridgeFault("INVALID_URL_CONFIGURATION", 503) from None
    return value.rstrip("/") if base else value


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict
    body: bytes

    def json(self):
        try:
            return decode_json(self.body)
        except BridgeFault:
            raise BridgeFault("UPSTREAM_INVALID_JSON") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Transport:
    def __init__(self, *, allow_loopback=False, sleep=time.sleep):
        self.allow_loopback = allow_loopback
        self.sleep = sleep
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def request(self, method, url, *, headers=None, body=None, attempts=1, timeout=20):
        checked_url(url, allow_loopback=self.allow_loopback)
        if attempts not in (1, 2, 3) or not 0 < timeout <= 60:
            raise BridgeFault("INVALID_HTTP_LIMIT", 503)
        for attempt in range(attempts):
            response = None
            try:
                request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
                try:
                    handle = self.opener.open(request, timeout=timeout)
                except urllib.error.HTTPError as exc:
                    handle = exc
                with handle:
                    payload = handle.read(MAX_RESPONSE_BYTES + 1)
                    if len(payload) > MAX_RESPONSE_BYTES:
                        raise BridgeFault("UPSTREAM_RESPONSE_TOO_LARGE")
                    response = Response(handle.code, dict(handle.headers.items()), payload)
            except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
                if attempt + 1 == attempts:
                    raise BridgeFault("UPSTREAM_TRANSPORT_ERROR", 503, True) from None
            if response is not None and (response.status not in RETRY_STATUSES
                                          or attempt + 1 == attempts):
                return response
            delay = min(0.5 * (2 ** attempt) + random.uniform(0, 0.1), 5)
            if response is not None:
                try:
                    delay = max(delay, min(float(response.headers.get("Retry-After", "0")), 5))
                except ValueError:
                    pass
            self.sleep(delay)
        raise BridgeFault("UPSTREAM_TRANSPORT_ERROR", 503, True)
