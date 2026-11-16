# Hunt-only lead automation — Case-92

A safety-gated GitHub Actions → deployed CrewAI → Render ingestion → Zoho CRM bridge.

## Schedule

`.github/workflows/hunt_schedule.yml` uses `0 11,15,19 * * *` with GitHub's native `America/New_York` timezone. DST is automatic. GitHub scheduling is best-effort, not an exact-minute SLA.

## Safety state

Live hunting is OFF (`HUNT_ENABLED=false`). Render CRM writes are OFF (`BRIDGE_LIVE_ENABLED=false`, also the application's default). Manual workflow dispatch defaults to **simulate**. A passing simulation is not proof of live provider delivery.

## Run the offline proof

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python -m tests.simulation --summary simulation-summary.json
```

The simulation runs the production ingestion code over local HTTP with loopback-only CrewAI and Zoho stand-ins. It never contacts a live provider or writes a real CRM record.

## Files and configuration

- `daily_job.py`, `lead_bridge/`: hunt runner, authenticated ingestion API, OAuth, duplicate handling, and read-back verification.
- `Dockerfile`, `render.yaml`: non-root application server and an explicit **free-plan** Render deployment specification.
- `BRAIN.md`: setup, configuration, owner-only activation requirements, and actual verification status.
- `docs/API.md`: HTTP contract and canonical lead schema.
- `docs/OPERATIONS.md`: credentials, deployment, retry/resume, and rollback procedures.
- `reports/`: sanitized local test evidence. No lead rows, tokens, or production secrets belong here.

This repository is public. Keep all real credentials in GitHub Actions secrets and Render secret environment variables. Linking an app in Composio does not automatically populate a deployed application's credentials.
