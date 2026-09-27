"""SQLite storage."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .sources import Job

DB_PATH = os.getenv("DB_PATH", "jobs.db")
STATUSES = ["new", "shortlisted", "applied", "interview", "rejected", "dismissed"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    uid          TEXT UNIQUE NOT NULL,
    source       TEXT, company TEXT, title TEXT, location TEXT, url TEXT,
    description  TEXT, posted_at TEXT, found_at TEXT,
    score        INTEGER DEFAULT 0,
    status       TEXT DEFAULT 'new',
    status_at    TEXT,
    cover_letter TEXT,
    notes        TEXT
);
CREATE INDEX IF NOT EXISTS ix_status ON jobs(status);
"""


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def insert(job: Job, score: int) -> bool:
    """Insert if unseen. Returns True if it's a new job."""
    with conn() as c:
        cur = c.execute(
            """INSERT OR IGNORE INTO jobs
               (uid, source, company, title, location, url, description, posted_at, found_at, score)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (job.uid, job.source, job.company, job.title, job.location, job.url,
             job.description, job.posted_at, now(), score),
        )
        return cur.rowcount == 1


def list_jobs(status: str | None = None, q: str | None = None, limit: int = 300):
    sql, args = "SELECT * FROM jobs WHERE 1=1", []
    if status:
        sql += " AND status = ?"
        args.append(status)
    if q:
        sql += " AND (title LIKE ? OR company LIKE ? OR location LIKE ?)"
        args += [f"%{q}%"] * 3
    sql += " ORDER BY found_at DESC, score DESC LIMIT ?"
    args.append(limit)
    with conn() as c:
        return c.execute(sql, args).fetchall()


def get(job_id: int):
    with conn() as c:
        return c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def set_status(job_id: int, status: str):
    if status not in STATUSES:
        raise ValueError(status)
    with conn() as c:
        c.execute("UPDATE jobs SET status=?, status_at=? WHERE id=?", (status, now(), job_id))


def set_field(job_id: int, field: str, value: str):
    if field not in ("cover_letter", "notes"):
        raise ValueError(field)
    with conn() as c:
        c.execute(f"UPDATE jobs SET {field}=? WHERE id=?", (value, job_id))


def counts() -> dict[str, int]:
    with conn() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}
