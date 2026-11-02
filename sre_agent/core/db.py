"""Tiny SQLite store for agent runs / approval state. Survives restarts, zero setup."""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import settings

# Ordered schema migrations; index + 1 is the schema version stored in PRAGMA user_version.
# Append only: never edit a released step, so package upgrades migrate any older DB in place.
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS items (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        data TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
)
SCHEMA_VERSION = len(MIGRATIONS)


def migrate(c: sqlite3.Connection) -> int:
    """Apply pending migrations; returns the schema version. Refuses a DB written by a newer release."""
    current = c.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise RuntimeError(f"database schema v{current} is newer than this release (v{SCHEMA_VERSION}); "
                           "upgrade sre-agent or restore the pre-upgrade backup")
    for version in range(current, SCHEMA_VERSION):
        c.executescript(MIGRATIONS[version])
        c.execute(f"PRAGMA user_version = {version + 1}")
    return SCHEMA_VERSION


@contextmanager
def _conn():
    c = sqlite3.connect(settings.db_path)
    c.row_factory = sqlite3.Row
    migrate(c)  # one PRAGMA read when already current
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(r) -> dict:
    return {**dict(r), "data": json.loads(r["data"])}


def create_item(kind: str, data: dict, status: str = "new") -> str:
    """Insert a JSON-serialisable record and return its 10-char id. `kind` groups items per agent."""
    item_id = uuid.uuid4().hex[:10]
    with _conn() as c:
        c.execute("INSERT INTO items VALUES (?,?,?,?,?,?)",
                  (item_id, kind, status, json.dumps(data), _now(), _now()))
    return item_id


def get_item(item_id: str) -> dict | None:
    """Return {id, kind, status, data, created_at, updated_at} or None if unknown."""
    with _conn() as c:
        r = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    return _row(r) if r else None


def update_item(item_id: str, status: str | None = None, **data_updates) -> None:
    """Merge data_updates into the stored data and optionally change status."""
    item = get_item(item_id)
    if item is None:
        raise KeyError(item_id)
    merged = {**item["data"], **data_updates}
    with _conn() as c:
        c.execute("UPDATE items SET status=?, data=?, updated_at=? WHERE id=?",
                  (status or item["status"], json.dumps(merged), _now(), item_id))


def list_items(kind: str | None = None, status: str | None = None) -> list[dict]:
    """Items newest-first, optionally filtered by kind and/or status."""
    q, args = "SELECT * FROM items WHERE 1=1", []
    if kind:
        q, args = q + " AND kind=?", [*args, kind]
    if status:
        q, args = q + " AND status=?", [*args, status]
    with _conn() as c:
        return [_row(r) for r in c.execute(q + " ORDER BY created_at DESC", args)]
