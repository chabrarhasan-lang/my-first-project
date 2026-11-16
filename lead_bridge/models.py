"""Allowlisted lead schema; limits verified against the connected Zoho tenant."""
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .errors import BridgeFault

FIELDS = {
    "email": ("Email", 100), "last_name": ("Last_Name", 80),
    "first_name": ("First_Name", 40), "company": ("Company", 200),
    "phone": ("Phone", 30), "website": ("Website", 255),
}
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
EMAIL = re.compile(r"^[^\s@<>\x00-\x1f]+@[^\s@<>\x00-\x1f]+\.[^\s@<>\x00-\x1f]+$")


@dataclass(frozen=True)
class ValidatedLeads:
    leads: tuple
    received: int
    duplicates: int


def validate_leads(items, max_leads=100):
    if not isinstance(items, list) or len(items) > max_leads:
        raise BridgeFault("INVALID_LEAD_BATCH", 422)
    normalized, seen = [], set()
    for item in items:
        if not isinstance(item, dict) or set(item) - set(FIELDS):
            raise BridgeFault("INVALID_LEAD_FIELDS", 422)
        record = {}
        for field, (api_name, maximum) in FIELDS.items():
            if field not in item:
                continue
            value = item[field]
            if (not isinstance(value, str)
                    or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                raise BridgeFault("INVALID_LEAD_VALUE", 422)
            value = value.strip()
            if len(value) > maximum:
                raise BridgeFault("LEAD_FIELD_TOO_LONG", 422)
            if value:
                record[api_name] = value
        if not record.get("Last_Name") or not EMAIL.fullmatch(record.get("Email", "")):
            raise BridgeFault("MISSING_OR_INVALID_LEAD_IDENTITY", 422)
        record["Email"] = record["Email"].lower()
        if record.get("Website"):
            try:
                parsed = urlsplit(record["Website"])
                if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                        or parsed.username or parsed.password):
                    raise ValueError()
            except ValueError:
                raise BridgeFault("INVALID_LEAD_WEBSITE", 422) from None
        if record["Email"] not in seen:
            seen.add(record["Email"])
            normalized.append(record)
    return ValidatedLeads(tuple(normalized), len(items), len(items) - len(normalized))


def validate_envelope(payload):
    if not isinstance(payload, dict) or set(payload) != {"run_id", "mode", "source", "leads"}:
        raise BridgeFault("INVALID_ENVELOPE", 422)
    if payload["mode"] != "hunt-only" or payload["source"] != "crewai":
        raise BridgeFault("HUNT_ONLY_REQUIRED", 422)
    if not isinstance(payload["run_id"], str) or not RUN_ID.fullmatch(payload["run_id"]):
        raise BridgeFault("INVALID_RUN_ID", 422)
    return validate_leads(payload["leads"])
