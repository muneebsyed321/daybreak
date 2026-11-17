---
name: daybreak
description: Run a live DAYBREAK preflight against the local backend (http://localhost:8000) and report tomorrow's readiness, verified issues, possible issues and verified fixes with their evidence. Use when the user types /daybreak, or asks to check, test or preflight tomorrow's plans, interview, or the Morning Report.
argument-hint: "[demo | url <https://...>]"
allowed-tools: Bash
---

# DAYBREAK preflight

Run the script from the project root. It needs the backend on `:8000`.

```bash
.claude/skills/daybreak/scripts/preflight.sh            # run against current inputs
.claude/skills/daybreak/scripts/preflight.sh demo       # reseed demo inputs first
.claude/skills/daybreak/scripts/preflight.sh url https://example.com   # Live Twist
```

If the script prints `backend down`, start it:

```bash
cd backend && nohup .venv/bin/uvicorn main:app --port 8000 > uvicorn.log 2>&1 &
```

## Report back

1. Readiness (`ready`, `at_risk`, `not_ready`) and the run number.
2. Each finding: status, one-line summary, cited source ids.
3. Repairs: the offline packet link (`http://localhost:8000/artifacts/...`) and any drafts waiting for approval.

## Rules

- Only `verified_issue` and `verified_fix` are proven. Call `possible_issue` unverified.
- Cognee memory and Gemini output are hypotheses. Never quote them as proof.
- Drafts stay drafts. Do not approve, send, or edit a calendar unless the user asks.
