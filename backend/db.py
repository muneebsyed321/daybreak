"""Authoritative SQLite state for DAYBREAK. FastAPI is the sole writer."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, UTC
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "daybreak.db"
ARTIFACTS_DIR = BASE_DIR / "artifacts"
ARTIFACTS_DIR.mkdir(exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    filename TEXT,
    meta TEXT NOT NULL DEFAULT '{}',
    cognee_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    readiness TEXT,
    extraction TEXT NOT NULL DEFAULT '{}',
    cognee TEXT NOT NULL DEFAULT '{}',
    graph TEXT NOT NULL DEFAULT '{}',
    error TEXT
);
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    key TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL,
    source_ids TEXT NOT NULL DEFAULT '[]',
    evidence TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS repairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    finding_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    artifact_url TEXT,
    retest_screenshot TEXT,
    evidence TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

JSON_COLS = {"meta", "extraction", "cognee", "graph", "source_ids", "evidence"}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as c:
        c.executescript(SCHEMA)


def _row(r):
    if r is None:
        return None
    d = dict(r)
    for k in JSON_COLS & d.keys():
        try:
            d[k] = json.loads(d[k]) if d[k] else None
        except (TypeError, ValueError):
            pass
    return d


def query(sql: str, params=()) -> list[dict]:
    with connect() as c:
        return [_row(r) for r in c.execute(sql, params).fetchall()]


def one(sql: str, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params=()) -> int:
    with connect() as c:
        cur = c.execute(sql, params)
        return cur.lastrowid


# ---- sources ---------------------------------------------------------------
def upsert_source(id: str, type: str, title: str, content: str, filename=None, meta=None) -> dict:
    ts = now()
    with connect() as c:
        c.execute(
            """INSERT INTO sources (id, type, title, content, filename, meta, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET type=excluded.type, title=excluded.title,
                 content=excluded.content, filename=excluded.filename, meta=excluded.meta,
                 updated_at=excluded.updated_at""",
            (id, type, title, content, filename, json.dumps(meta or {}), ts, ts),
        )
    return get_source(id)


def get_source(id: str):
    return one("SELECT * FROM sources WHERE id=?", (id,))


def list_sources() -> list[dict]:
    return query("SELECT * FROM sources ORDER BY created_at, id")


def delete_source(id: str) -> None:
    execute("DELETE FROM sources WHERE id=?", (id,))


def mark_cognee_synced(id: str, h: str) -> None:
    execute("UPDATE sources SET cognee_hash=? WHERE id=?", (h, id))


# ---- runs / findings / repairs --------------------------------------------
def create_run(trigger: str) -> int:
    return execute("INSERT INTO runs (trigger, status, started_at) VALUES (?,?,?)", (trigger, "running", now()))


def finish_run(run_id: int, **fields) -> None:
    fields.setdefault("finished_at", now())
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = [json.dumps(v) if k in JSON_COLS else v for k, v in fields.items()]
    execute(f"UPDATE runs SET {cols} WHERE id=?", (*vals, run_id))


def add_finding(run_id: int, key: str, title: str, status: str, summary: str, source_ids: list, evidence: dict) -> int:
    return execute(
        "INSERT INTO findings (run_id, key, title, status, summary, source_ids, evidence, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, key, title, status, summary, json.dumps(source_ids), json.dumps(evidence), now()),
    )


def add_repair(run_id: int, finding_key: str, kind: str, title: str, status: str, body: str = "",
               artifact_url=None, retest_screenshot=None, evidence=None) -> int:
    ts = now()
    return execute(
        """INSERT INTO repairs (run_id, finding_key, kind, title, status, body, artifact_url, retest_screenshot,
           evidence, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, finding_key, kind, title, status, body, artifact_url, retest_screenshot,
         json.dumps(evidence or {}), ts, ts),
    )


def latest_run(finished_only: bool = True):
    where = "WHERE status != 'running'" if finished_only else ""
    return one(f"SELECT * FROM runs {where} ORDER BY id DESC LIMIT 1")


def findings_for(run_id: int) -> list[dict]:
    return query("SELECT * FROM findings WHERE run_id=? ORDER BY id", (run_id,))


def repairs_for(run_id: int) -> list[dict]:
    return query("SELECT * FROM repairs WHERE run_id=? ORDER BY id", (run_id,))


def previous_finding(key: str, before_run_id: int):
    return one(
        """SELECT f.* FROM findings f JOIN runs r ON r.id=f.run_id
           WHERE f.key=? AND f.run_id<? AND r.status='completed' ORDER BY f.run_id DESC LIMIT 1""",
        (key, before_run_id),
    )
