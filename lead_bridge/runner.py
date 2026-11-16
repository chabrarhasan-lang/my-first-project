"""Scheduled hunt CLI. Real secrets and explicit activation are mandatory."""
import argparse
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from .crew import CrewClient
from .errors import BridgeFault
from .http import Transport, checked_url, decode_json, encode_json, secret
from .models import FIELDS, validate_leads


class IngestClient:
    def __init__(self, url, token, *, transport=None, testing=False):
        self.url = checked_url(url, allow_loopback=testing)
        parsed = urllib.parse.urlsplit(self.url)
        if parsed.path != "/ingest-crewai-leads" or parsed.query:
            raise BridgeFault("INGEST_URL_CONFIGURATION", 503)
        self.preflight_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/preflight", "", ""))
        self.token = secret(token, 32)
        self.http = transport or Transport()

    def preflight(self):
        response = self.http.request("GET", self.preflight_url,
            headers={"Authorization": "Bearer " + self.token}, attempts=3, timeout=60)
        if response.status != 200:
            raise BridgeFault("INGEST_PREFLIGHT_FAILED", 503)
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("ready") is not True or payload.get("zoho_read_access") is not True:
            raise BridgeFault("INGEST_PREFLIGHT_INVALID_RESPONSE", 503)

    def send(self, kickoff_id, index, leads):
        run_id = f"crew:{kickoff_id}:batch:{index}"
        payload = {"run_id": run_id, "source": "crewai", "mode": "hunt-only", "leads": leads}
        response = self.http.request("POST", self.url, headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json",
            "X-Idempotency-Key": run_id}, body=encode_json(payload), attempts=3, timeout=60)
        if response.status not in (200, 207):
            safe_partial = {}
            try:
                failed = response.json()
                failed_metrics = failed.get("metrics", {}) if isinstance(failed, dict) else {}
                safe_partial = {k: failed_metrics[k] for k in (
                    "accepted", "verified", "inserted", "updated", "unknown_action", "failed")
                    if type(failed_metrics.get(k)) is int and failed_metrics[k] >= 0}
            except BridgeFault:
                pass
            raise BridgeFault("INGEST_DELIVERY_HTTP_ERROR", 502, metrics=safe_partial)
        result = response.json()
        metrics = result.get("metrics") if isinstance(result, dict) else None
        keys = ("accepted", "verified", "inserted", "updated", "unknown_action", "failed")
        if (not isinstance(metrics, dict) or any(type(metrics.get(k)) is not int or metrics[k] < 0 for k in keys)
                or not isinstance(result.get("ok"), bool) or not isinstance(result.get("replayed"), bool)
                or result.get("run_id") != run_id):
            raise BridgeFault("INGEST_INVALID_RECEIPT")
        if (response.status != 200 or result["ok"] is not True or metrics["failed"]
                or metrics["accepted"] != metrics["verified"] or metrics["verified"] != len(leads)
                or metrics["inserted"] + metrics["updated"] + metrics["unknown_action"] != metrics["accepted"]):
            raise BridgeFault("INGEST_PARTIAL_OR_UNVERIFIED_DELIVERY", 502, metrics=metrics)
        receipt = {k: metrics[k] for k in keys}
        receipt.update(replayed_batches=int(result["replayed"]),
                       replayed_records=len(leads) if result["replayed"] else 0)
        if result["replayed"]:
            receipt.update(inserted=0, updated=0, unknown_action=0)
        return receipt


def run_pipeline(crew, ingest, inputs, *, resume_id=None, max_leads=100, progress=lambda **kw: None):
    if not 1 <= max_leads <= 1000:
        raise BridgeFault("HUNT_LEAD_LIMIT_CONFIGURATION", 503)
    ingest.preflight()
    kickoff_id, raw = crew.collect(inputs, resume_id=resume_id, progress=progress)
    progress(stage="validating_leads", collected=len(raw))
    if len(raw) > max_leads:
        raise BridgeFault("HUNT_LEAD_LIMIT_EXCEEDED_NO_LEADS_SENT", 422)
    valid = validate_leads(raw, max_leads=max_leads)
    reverse = {api: local for local, (api, _) in FIELDS.items()}
    leads = [{reverse[k]: v for k, v in lead.items()} for lead in valid.leads]
    totals = {"collected": valid.received, "unique": len(leads), "duplicates": valid.duplicates,
              "accepted": 0, "verified": 0, "inserted": 0, "updated": 0, "unknown_action": 0,
              "failed": 0, "batches": 0, "replayed_batches": 0, "replayed_records": 0,
              "delivery_proven": False, "delivery_outcome_unknown": False}
    for offset in range(0, len(leads), 100):
        try:
            receipt = ingest.send(kickoff_id, offset // 100, leads[offset:offset + 100])
        except BridgeFault as fault:
            safe_partial = {k: fault.metrics[k] for k in ("accepted", "verified", "inserted", "updated", "unknown_action", "failed")
                            if type(fault.metrics.get(k)) is int and fault.metrics[k] >= 0}
            totals["delivery_outcome_unknown"] = not bool(safe_partial)
            for key, value in safe_partial.items():
                totals[key] += value
            fault.metrics = totals
            raise
        for key, value in receipt.items():
            totals[key] += value
        totals["batches"] += 1
        progress(stage="delivering", **totals)
    totals["delivery_proven"] = bool(totals["verified"]) and totals["verified"] == totals["unique"]
    return kickoff_id, totals


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run an explicitly enabled, hunt-only deployed CrewAI job")
    parser.add_argument("--summary", default="run-summary.json")
    parser.add_argument("--resume-kickoff-id", default="")
    args = parser.parse_args(argv)
    started = time.monotonic()
    report = {"test_kind": "live", "mode": "hunt-only", "status": "running", "stage": "configuration",
              "started_at_utc": datetime.now(timezone.utc).isoformat(), "run_id": os.getenv("GITHUB_RUN_ID", "manual")}
    output = Path(args.summary)
    def persist(**updates):
        report.update(updates)
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_bytes(encode_json(report) + b"\n")
        temporary.chmod(0o600)
        temporary.replace(output)
    exit_code = 0
    try:
        persist()
        if os.getenv("HUNT_ENABLED") != "true" or os.getenv("CREWAI_HUNT_ONLY_CONFIRMED") != "true":
            raise BridgeFault("LIVE_HUNT_NOT_EXPLICITLY_ENABLED", 503)
        if os.getenv("GITHUB_RUN_ATTEMPT", "1") != "1" and not args.resume_kickoff_id:
            raise BridgeFault("RERUN_REQUIRES_RESUME_KICKOFF_ID", 503)
        raw_inputs = os.getenv("CREWAI_INPUTS_JSON")
        if raw_inputs is None and not args.resume_kickoff_id:
            raise BridgeFault("CREW_INPUTS_CONFIGURATION_REQUIRED", 503)
        inputs = decode_json(raw_inputs or "{}")
        transport = Transport()
        ingest = IngestClient(os.getenv("RENDER_INGEST_URL", ""), os.getenv("BRIDGE_INGEST_TOKEN", ""), transport=transport)
        crew = CrewClient(os.getenv("CREWAI_BASE_URL", ""), os.getenv("CREWAI_API_KEY", ""), transport=transport,
            leads_pointer=os.getenv("CREWAI_LEADS_POINTER", "/result/output"))
        try:
            maximum = int(os.getenv("HUNT_MAX_LEADS", "100"))
        except ValueError:
            raise BridgeFault("HUNT_LEAD_LIMIT_CONFIGURATION", 503) from None
        kickoff_id, metrics = run_pipeline(crew, ingest, inputs, resume_id=args.resume_kickoff_id or None,
                                          max_leads=maximum, progress=persist)
        persist(status="passed", stage="complete", kickoff_id=kickoff_id, **metrics)
    except BridgeFault as fault:
        report.update(status="failed", error=fault.code, **fault.metrics)
        try:
            persist()
        except OSError:
            report["summary_write_error"] = True
        exit_code = 1
    except OSError:
        report.update(status="failed", error="SUMMARY_WRITE_FAILED", summary_write_error=True)
        exit_code = 1
    except Exception:
        report.update(status="failed", error="INTERNAL_RUNNER_ERROR")
        try:
            persist()
        except OSError:
            report["summary_write_error"] = True
        exit_code = 1
    print(encode_json(report).decode())
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as summary:
                summary.write("## Hunt-only run\n\n```json\n" + encode_json(report).decode() + "\n```\n")
        except OSError:
            print("::warning::STEP_SUMMARY_WRITE_FAILED", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
