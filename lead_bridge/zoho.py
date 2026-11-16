"""Zoho v8 upsert + read-back verification. No outreach endpoints exist here."""
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass

from .errors import BridgeFault
from .http import Transport, checked_url, encode_json, secret

REGIONS = {
    "https://accounts.zoho.com": "https://www.zohoapis.com",
    "https://accounts.zoho.eu": "https://www.zohoapis.eu",
    "https://accounts.zoho.in": "https://www.zohoapis.in",
    "https://accounts.zoho.com.au": "https://www.zohoapis.com.au",
    "https://accounts.zoho.com.cn": "https://www.zohoapis.com.cn",
    "https://accounts.zoho.jp": "https://www.zohoapis.jp",
    "https://accounts.zohocloud.ca": "https://www.zohoapis.ca",
    "https://accounts.zoho.sa": "https://www.zohoapis.sa",
}
RECORD_ID = re.compile(r"^[0-9]{5,40}$")
SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")


@dataclass(frozen=True)
class Credentials:
    accounts_url: str
    api_domain: str
    client_id: str
    client_secret: str
    refresh_token: str

    def validate(self, *, testing=False):
        checked_url(self.accounts_url, allow_loopback=testing, base=True)
        checked_url(self.api_domain, allow_loopback=testing, base=True)
        if not testing and REGIONS.get(self.accounts_url) != self.api_domain:
            raise BridgeFault("ZOHO_REGION_CONFIGURATION", 503)
        for value in (self.client_id, self.client_secret, self.refresh_token):
            secret(value)


class ZohoClient:
    def __init__(self, credentials, transport=None, *, testing=False, clock=time.monotonic):
        credentials.validate(testing=testing)
        self.credentials = credentials
        self.transport = transport or Transport()
        self.clock = clock
        self.token, self.expires = None, 0
        self.lock = threading.Lock()

    def _access_token(self, force=False):
        with self.lock:
            if not force and self.token and self.clock() < self.expires:
                return self.token
            c = self.credentials
            body = urllib.parse.urlencode({"grant_type": "refresh_token", "client_id": c.client_id,
                                          "client_secret": c.client_secret,
                                          "refresh_token": c.refresh_token}).encode()
            response = self.transport.request("POST", c.accounts_url + "/oauth/v2/token", body=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"}, attempts=2, timeout=15)
            if response.status != 200:
                raise BridgeFault("ZOHO_OAUTH_HTTP_ERROR", 503)
            value = response.json()
            if not isinstance(value, dict) or not isinstance(value.get("access_token"), str):
                raise BridgeFault("ZOHO_OAUTH_INVALID_RESPONSE", 503)
            secret(value["access_token"])
            if value.get("api_domain") and value["api_domain"].rstrip("/") != c.api_domain:
                raise BridgeFault("ZOHO_OAUTH_DOMAIN_MISMATCH", 503)
            lifetime = value.get("expires_in", 3600)
            if not isinstance(lifetime, (int, float)) or not 0 < lifetime <= 86400:
                raise BridgeFault("ZOHO_OAUTH_INVALID_EXPIRY", 503)
            self.token = value["access_token"]
            self.expires = self.clock() + max(lifetime - 60, 1)
            return self.token

    def _crm(self, method, path, body=None):
        for auth_attempt in range(2):
            token = self._access_token(force=auth_attempt == 1)
            response = self.transport.request(method, self.credentials.api_domain + path,
                headers={"Authorization": "Zoho-oauthtoken " + token, "Content-Type": "application/json"},
                body=encode_json(body) if body is not None else None, attempts=3, timeout=20)
            if response.status != 401:
                return response
        raise BridgeFault("ZOHO_AUTHENTICATION_FAILED", 503)

    def probe(self):
        response = self._crm("GET", "/crm/v8/Leads?fields=Email&per_page=1")
        if response.status == 204:
            return
        if response.status != 200:
            raise BridgeFault("ZOHO_READ_PREFLIGHT_FAILED", 503)
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise BridgeFault("ZOHO_READ_PREFLIGHT_INVALID_RESPONSE", 503)

    def deliver(self, leads):
        if len(leads) > 100:
            raise BridgeFault("ZOHO_BATCH_TOO_LARGE", 422)
        result = {"accepted": 0, "verified": 0, "inserted": 0, "updated": 0,
                  "unknown_action": 0, "failed": 0, "errors": []}
        if not leads:
            return result
        request_body = {
            "data": list(leads), "duplicate_check_fields": ["Email"], "trigger": [],
            "$skip_feature_execution": [
                {"name": "cadences", "action": "insert"},
                {"name": "cadences", "action": "update"},
            ],
        }
        response = self._crm("POST", "/crm/v8/Leads/upsert", request_body)
        if response.status not in (200, 201, 202, 207):
            raise BridgeFault("ZOHO_UPSERT_HTTP_ERROR", 503, response.status in {429, 500, 502, 503, 504})
        payload = response.json()
        records = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(records, list) or len(records) != len(leads):
            raise BridgeFault("ZOHO_UPSERT_INVALID_RESPONSE", 502)
        expected = {}
        for index, (lead, record) in enumerate(zip(leads, records)):
            details = record.get("details", {}) if isinstance(record, dict) else {}
            record_id = details.get("id") if isinstance(details, dict) else None
            if (isinstance(record, dict) and record.get("status") == "success"
                    and record.get("code") == "SUCCESS" and isinstance(record_id, str)
                    and RECORD_ID.fullmatch(record_id)):
                if record_id in expected:
                    raise BridgeFault("ZOHO_DUPLICATE_RESULT_ID", 502)
                expected[record_id] = lead["Email"]
                result["accepted"] += 1
                action = record.get("action") or details.get("action")
                result[{"insert": "inserted", "update": "updated"}.get(action, "unknown_action")] += 1
            else:
                result["failed"] += 1
                code = record.get("code") if isinstance(record, dict) else None
                result["errors"].append({"index": index,
                    "code": code if isinstance(code, str) and SAFE_CODE.fullmatch(code) else "ZOHO_RECORD_REJECTED"})
        if expected:
            query = urllib.parse.urlencode({"ids": ",".join(expected), "fields": "Email"})
            try:
                verification = self._crm("GET", "/crm/v8/Leads?" + query)
                if verification.status != 200:
                    raise BridgeFault("ZOHO_READBACK_HTTP_ERROR", 503)
                data = verification.json()
                data = data.get("data") if isinstance(data, dict) else None
                if not isinstance(data, list):
                    raise BridgeFault("ZOHO_READBACK_INVALID_RESPONSE", 502)
                found = {}
                for record in data:
                    if not isinstance(record, dict) or not isinstance(record.get("id"), str):
                        raise BridgeFault("ZOHO_READBACK_INVALID_RESPONSE", 502)
                    rid, email = record["id"], record.get("Email")
                    if rid in found or rid not in expected or not isinstance(email, str):
                        raise BridgeFault("ZOHO_READBACK_MISMATCH", 502)
                    found[rid] = email.strip().lower()
                result["verified"] = sum(found.get(rid) == email for rid, email in expected.items())
                if result["verified"] != result["accepted"]:
                    raise BridgeFault("ZOHO_READBACK_MISMATCH", 502)
            except BridgeFault as fault:
                fault.metrics = result
                raise
        return result
