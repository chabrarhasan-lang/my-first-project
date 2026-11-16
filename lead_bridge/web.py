"""Production WSGI ingestion API. No unauthenticated writes or simulation route."""
import hashlib
import hmac
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from http import HTTPStatus

from .errors import BridgeFault
from .http import decode_json, encode_json, secret
from .models import validate_envelope
from .zoho import Credentials, ZohoClient

MAX_BODY_BYTES = 1024 * 1024
LOGGER = logging.getLogger("lead_bridge")


@dataclass(frozen=True)
class ServerSettings:
    token: str
    live_enabled: bool = False


class IngestionApp:
    def __init__(self, settings, destination=None, *, clock=time.monotonic):
        self.settings, self.destination, self.clock = settings, destination, clock
        self.receipts = OrderedDict()
        self.lock = threading.Lock()
        try:
            secret(settings.token, 32)
            self.configured = settings.live_enabled and destination is not None
        except BridgeFault:
            self.configured = False

    def __call__(self, environ, start_response):
        try:
            status, payload = self.handle(environ)
        except BridgeFault as fault:
            status, payload = fault.status, {"ok": False, "error": fault.code,
                "retryable": fault.retryable, **({"metrics": fault.metrics} if fault.metrics else {})}
        except Exception:
            LOGGER.error("ingestion_internal_error")
            status, payload = 500, {"ok": False, "error": "INTERNAL_ERROR", "retryable": False}
        body = encode_json(payload)
        headers = [("Content-Type", "application/json; charset=utf-8"),
                   ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
                   ("X-Content-Type-Options", "nosniff")]
        start_response(f"{status} {HTTPStatus(status).phrase}", headers)
        return [b"" if environ.get("REQUEST_METHOD") == "HEAD" else body]

    def handle(self, environ):
        method, path = environ.get("REQUEST_METHOD"), environ.get("PATH_INFO")
        if path == "/healthz" and method in {"GET", "HEAD"}:
            return 200, {"status": "up"}
        if path == "/readyz" and method in {"GET", "HEAD"}:
            return (200 if self.configured else 503), {"ready": self.configured,
                "scope": "local_configuration_only"}
        if path not in {"/ingest-crewai-leads", "/preflight"}:
            raise BridgeFault("NOT_FOUND", 404)
        if (path == "/preflight" and method != "GET") or (path == "/ingest-crewai-leads" and method != "POST"):
            raise BridgeFault("METHOD_NOT_ALLOWED", 405)
        supplied = environ.get("HTTP_AUTHORIZATION", "")
        expected = "Bearer " + self.settings.token
        if (not self.settings.token or not isinstance(supplied, str)
                or not hmac.compare_digest(supplied.encode(), expected.encode())):
            raise BridgeFault("UNAUTHORIZED", 401)
        if not self.configured:
            raise BridgeFault("BRIDGE_NOT_CONFIGURED", 503)
        if path == "/preflight":
            self.destination.probe()
            return 200, {"ready": True, "zoho_read_access": True}
        if environ.get("CONTENT_TYPE", "").split(";")[0].strip().lower() != "application/json":
            raise BridgeFault("JSON_CONTENT_TYPE_REQUIRED", 415)
        try:
            length = int(environ.get("CONTENT_LENGTH", ""))
        except (ValueError, TypeError):
            raise BridgeFault("CONTENT_LENGTH_REQUIRED", 411) from None
        if length <= 0 or length > MAX_BODY_BYTES:
            raise BridgeFault("REQUEST_BODY_TOO_LARGE", 413)
        raw = environ["wsgi.input"].read(length)
        if len(raw) != length:
            raise BridgeFault("INCOMPLETE_BODY", 400)
        payload = decode_json(raw)
        validated = validate_envelope(payload)
        run_id = payload["run_id"]
        if environ.get("HTTP_X_IDEMPOTENCY_KEY") != run_id:
            raise BridgeFault("IDEMPOTENCY_KEY_REQUIRED", 422)
        digest = hashlib.sha256(encode_json(payload)).hexdigest()
        # One process / bounded request cache. Zoho Email upsert is the restart-level dedupe mechanism.
        with self.lock:
            now = self.clock()
            for key in list(self.receipts):
                if now - self.receipts[key][3] >= 86400:
                    del self.receipts[key]
            if run_id in self.receipts:
                old_digest, old_status, receipt, _ = self.receipts[run_id]
                if digest != old_digest:
                    raise BridgeFault("IDEMPOTENCY_CONFLICT", 409)
                return old_status, {**receipt, "replayed": True}
            metrics = {"received": validated.received, "unique": len(validated.leads),
                       "duplicates": validated.duplicates}
            try:
                metrics.update(self.destination.deliver(validated.leads))
            except BridgeFault as fault:
                fault.metrics = {**metrics, **fault.metrics}
                raise
            successful = metrics["failed"] == 0 and metrics["accepted"] == metrics["verified"] == len(validated.leads)
            receipt = {"ok": successful, "mode": "hunt-only", "run_id": run_id,
                       "replayed": False, "metrics": metrics}
            status = 200 if successful else 207
            if successful:
                self.receipts[run_id] = (digest, status, receipt, now)
                while len(self.receipts) > 256:
                    self.receipts.popitem(last=False)
            LOGGER.info("ingestion_complete run_id=%s accepted=%d verified=%d failed=%d",
                        run_id, metrics["accepted"], metrics["verified"], metrics["failed"])
            return status, receipt


def create_app():
    settings = ServerSettings(os.environ.get("BRIDGE_INGEST_TOKEN", ""),
                              os.environ.get("BRIDGE_LIVE_ENABLED") == "true")
    destination = None
    if settings.live_enabled:
        try:
            secret(settings.token, 32)
            credentials = Credentials(*(os.environ.get(name, "") for name in (
                "ZOHO_ACCOUNTS_URL", "ZOHO_API_DOMAIN", "ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN")))
            destination = ZohoClient(credentials)
        except BridgeFault as fault:
            LOGGER.warning("bridge_configuration_invalid code=%s", fault.code)
    return IngestionApp(settings, destination)


app = create_app()
