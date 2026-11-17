"""Cognee Cloud HTTP client. Everything it returns is treated as hypothesis context, never as verification."""
import asyncio
import hashlib
import json
import logging
import os
import re
import time

import httpx

import db

log = logging.getLogger("daybreak.cognee")
DATASET = "daybreak_tomorrow"
SEARCH_QUERY = "interview location portfolio url resume version"
COGNIFY_WAIT_S = float(os.environ.get("DAYBREAK_COGNIFY_WAIT_S", "40"))


def _base() -> str:
    return os.environ["COGNEE_SERVICE_URL"].rstrip("/")


def _headers() -> dict:
    return {"X-Api-Key": os.environ["COGNEE_API_KEY"], "X-Tenant-Id": os.environ["COGNEE_TENANT_ID"]}


def source_document(s: dict) -> str:
    return f"[SOURCE_ID={s['id']} | TYPE={s['type']} | TITLE={s['title']}]\n{s['content']}"


def _hash(s: dict) -> str:
    return hashlib.sha256(source_document(s).encode()).hexdigest()


def _short(resp: httpx.Response, limit: int = 1500):
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    text = json.dumps(body) if not isinstance(body, str) else body
    return body, text[:limit]


def _snippets(payload, out: list, depth: int = 0) -> None:
    """Flatten whatever shape search returned into readable text snippets."""
    if depth > 6 or len(out) >= 12:
        return
    if isinstance(payload, str):
        t = payload.strip()
        if t and t not in out:
            out.append(t[:1200])
    elif isinstance(payload, list):
        for item in payload:
            _snippets(item, out, depth + 1)
    elif isinstance(payload, dict):
        for key in ("search_result", "searchResult", "text", "answer", "content", "result", "results", "context", "description", "name"):
            if key in payload and payload[key]:
                _snippets(payload[key], out, depth + 1)


async def sync_and_search(sources: list[dict]) -> dict:
    """add (changed sources only) -> cognify -> search. Returns a log of real Cognee Cloud responses."""
    result = {"dataset": DATASET, "service": _base(), "steps": [], "ingested_source_ids": [],
              "already_persisted_source_ids": [], "query": SEARCH_QUERY, "memory_snippets": [], "ok": False}
    t0 = time.time()
    try:
        async with httpx.AsyncClient(base_url=_base(), headers=_headers(), timeout=90, follow_redirects=True) as client:
            changed = [s for s in sources if s.get("cognee_hash") != _hash(s)]
            result["already_persisted_source_ids"] = [s["id"] for s in sources if s not in changed]
            added_new = False
            for s in changed:
                files = {"data": (f"{s['id']}.txt", source_document(s).encode(), "text/plain")}
                r = await client.post("/api/v1/add", data={"datasetName": DATASET}, files=files)
                _, text = _short(r, 600)
                result["steps"].append({"step": "add", "source_id": s["id"], "http_status": r.status_code, "response": text})
                log.info("cognee add %s -> %s %s", s["id"], r.status_code, text[:200])
                added_new = added_new or r.status_code < 300
                if r.status_code == 409:
                    # add() never overwrites: a changed source must replace its stored document via PATCH /update.
                    m = re.search(r"data_id[ =]([0-9a-f\-]{36}).*?dataset ([0-9a-f\-]{36})", text, re.S)
                    if m:
                        files = {"data": (f"{s['id']}.txt", source_document(s).encode(), "text/plain")}
                        r = await client.patch("/api/v1/update", params={"data_id": m.group(1), "dataset_id": m.group(2)}, files=files)
                        _, text = _short(r, 600)
                        result["steps"].append({"step": "update", "source_id": s["id"], "http_status": r.status_code, "response": text})
                        log.info("cognee update %s -> %s %s", s["id"], r.status_code, text[:200])
                if r.status_code < 300:
                    db.mark_cognee_synced(s["id"], _hash(s))
                    result["ingested_source_ids"].append(s["id"])

            if changed and not added_new:
                result["steps"].append({"step": "cognify", "skipped": True,
                                        "note": "only existing documents changed; PATCH /update rebuilt their graph regions incrementally"})
            elif changed:
                r = await client.post("/api/v1/cognify", json={"datasets": [DATASET]})
                body, text = _short(r, 800)
                result["steps"].append({"step": "cognify", "http_status": r.status_code, "response": text})
                log.info("cognee cognify -> %s %s", r.status_code, text[:300])
                # cognify runs server-side in the background; wait (bounded) for the graph build to finish.
                deadline = time.time() + COGNIFY_WAIT_S
                last = None
                while r.status_code < 300 and time.time() < deadline:
                    await asyncio.sleep(1.5)
                    st = await client.get("/api/v1/datasets/status", params={"pipeline": "cognify_pipeline"})
                    _, last = _short(st, 600)
                    if st.status_code >= 300 or ("STARTED" not in last and "INITIATED" not in last):
                        break
                if last is not None:
                    result["steps"].append({"step": "cognify_status", "response": last,
                                            "waited_s": round(time.time() - (deadline - COGNIFY_WAIT_S), 1)})
            else:
                result["steps"].append({"step": "add", "skipped": True,
                                        "note": "all sources already persisted in Cognee Cloud from earlier runs"})

            r = await client.post("/api/v1/search", json={"query": SEARCH_QUERY, "datasets": [DATASET]})
            body, text = _short(r, 3000)
            snippets: list[str] = []
            if r.status_code < 300:
                _snippets(body, snippets)
            result["steps"].append({"step": "search", "http_status": r.status_code, "response": text})
            log.info("cognee search -> %s %s", r.status_code, text[:400])

            # Raw persisted chunks: shows the graph memory itself, independent of the LLM-written answer.
            r2 = await client.post("/api/v1/search", json={"query": SEARCH_QUERY, "datasets": [DATASET],
                                                           "searchType": "CHUNKS", "topK": 6})
            body2, text2 = _short(r2, 3000)
            chunks: list[str] = []
            if r2.status_code < 300:
                _snippets(body2, chunks)
            result["steps"].append({"step": "search_chunks", "http_status": r2.status_code, "response": text2})
            log.info("cognee search(CHUNKS) -> %s %s", r2.status_code, text2[:300])

            result["memory_snippets"] = snippets
            result["memory_chunks"] = chunks
            result["ok"] = r.status_code < 300
    except Exception as e:
        log.exception("cognee failure")
        result["error"] = f"{type(e).__name__}: {e}"[:500]
    result["elapsed_s"] = round(time.time() - t0, 1)
    return result
