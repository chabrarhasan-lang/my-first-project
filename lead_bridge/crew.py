"""Deployed CrewAI API adapter. Kickoff is intentionally NEVER auto-retried."""
import re
import time

from .errors import BridgeFault
from .http import Transport, checked_url, decode_json, encode_json, secret

EXECUTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def selected_leads(status_payload, pointer="/result/output"):
    value = status_payload
    if pointer:
        if not isinstance(pointer, str) or not pointer.startswith("/"):
            raise BridgeFault("CREW_OUTPUT_POINTER_CONFIGURATION", 503)
        for token in pointer[1:].split("/"):
            if re.search(r"~(?![01])", token):
                raise BridgeFault("CREW_OUTPUT_POINTER_CONFIGURATION", 503)
            token = token.replace("~1", "/").replace("~0", "~")
            try:
                if isinstance(value, list) and token.isdigit():
                    value = value[int(token)]
                elif isinstance(value, dict):
                    value = value[token]
                else:
                    raise KeyError()
            except (KeyError, IndexError, ValueError):
                raise BridgeFault("CREW_OUTPUT_POINTER_NOT_FOUND") from None
    if isinstance(value, str):
        try:
            value = decode_json(value)
        except BridgeFault:
            raise BridgeFault("CREW_OUTPUT_MUST_BE_STRUCTURED_JSON") from None
    if isinstance(value, dict):
        value = value.get("leads")
    if not isinstance(value, list):
        raise BridgeFault("CREW_OUTPUT_MUST_CONTAIN_LEADS_ARRAY")
    return value


class CrewClient:
    def __init__(self, base_url, api_key, *, transport=None, leads_pointer="/result/output",
                 timeout_seconds=1200, poll_seconds=5, clock=time.monotonic, sleep=time.sleep,
                 testing=False):
        self.base = checked_url(base_url, base=True, allow_loopback=testing)
        self.key = secret(api_key)
        self.http = transport or Transport()
        if not 1 <= timeout_seconds <= 1800 or not 0 < poll_seconds <= 60:
            raise BridgeFault("CREW_POLL_LIMIT_CONFIGURATION", 503)
        self.pointer, self.timeout, self.interval = leads_pointer, timeout_seconds, poll_seconds
        self.clock, self.sleep = clock, sleep

    @property
    def headers(self):
        return {"Authorization": "Bearer " + self.key, "Content-Type": "application/json"}

    def _read(self, path):
        response = self.http.request("GET", self.base + path, headers=self.headers, attempts=3)
        if response.status != 200:
            raise BridgeFault("CREW_READ_HTTP_ERROR", 502)
        body = response.json()
        if not isinstance(body, dict):
            raise BridgeFault("CREW_INVALID_RESPONSE")
        return body

    def collect(self, inputs, *, resume_id=None, progress=lambda **kw: None):
        if not isinstance(inputs, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                               for k, v in inputs.items()):
            raise BridgeFault("CREW_INPUTS_MUST_BE_STRING_MAP", 503)
        if resume_id:
            if not isinstance(resume_id, str) or not EXECUTION_ID.fullmatch(resume_id):
                raise BridgeFault("INVALID_RESUME_KICKOFF_ID", 422)
            kickoff_id = resume_id
        else:
            schema = self._read("/inputs")
            accepted = schema.get("inputs")
            required = schema.get("required_inputs", accepted)
            if (not isinstance(accepted, list) or not isinstance(required, list)
                    or any(not isinstance(x, str) for x in accepted + required)
                    or not set(required).issubset(accepted)):
                raise BridgeFault("CREW_INVALID_INPUT_SCHEMA")
            if set(required) - set(inputs) or set(inputs) - set(accepted):
                raise BridgeFault("CREW_INPUT_SCHEMA_MISMATCH", 503)
            try:
                response = self.http.request("POST", self.base + "/kickoff", headers=self.headers,
                    body=encode_json({"inputs": inputs}), attempts=1, timeout=30)
            except BridgeFault:
                raise BridgeFault("CREW_KICKOFF_OUTCOME_UNKNOWN_DO_NOT_RETRY", 502) from None
            if response.status not in (200, 201, 202):
                code = ("CREW_KICKOFF_OUTCOME_UNKNOWN_DO_NOT_RETRY" if response.status >= 500
                        else "CREW_KICKOFF_REJECTED")
                raise BridgeFault(code, 502)
            body = response.json()
            kickoff_id = body.get("kickoff_id") if isinstance(body, dict) else None
            if not isinstance(kickoff_id, str) or not EXECUTION_ID.fullmatch(kickoff_id):
                raise BridgeFault("CREW_KICKOFF_OUTCOME_UNKNOWN_DO_NOT_RETRY")
        # Persist this ID immediately via the caller, before polling can fail.
        progress(kickoff_id=kickoff_id, stage="crew_running")
        deadline = self.clock() + self.timeout
        while self.clock() < deadline:
            status = self._read("/status/" + kickoff_id)
            state = status.get("state", status.get("status", ""))
            if not isinstance(state, str):
                raise BridgeFault("CREW_INVALID_STATUS")
            state = state.upper()
            if state in {"SUCCESS", "COMPLETED"}:
                return kickoff_id, selected_leads(status, self.pointer)
            if state in {"FAILED", "FAILURE", "CANCELLED", "CANCELED", "ERROR"}:
                raise BridgeFault("CREW_EXECUTION_FAILED")
            if state not in {"RUNNING", "PENDING", "STARTED", "QUEUED", "IN_PROGRESS"}:
                raise BridgeFault("CREW_UNSUPPORTED_EXECUTION_STATE")
            self.sleep(min(self.interval, max(0, deadline - self.clock())))
        raise BridgeFault("CREW_EXECUTION_TIMEOUT_RESUME_BY_ID", 504)
