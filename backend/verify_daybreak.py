"""End-to-end verification for DAYBREAK against the live backend (real Cognee, Gemini, Playwright)."""
import json
import sqlite3
import sys
import time
from pathlib import Path

import httpx

BASE = "http://localhost:8000"
DB = Path(__file__).parent / "daybreak.db"
c = httpx.Client(base_url=BASE, timeout=300)
results = []


def check(gate, name, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {gate}: {name}" + (f"  ({detail})" if detail else ""), flush=True)


def run():
    t = time.time()
    r = c.post("/api/run").json()
    rep = c.get("/api/report/latest").json()
    assert rep["run"]["id"] == r["run_id"], "report is not for the run just executed"
    print(f"      run #{r['run_id']} {r['status']} in {time.time()-t:.0f}s, readiness={r['readiness']}", flush=True)
    return r["run_id"], rep


def by_key(rep):
    return {f["key"]: f for f in rep["findings"]}


def artifact(path):
    r = c.get(path)
    return r.status_code, r.headers.get("content-type", ""), r.content


print("== Servers ==")
check("S", "backend :8000 returns 200", c.get("/").status_code == 200)
for port in (3000, 3001):
    try:
        r = httpx.get(f"http://localhost:{port}/", timeout=60)
        is_daybreak = "DAYBREAK" in r.text
        if port == 3001 or is_daybreak:
            check("S", f"frontend :{port} returns 200 and serves DAYBREAK", r.status_code == 200 and is_daybreak, f"HTTP {r.status_code}")
        else:
            print(f"      note: :{port} answers HTTP {r.status_code} but is not DAYBREAK (another app owns that port)")
    except Exception as e:  # noqa: BLE001
        print(f"      note: :{port} not reachable ({type(e).__name__})")

print("== G1-G7: initial demo run ==")
seed = c.post("/api/seed-demo").json()
check("G0", "seed-demo persisted 4 demo sources", {"src_ics", "src_email", "src_url", "src_notes"} <= set(seed["sources"]))
run1, rep = run()
cog, f, reps = rep["cognee"], by_key(rep), rep["repairs"]
steps = {s["step"]: s for s in cog["steps"]}
covered = set(cog["ingested_source_ids"]) | set(cog["already_persisted_source_ids"])
adds = [s for s in cog["steps"] if s["step"] == "add" and not s.get("skipped")]
check("G1", "Cognee Cloud holds all 4 sources (ingested now or persisted from an earlier run)",
      {"src_ics", "src_email", "src_url", "src_notes"} <= covered and cog["ok"] and not cog.get("error"),
      f"ingested={cog['ingested_source_ids']} persisted={cog['already_persisted_source_ids']}")
mem = " ".join(cog["memory_snippets"] + cog.get("memory_chunks", []))
check("G2", "Cognee Cloud search returned memory (HTTP 200, non-empty, on-topic)",
      steps["search"]["http_status"] == 200 and len(cog["memory_snippets"]) > 0 and any(k in mem for k in ("Howard", "Resume", "portfolio", "Portfolio")),
      cog["memory_snippets"][0][:110].replace("\n", " ") if cog["memory_snippets"] else "empty")
gem = rep["extraction"]["gemini"]
check("G2b", "Gemini structured extraction returned the schema fields", gem["ok"] and gem["values"].get("email_location") is not None,
      f"model={gem.get('model')} requested={gem.get('requested_model')} failed={[a['model'] for a in gem.get('failed_attempts', [])]}")
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
rows = con.execute("SELECT key, status, source_ids FROM findings WHERE run_id=?", (run1,)).fetchall()
cited = {sid for _, _, ids in rows for sid in json.loads(ids)}
wal = con.execute("PRAGMA journal_mode").fetchone()[0]
check("G3", "SQLite persisted findings citing src_ics, src_email, src_url, src_notes",
      {"src_ics", "src_email", "src_url", "src_notes"} <= cited and wal == "wal", f"{len(rows)} findings rows, journal_mode={wal}")
web = f["portfolio"]["evidence"]["web"]
code, ctype, body = artifact(web["screenshot"])
check("G4", "Playwright caught a real login redirect and saved a screenshot",
      f["portfolio"]["status"] == "verified_issue" and "/login" in web["final_url"] and web["password_input"] is True
      and code == 200 and "png" in ctype and len(body) > 5000,
      f"{web['url']} -> {web['final_url'][:40]}… HTTP {web['http_status']}, {web['screenshot']} {len(body)} bytes")
check("G5", "Location mismatch detected as verified_issue citing calendar and email",
      f["location"]["status"] == "verified_issue" and f["location"]["source_ids"] == ["src_ics", "src_email"]
      and "Building A" in f["location"]["summary"] and "Building C" in f["location"]["summary"], f["location"]["summary"])
check("G6", "Resume version drift detected as verified_issue citing notes",
      f["resume"]["status"] == "verified_issue" and "src_notes" in f["resume"]["source_ids"]
      and "Resume_v1_2024.pdf" in f["resume"]["summary"] and "Resume_v2_2026_Final.pdf" in f["resume"]["summary"], f["resume"]["summary"])
KW = rep["extraction"]["resolved"]["expected_portfolio_keyword"]["value"]
packet = next((r for r in reps if r["kind"] == "offline_packet"), None)
ok7 = False
detail = "no packet repair"
if packet:
    pc, _, pbody = artifact(packet["artifact_url"])
    sc, sct, sbody = artifact(packet["retest_screenshot"])
    retest = packet["evidence"]["retest"]
    ok7 = (packet["status"] == "verified_fix" and pc == 200 and KW.encode() in pbody and b"Resume_v2_2026_Final.pdf" in pbody
           and retest["passed"] and all(ch["passed"] for ch in retest["checks"]) and sc == 200 and "png" in sct and len(sbody) > 5000)
    detail = f"{packet['artifact_url']} retest_passed={retest['passed']} screenshot={packet['retest_screenshot']}"
check("G7", "Offline HTML packet generated and independently retested in Playwright as verified_fix", ok7, detail)
drafts = [r for r in reps if r["kind"] in ("calendar_update", "recruiter_email")]
check("G7b", "Calendar update and recruiter email drafted as pending_approval (nothing sent)",
      len(drafts) == 2 and all(r["status"] == "pending_approval" for r in drafts))

print("== G9: negative case ==")
c.post("/api/sources", data={"id": "src_g9_incomplete", "title": "Unconfirmed parking note", "content": "parking validated? TBD"})
check("G9a", "custom input persisted in SQLite and can be reopened", c.get("/api/sources/src_g9_incomplete").json()["content"] == "parking validated? TBD")
run2, rep2 = run()
f2 = by_key(rep2)
g9 = f2.get("unverified:src_g9_incomplete")
touching = [x for x in rep2["findings"] if "src_g9_incomplete" in x["source_ids"]] + [r for r in rep2["repairs"] if "g9" in r["finding_key"]]
check("G9b", "incomplete/unverifiable source is marked possible_issue", bool(g9) and g9["status"] == "possible_issue", g9["summary"][:90] if g9 else "missing")
check("G9c", "nothing about it is marked verified_fix or verified_issue", all(x["status"] not in ("verified_fix", "verified_issue") for x in touching))
check("G9d", "failing portfolio URL is still verified_issue (no false fix)", f2["portfolio"]["status"] == "verified_issue")
draft = next(r for r in rep2["repairs"] if r["kind"] == "recruiter_email")
appr = c.post(f"/api/repairs/{draft['id']}/status", json={"status": "approved"})
after = by_key(c.get("/api/report/latest").json())
check("G9e", "approving a draft does not turn its finding into a fix", appr.json()["status"] == "approved" and after["location"]["status"] == "verified_issue")
pk = next(r for r in rep2["repairs"] if r["kind"] == "offline_packet")
check("G9f", "a verified repair cannot be relabelled through the approve endpoint", c.post(f"/api/repairs/{pk['id']}/status", json={"status": "approved"}).status_code == 409)
c.delete("/api/sources/src_g9_incomplete")

print("== Live Twist ==")
before = f2["portfolio"]
upd = c.post("/api/update-url", json={"url": "https://example.com"})
check("LT-", "live twist starts from the failing URL", before["evidence"]["web"]["url"] == "https://github.com/settings/profile")
check("LT0", "update-url saved https://example.com to src_url", upd.status_code == 200 and upd.json()["content"] == "https://example.com")
run3, rep3 = run()
p3 = by_key(rep3)["portfolio"]
w3 = p3["evidence"]["web"]
code, ctype, body = artifact(w3["screenshot"])
check("LT1", "portfolio finding flipped verified_issue -> verified_fix", before["status"] == "verified_issue" and p3["status"] == "verified_fix",
      f"run #{run2} {before['status']} -> run #{run3} {p3['status']}")
check("LT2", "new passing Playwright screenshot (no login wall, keyword found)",
      w3["passed"] and w3["http_status"] == 200 and w3["password_input"] is False and w3["keyword_found"] is True
      and w3["screenshot"] != before["evidence"]["web"]["screenshot"] and code == 200 and "png" in ctype and len(body) > 3000,
      f"{w3['final_url']} HTTP {w3['http_status']}, {w3['screenshot']} {len(body)} bytes")
check("LT3", "no offline packet generated when the live URL passes", not any(r["kind"] == "offline_packet" for r in rep3["repairs"]))
check("LT4", "other verified issues are untouched by the URL fix", by_key(rep3)["location"]["status"] == "verified_issue" and by_key(rep3)["resume"]["status"] == "verified_issue")

c.post("/api/seed-demo")
print(f"\n{sum(results)}/{len(results)} checks passed" + ("" if all(results) else "  <-- FAILURES"))
sys.exit(0 if all(results) else 1)
