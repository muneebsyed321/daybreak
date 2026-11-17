"""DAYBREAK backend — go to sleep with plans, wake up with plans that have been tested."""
import asyncio
import html
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from email.utils import format_datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from google import genai
from pydantic import BaseModel

import cognee_client
import db
import parsers
import web_verify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("daybreak")

# Tried in order; a 503 or timeout moves to the next model. Every attempt is recorded in the report.
GEMINI_MODELS = ["gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-4-hydrogen"]
GEMINI_MODEL = GEMINI_MODELS[0]
PROGRESS = {"stage": None, "run_id": None}


def stage(text):
    PROGRESS["stage"] = text
    log.info("stage: %s", text)
DEMO_URL = "https://github.com/settings/profile"
ROLE_IDS = {"calendar": ("src_ics", "ics"), "email": ("src_email", "eml"), "url": ("src_url", "url"), "notes": ("src_notes", "note")}
FIELD_ROLE = {"event_title": "calendar", "event_time": "calendar", "calendar_location": "calendar",
              "email_location": "email", "portfolio_url": "url", "expected_portfolio_keyword": "email",
              "referenced_resume": "notes", "latest_resume": "notes"}

_run_lock = asyncio.Lock()
_genai_client = None
scheduler = AsyncIOScheduler()


class Extraction(BaseModel):
    event_title: str | None = None
    event_time: str | None = None
    calendar_location: str | None = None
    email_location: str | None = None
    portfolio_url: str | None = None
    expected_portfolio_keyword: str | None = None
    referenced_resume: str | None = None
    latest_resume: str | None = None


# ---------------------------------------------------------------------------
# Hypothesis generation (Gemini). Output is never trusted without a literal quote in the cited source.
# ---------------------------------------------------------------------------
async def gemini_extract(sources: list[dict], memory: list[str]) -> dict:
    global _genai_client
    docs = "\n\n".join(cognee_client.source_document(s) for s in sources)
    mem = "\n".join(f"- {m[:500]}" for m in memory[:6]) or "(none)"
    prompt = (
        "You are preparing for tomorrow's event. Extract fields from the SOURCES below. Copy values verbatim "
        "from the sources; use null when a value is not stated.\n"
        "- event_title, event_time: from the calendar (.ics) source\n"
        "- calendar_location: the location stated in the calendar source\n"
        "- email_location: the location stated in the recruiter email\n"
        "- portfolio_url: the portfolio URL to be shown\n"
        "- expected_portfolio_keyword: the exact text the email says the portfolio page should show\n"
        "- referenced_resume: resume filename the deck/presentation currently links to\n"
        "- latest_resume: resume filename described as the latest\n\n"
        f"SOURCES:\n{docs}\n\nMEMORY CONTEXT FROM COGNEE (may be stale, lower priority than sources):\n{mem}"
    )
    t0 = time.time()
    attempts = []
    for model in GEMINI_MODELS:
        try:
            if _genai_client is None:
                _genai_client = genai.Client()
            resp = await asyncio.wait_for(
                _genai_client.aio.models.generate_content(
                    model=model, contents=prompt,
                    config={"response_mime_type": "application/json", "response_schema": Extraction, "temperature": 0},
                ), timeout=15)
            parsed = resp.parsed if isinstance(resp.parsed, Extraction) else Extraction.model_validate_json(resp.text)
            return {"ok": True, "model": model, "requested_model": GEMINI_MODEL, "failed_attempts": attempts,
                    "values": parsed.model_dump(), "elapsed_s": round(time.time() - t0, 1)}
        except Exception as e:
            log.warning("gemini extraction with %s failed: %s", model, str(e)[:200])
            attempts.append({"model": model, "error": f"{type(e).__name__}: {e}"[:300]})
    return {"ok": False, "model": None, "requested_model": GEMINI_MODEL, "failed_attempts": attempts, "values": {},
            "error": attempts[-1]["error"], "elapsed_s": round(time.time() - t0, 1)}


def pick_roles(sources: list[dict]) -> tuple[dict, list[dict]]:
    roles = {}
    for role, (sid, typ) in ROLE_IDS.items():
        match = next((s for s in sources if s["id"] == sid), None)
        if not match:
            cands = [s for s in sources if s["type"] == typ]
            match = max(cands, key=lambda s: s["updated_at"]) if cands else None
        roles[role] = match
    used = {s["id"] for s in roles.values() if s}
    return roles, [s for s in sources if s["id"] not in used]


def grounded(value: str | None, source: dict | None) -> bool:
    if not value or not source:
        return False
    return value.lower() in source["content"].lower() or (len(parsers.norm(value)) > 2 and parsers.norm(value) in parsers.norm(source["content"]))


def resolve(gem: dict, det: dict, roles: dict) -> dict:
    """Per field: accept the Gemini hypothesis only if it literally appears in the cited source; else parser value."""
    out = {}
    for field, role in FIELD_ROLE.items():
        src = roles.get(role)
        g, d = gem.get(field), det.get(field)
        if grounded(g, src):
            out[field] = {"value": g.strip(), "origin": "gemini (quote found in source)", "source_id": src["id"]}
        elif d:
            out[field] = {"value": d, "origin": "deterministic parser" + (" (gemini value not found in source)" if g else ""),
                          "source_id": src["id"] if src else None}
        else:
            out[field] = {"value": None, "origin": "not found" + (f" (ungrounded gemini guess discarded: {g!r})" if g else ""),
                          "source_id": src["id"] if src else None}
    return out


def cite(source: dict | None, needle: str | None) -> dict | None:
    if not source:
        return None
    quote = parsers.quote_containing(source["content"], needle or "")
    if not quote and needle:
        words = parsers.norm(needle).split()
        quote = next((ln.strip() for ln in source["content"].splitlines() if words and all(w in parsers.norm(ln) for w in words)), None)
    return {"source_id": source["id"], "title": source["title"], "type": source["type"], "quote": quote or source["content"][:200]}


def memory_mentions(cog: dict, *values) -> list[str]:
    hay = (cog.get("memory_snippets") or []) + (cog.get("memory_chunks") or [])
    hits = []
    for snip in hay:
        if any(v and parsers.norm(v) in parsers.norm(snip) for v in values):
            hits.append(snip[:400])
    return hits[:3]


# ---------------------------------------------------------------------------
# Safe repairs
# ---------------------------------------------------------------------------
def write_packet(run_id: int, hyp: dict, web: dict) -> str:
    v = lambda k: html.escape(hyp[k]["value"] or "—")
    failed = "".join(f"<li>{html.escape(c['name'])}: {html.escape(c['detail'])}</li>" for c in web["checks"] if not c["passed"])
    name = f"run_{run_id}_portfolio_packet.html"
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Offline Portfolio Packet</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:40px}}
main{{max-width:760px;margin:auto}}h1{{font-size:34px;margin:0 0 6px}}h2{{font-size:15px;text-transform:uppercase;letter-spacing:.08em;color:#94a3b8;margin:28px 0 8px}}
.card{{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:18px 22px}}code{{color:#6ee7b7}}li{{margin:4px 0}}.muted{{color:#94a3b8;font-size:13px}}</style></head>
<body><main>
<p class="muted">DAYBREAK offline backup packet · generated by run #{run_id} · opens with no network and no sign-in</p>
<h1>Portfolio backup packet</h1>
<p>For <strong>{v('event_title')}</strong> ({v('event_time')}).</p>
<p>Page marker the panel will look for: <code>{v('expected_portfolio_keyword')}</code></p>
<h2>Why this packet exists</h2>
<div class="card"><p>The live portfolio link <code>{html.escape(web['url'])}</code> failed an unauthenticated check, so the panel cannot open it:</p><ul>{failed}</ul></div>
<h2>Resume</h2>
<div class="card"><p>Latest resume to bring / attach: <code>{v('latest_resume')}</code></p>
<p class="muted">The deck currently links to <code>{v('referenced_resume')}</code>.</p></div>
<h2>Where to go</h2>
<div class="card"><p>Calendar says: {v('calendar_location')}<br>Recruiter email says: {v('email_location')}</p>
<p class="muted">These disagree — confirm with the recruiter before leaving.</p></div>
</main></body></html>"""
    (db.ARTIFACTS_DIR / name).write_text(doc, encoding="utf-8")
    return name


# ---------------------------------------------------------------------------
# The single overnight worker — used by APScheduler and POST /api/run
# ---------------------------------------------------------------------------
async def run_preflight(trigger: str) -> int:
    async with _run_lock:
        run_id = db.create_run(trigger)
        PROGRESS["run_id"] = run_id
        log.info("run %s started (%s)", run_id, trigger)
        try:
            await _execute(run_id)
        except Exception as e:
            log.exception("run %s failed", run_id)
            db.finish_run(run_id, status="failed", error=f"{type(e).__name__}: {e}"[:800], readiness="unknown")
        finally:
            PROGRESS["stage"] = None
        return run_id


async def _execute(run_id: int) -> None:
    sources = db.list_sources()
    roles, extras = pick_roles(sources)

    # 1) Cognee Cloud memory (hypothesis context) ---------------------------------
    stage("Syncing sources to Cognee Cloud and retrieving memory")
    cog = await cognee_client.sync_and_search(sources) if sources else {"ok": False, "steps": [], "memory_snippets": [], "note": "no sources"}

    # 2) Gemini structured extraction (hypothesis) + deterministic cross-check ----
    stage("Gemini structured extraction (hypotheses)")
    gem = await gemini_extract(sources, cog.get("memory_snippets") or []) if sources else {"ok": False, "values": {}}
    det = parsers.deterministic_extract(roles)
    hyp = resolve(gem.get("values") or {}, det, roles)
    val = {k: h["value"] for k, h in hyp.items()}
    extraction = {"gemini": gem, "deterministic": det, "resolved": hyp,
                  "note": "Cognee + Gemini output is hypothesis only; every status below comes from an independent check."}

    findings: list[dict] = []

    def finding(key, title, status, summary, source_ids, evidence):
        fid = db.add_finding(run_id, key, title, status, summary, source_ids, evidence)
        findings.append({"id": fid, "key": key, "title": title, "status": status, "summary": summary,
                         "source_ids": source_ids, "evidence": evidence})

    def prior(key):
        p = db.previous_finding(key, run_id)
        return p, (p["status"] if p else None)

    # 3a) Location: direct source-to-source comparison ----------------------------
    cal, eml = roles["calendar"], roles["email"]
    cl, el = val["calendar_location"], val["email_location"]
    loc_ids = [s["id"] for s in (cal, eml) if s]
    loc_ev = {"check": "source-to-source comparison", "calendar_location": cl, "email_location": el,
              "citations": [c for c in (cite(cal, cl), cite(eml, el)) if c],
              "cognee_memory": memory_mentions(cog, cl, el), "hypothesis_origin": {"calendar_location": hyp["calendar_location"]["origin"], "email_location": hyp["email_location"]["origin"]}}
    prev, prev_status = prior("location")
    if cl and el and parsers.norm(cl) != parsers.norm(el):
        finding("location", "Location contradiction", "verified_issue",
                f'Calendar says "{cl}" but the recruiter email says "{el}".', loc_ids, loc_ev)
    elif cl and el:
        st = "verified_fix" if prev_status in ("verified_issue", "verified_fix") else "verified_ok"
        loc_ev["previous"] = {"run_id": prev["run_id"], "status": prev_status} if prev else None
        finding("location", "Location consistent", st, f'Calendar and email agree on "{cl}".', loc_ids, loc_ev)
    else:
        missing = [n for n, v in (("calendar location", cl), ("email location", el)) if not v]
        finding("location", "Location cannot be cross-checked", "possible_issue",
                f"Missing input: {', '.join(missing)}. Nothing to compare, so the location is unverified.", loc_ids, loc_ev)

    # 3b) Resume version: deterministic filename comparison -----------------------
    notes = roles["notes"]
    rr, lr = val["referenced_resume"], val["latest_resume"]
    res_ids = [notes["id"]] if notes else []
    res_ev = {"check": "deterministic file-version comparison", "referenced_resume": rr, "latest_resume": lr,
              "referenced_version": list(parsers.resume_version(rr or "")), "latest_version": list(parsers.resume_version(lr or "")),
              "citations": [c for c in (cite(notes, rr), cite(notes, lr)) if c], "cognee_memory": memory_mentions(cog, rr, lr)}
    prev, prev_status = prior("resume")
    if rr and lr and rr != lr:
        newer = parsers.resume_version(lr) > parsers.resume_version(rr)
        if newer:
            finding("resume", "Resume version drift", "verified_issue",
                    f"Deck links to {rr}, but {lr} is the latest file.", res_ids, res_ev)
        else:
            finding("resume", "Resume versions differ (order unclear)", "possible_issue",
                    f"Deck links to {rr}; notes call {lr} the latest, but filenames do not prove which is newer.", res_ids, res_ev)
    elif rr and lr:
        st = "verified_fix" if prev_status in ("verified_issue", "verified_fix") else "verified_ok"
        finding("resume", "Resume version current", st, f"Deck links to the latest resume ({lr}).", res_ids, res_ev)
    else:
        finding("resume", "Resume version cannot be checked", "possible_issue",
                "Missing input: no referenced and latest resume filenames found in notes.", res_ids, res_ev)

    # 3c) Portfolio URL: independent Playwright check, fresh context --------------
    url_src = roles["url"]
    url, kw = val["portfolio_url"], val["expected_portfolio_keyword"]
    port_ids = [s["id"] for s in (url_src, eml) if s]
    prev, prev_status = prior("portfolio")
    web = None
    if not url:
        finding("portfolio", "Portfolio URL missing", "possible_issue",
                "No portfolio URL was provided, so public access could not be tested.", port_ids,
                {"check": "playwright", "citations": [c for c in (cite(eml, kw),) if c]})
    else:
        stage("Playwright: opening portfolio URL with no session")
        web = await web_verify.check_url(url, kw, f"run_{run_id}_portfolio_check.png")
        port_ev = {"check": "playwright unauthenticated (fresh context, zero cookies)", "web": web, "expected_keyword": kw,
                   "citations": [c for c in (cite(url_src, url), cite(eml, kw)) if c],
                   "cognee_memory": memory_mentions(cog, url, kw),
                   "previous": {"run_id": prev["run_id"], "status": prev_status,
                                "url": ((prev.get("evidence") or {}).get("web") or {}).get("url")} if prev else None}
        failed = [c for c in web["checks"] if not c["passed"]]
        if web["hard_failure"]:
            finding("portfolio", "Portfolio URL not publicly reachable", "verified_issue",
                    f"{url} failed unauthenticated access: " + "; ".join(c["detail"] for c in failed), port_ids, port_ev)
        elif not kw:
            finding("portfolio", "Portfolio URL loads, content unconfirmed", "possible_issue",
                    f"{url} loaded without a login wall, but there is no expected keyword to confirm it is the right page.", port_ids, port_ev)
        else:
            st = "verified_fix" if prev_status in ("verified_issue", "verified_fix") else "verified_ok"
            lead = "Fixed: " if st == "verified_fix" else ""
            finding("portfolio", "Portfolio URL publicly reachable", st,
                    f'{lead}{url} opened with no session (HTTP {web["http_status"]}), no login wall, and shows "{kw}".', port_ids, port_ev)

    # 3d) Anything else is context we have no independent check for ---------------
    for s in extras:
        empty = len(s["content"].strip()) < 20
        finding(f"unverified:{s['id']}", f"Unverified input: {s['title']}", "possible_issue",
                ("Input is incomplete (almost no content). " if empty else "") +
                "No independent check exists for this input, so its claims are treated as unverified.",
                [s["id"]], {"check": "none available", "citations": [cite(s, None)]})

    # 4) Safe repairs -------------------------------------------------------------
    by_key = {f["key"]: f for f in findings}
    repairs_meta = []
    if by_key["portfolio"]["status"] == "verified_issue" and web:
        stage("Generating offline packet and retesting it in Playwright")
        name = write_packet(run_id, hyp, web)
        packet_path = db.ARTIFACTS_DIR / name
        retest = await web_verify.check_url(packet_path.as_uri(), kw, f"run_{run_id}_packet_retest.png")
        ok = retest["passed"]
        db.add_repair(run_id, "portfolio", "offline_packet", "Offline portfolio backup packet",
                      "verified_fix" if ok else "possible_issue",
                      "Standalone HTML packet that opens with no network and no sign-in. Retested in a fresh Playwright "
                      "context. This is a backup — the live URL itself is still not public.",
                      f"/artifacts/{name}", retest["screenshot"], {"retest": retest})
        repairs_meta.append({"status": "verified_fix" if ok else "possible_issue", "artifact": f"/artifacts/{name}"})
    resume_issue = by_key["resume"]["status"] == "verified_issue"
    if by_key["location"]["status"] == "verified_issue":
        db.add_repair(run_id, "location", "calendar_update", "Draft: update calendar location", "pending_approval",
                      f"Event: {val['event_title']}\nChange LOCATION\n  from: {cl}\n  to:   {el}\n\n"
                      "Basis: the recruiter email states a different room than the calendar entry. Which one is correct is "
                      "not verified — approve only after the recruiter confirms. DAYBREAK does not modify your calendar.",
                      evidence={"from": cl, "to": el})
        extra = f"\n\nI'll also bring the latest version of my resume ({lr})." if resume_issue else ""
        db.add_repair(run_id, "location", "recruiter_email", "Draft: confirm location with recruiter", "pending_approval",
                      f"To: {parsers.parse_eml(eml['content'])['from'] if eml else 'recruiter'}\n"
                      f"Subject: Confirming location for {val['event_title']}\n\nHi,\n\n"
                      f"My calendar invite still shows {cl}, while your email mentions {el}. "
                      f"Could you confirm that {el} is the right place to check in tomorrow?{extra}\n\nThank you!\n\n"
                      "[Draft only — DAYBREAK never sends messages.]", evidence={"calendar": cl, "email": el})
    if resume_issue:
        db.add_repair(run_id, "resume", "deck_link_update", "Draft: point deck at latest resume", "pending_approval",
                      f"In the presentation deck, replace the link to {rr} with {lr}.\n\n"
                      "Draft only — DAYBREAK does not edit your files.", evidence={"from": rr, "to": lr})

    # 5) Dependency graph ---------------------------------------------------------
    graph = build_graph(val, roles, extras, by_key, repairs_meta)
    n_ver = sum(1 for f in findings if f["status"] == "verified_issue")
    n_pos = sum(1 for f in findings if f["status"] == "possible_issue")
    readiness = "not_ready" if n_ver else ("at_risk" if n_pos else "ready")
    db.finish_run(run_id, status="completed", readiness=readiness, extraction=extraction, cognee=cog, graph=graph)
    log.info("run %s completed: %s (%s verified, %s possible)", run_id, readiness, n_ver, n_pos)


def build_graph(val, roles, extras, by_key, repairs_meta) -> dict:
    nodes, edges = [], []

    def node(id, kind, label, sub=None, status=None, finding_key=None, source_ids=None):
        nodes.append({"id": id, "kind": kind, "label": label, "sublabel": sub, "status": status,
                      "finding_key": finding_key, "source_ids": source_ids or []})

    def edge(a, b, label=None):
        edges.append({"id": f"{a}->{b}", "source": a, "target": b, "label": label})

    worst = lambda sts: next((s for s in ("verified_issue", "possible_issue", "verified_fix", "verified_ok") if s in sts), None)
    title = val.get("event_title") or "Tomorrow's event"
    node("event", "event", "9:00 AM Interview" if "9:00" in title else title, title, worst([f["status"] for f in by_key.values()]))
    deps = [("location", "Location", ["calendar", "email"]), ("portfolio", "Portfolio URL", ["url", "email"]),
            ("resume", "Resume Version", ["notes"])]
    src_status: dict[str, list] = {}
    for key, label, rls in deps:
        f = by_key[key]
        node(f"dep_{key}", "dependency", label, f["title"], f["status"], key, f["source_ids"])
        edge("event", f"dep_{key}", "depends on")
        for r in rls:
            s = roles.get(r)
            if s:
                src_status.setdefault(s["id"], []).append(f["status"])
                edge(f"dep_{key}", s["id"], "cited from")
    for s in [x for x in roles.values() if x]:
        fk = next((k for k, _, rls in deps if any(roles.get(r) and roles[r]["id"] == s["id"] for r in rls)), None)
        node(s["id"], "source", s["id"], f"{s['type']} · {s['title']}", worst(src_status.get(s["id"], [])), fk, [s["id"]])
    for s in extras:
        key = f"unverified:{s['id']}"
        node(s["id"], "source", s["id"], f"{s['type']} · {s['title']}", by_key[key]["status"], key, [s["id"]])
        edge("event", s["id"], "context")
    for r in repairs_meta:
        node("repair_packet", "repair", "Offline Backup Packet", "Playwright retest", r["status"], "portfolio", [])
        edge("dep_portfolio", "repair_packet", "repaired by")
    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    scheduler.add_job(run_preflight, "cron", hour=2, minute=0, args=["scheduled"], id="overnight", replace_existing=True)
    scheduler.start()
    log.info("overnight job scheduled, next run %s", scheduler.get_job("overnight").next_run_time)
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="DAYBREAK", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/artifacts", StaticFiles(directory=str(db.ARTIFACTS_DIR)), name="artifacts")


@app.get("/")
def health():
    job = scheduler.get_job("overnight")
    return {"service": "daybreak", "ok": True, "next_overnight_run": str(job.next_run_time) if job else None}


def demo_sources() -> list[dict]:
    day = datetime.now().astimezone() + timedelta(days=1)
    d = day.strftime("%Y%m%d")
    ics = "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Northstar Labs//Recruiting//EN", "BEGIN:VEVENT",
        "UID:pd-interview-001@northstarlabs.example", f"DTSTAMP:{datetime.now().strftime('%Y%m%d')}T080000",
        f"DTSTART;TZID=America/Los_Angeles:{d}T090000", f"DTEND;TZID=America/Los_Angeles:{d}T100000",
        "SUMMARY:9:00 AM Product Design Interview — Northstar Labs", "LOCATION:Building A, 400 Howard St",
        "DESCRIPTION:Onsite portfolio review with the design team. Bring portfolio and resume.",
        "END:VEVENT", "END:VCALENDAR", ""])
    eml = "\n".join([
        "From: Priya Raman <priya.raman@northstarlabs.example>", "To: Alex Morgan <alex.morgan@example.com>",
        "Subject: Updated room for tomorrow's Product Design Interview",
        f"Date: {format_datetime(datetime.now().astimezone().replace(hour=16, minute=5, second=0, microsecond=0))}", "MIME-Version: 1.0",
        'Content-Type: text/plain; charset="utf-8"', "",
        "Hi Alex,", "",
        "Quick update for tomorrow's 9:00 AM Product Design Interview: we've moved the room. "
        "Please come to Building C, 450 Howard St and check in at the front desk.", "",
        "Please make sure your portfolio link opens for the panel without signing in - we'll check that the page "
        'shows the text "documentation examples". Also bring or send the latest version of your resume.', "",
        "See you tomorrow,", "Priya", ""])
    notes = ("Prep notes for tomorrow's interview.\n"
             "Presentation deck (Northstar_Portfolio_Deck.key), slide 2 'About me', links to Resume_v1_2024.pdf.\n"
             "Resume_v2_2026_Final.pdf is the latest file in the ~/Documents/Resumes folder.\n")
    return [
        {"id": "src_ics", "type": "ics", "title": "Calendar invite — Product Design Interview", "content": ics, "filename": "interview.ics"},
        {"id": "src_email", "type": "eml", "title": "Recruiter email — updated room", "content": eml, "filename": "recruiter_update.eml"},
        {"id": "src_url", "type": "url", "title": "Portfolio URL", "content": DEMO_URL, "filename": None},
        {"id": "src_notes", "type": "note", "title": "Prep notes — deck and resume files", "content": notes, "filename": None},
    ]


@app.post("/api/seed-demo")
def seed_demo():
    for s in demo_sources():
        db.upsert_source(**s)
    return {"ok": True, "sources": [s["id"] for s in db.list_sources()]}


@app.get("/api/sources")
def list_sources():
    return db.list_sources()


@app.get("/api/sources/{source_id}")
def get_source(source_id: str):
    s = db.get_source(source_id)
    if not s:
        raise HTTPException(404, "source not found")
    return s


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: str):
    db.delete_source(source_id)
    return {"ok": True}


@app.post("/api/sources")
async def create_source(request: Request):
    ctype = request.headers.get("content-type", "")
    upload = None
    if "application/json" in ctype:
        data = await request.json()
    else:
        form = await request.form()
        data = {k: v for k, v in form.items() if isinstance(v, str)}
        upload = form.get("file") if not isinstance(form.get("file"), str) else None
    sid = (data.get("id") or "").strip() or f"src_{uuid.uuid4().hex[:8]}"
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", sid):
        raise HTTPException(400, "invalid id")
    meta, filename = {}, None
    if upload is not None and getattr(upload, "filename", None):
        filename = upload.filename
        raw = await upload.read()
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext == "pdf":
            path = db.BASE_DIR / "uploads"
            path.mkdir(exist_ok=True)
            fpath = path / f"{sid}.pdf"
            fpath.write_bytes(raw)
            content, meta = await parsers.parse_pdf(fpath)
            stype = "pdf"
        elif ext in ("ics", "eml"):
            content, stype = raw.decode("utf-8", errors="replace"), ext
            meta = {"parser": "lightweight", "fields": parsers.parse_ics(content) if ext == "ics" else
                    {k: v for k, v in parsers.parse_eml(content).items() if k != "body"}}
        else:
            raise HTTPException(400, "only .ics, .eml and .pdf uploads are supported")
        title = data.get("title") or filename
    else:
        content = (data.get("content") or "").strip()
        stype = data.get("type") or ("url" if re.fullmatch(r"https?://\S+", content) else "note")
        if stype not in ("note", "url", "ics", "eml"):
            raise HTTPException(400, "type must be note, url, ics or eml")
        title = data.get("title") or ("URL" if stype == "url" else "Note")
    return db.upsert_source(sid, stype, title, content, filename, meta)


class UrlUpdate(BaseModel):
    url: str


@app.post("/api/update-url")
def update_url(body: UrlUpdate):
    url = body.url.strip()
    if not re.fullmatch(r"https?://\S+", url):
        raise HTTPException(400, "url must start with http:// or https://")
    existing = db.get_source("src_url")
    return db.upsert_source("src_url", "url", existing["title"] if existing else "Portfolio URL", url)


@app.post("/api/run")
async def run_now():
    run_id = await run_preflight("manual")
    run = db.one("SELECT id, status, readiness, error FROM runs WHERE id=?", (run_id,))
    return {"run_id": run_id, **run}


class RepairStatus(BaseModel):
    status: str


@app.post("/api/repairs/{repair_id}/status")
def set_repair_status(repair_id: int, body: RepairStatus):
    if body.status not in ("approved", "dismissed", "pending_approval"):
        raise HTTPException(400, "status must be approved, dismissed or pending_approval")
    r = db.one("SELECT * FROM repairs WHERE id=?", (repair_id,))
    if not r:
        raise HTTPException(404, "repair not found")
    if r["status"] not in ("pending_approval", "approved", "dismissed"):
        raise HTTPException(409, f"repair is {r['status']}; only draft repairs can be approved or dismissed")
    db.execute("UPDATE repairs SET status=?, updated_at=? WHERE id=?", (body.status, db.now(), repair_id))
    return db.one("SELECT * FROM repairs WHERE id=?", (repair_id,))


@app.get("/api/report/latest")
def report_latest():
    run = db.latest_run()
    job = scheduler.get_job("overnight")
    base = {"sources": db.list_sources(), "running": _run_lock.locked(),
            "next_overnight_run": str(job.next_run_time) if job else None}
    if not run:
        return {**base, "run": None, "cognee": None, "extraction": None, "graph": {"nodes": [], "edges": []}, "findings": [], "repairs": []}
    cognee, extraction, graph = run.pop("cognee"), run.pop("extraction"), run.pop("graph")
    return {**base, "run": run, "cognee": cognee, "extraction": extraction, "graph": graph or {"nodes": [], "edges": []},
            "findings": db.findings_for(run["id"]), "repairs": db.repairs_for(run["id"])}


@app.get("/api/status")
def status():
    return {"running": _run_lock.locked(), "stage": PROGRESS["stage"], "run_id": PROGRESS["run_id"]}


@app.get("/api/runs")
def runs():
    return db.query("SELECT id, trigger, status, readiness, started_at, finished_at, error FROM runs ORDER BY id DESC LIMIT 20")
