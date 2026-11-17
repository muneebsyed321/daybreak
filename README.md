# DAYBREAK

Go to sleep with plans. Wake up with plans that have been tested.

DAYBREAK reads your calendar invite, recruiter email, portfolio link and prep notes overnight. It checks each dependency of tomorrow's event against the real world and hands you a Morning Report with evidence.

## How it decides

| Status | Meaning |
|---|---|
| `verified_issue` | Proven: a login wall in a cookie-free browser, two sources that contradict each other, or a stale file version |
| `possible_issue` | Missing input or no independent check exists |
| `verified_fix` | A later independent check passed |

Cognee Cloud memory and Gemini extraction only propose values. A value counts only after DAYBREAK finds it quoted in a source. Playwright opens URLs in a fresh context with zero cookies, so a 200 that lands on `/login` fails. When the portfolio fails, DAYBREAK builds an offline HTML packet and retests that file in Playwright. Calendar and email repairs stay drafts until you approve them, and DAYBREAK sends nothing.

## Stack

- `backend/`: FastAPI, SQLite (WAL), APScheduler (02:00 nightly), Playwright, Docling with a pypdf fallback, Cognee Cloud, Gemini
- `frontend/`: Next.js, Tailwind, React Flow

## Run

```bash
export COGNEE_SERVICE_URL=... COGNEE_API_KEY=... COGNEE_TENANT_ID=... GEMINI_API_KEY=...

cd backend
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python fastapi "uvicorn[standard]" pydantic apscheduler playwright pypdf google-genai httpx python-multipart docling
.venv/bin/python -m playwright install chromium
.venv/bin/uvicorn main:app --port 8000

cd ../frontend && npm install && npm run dev -- -p 3001
```

Open http://localhost:3001 and click **1. Load Demo & Run**.

## Verify

```bash
backend/.venv/bin/python backend/verify_daybreak.py
```

The script runs the demo, a negative case and the Live Twist (GitHub login URL to example.com) against the live services and prints one line per gate.

## Claude Code

Type `/daybreak` in this repo to run a preflight and get the report in your terminal. See `.claude/skills/daybreak/`.
