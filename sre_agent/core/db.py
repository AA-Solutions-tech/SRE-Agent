"""Tiny SQLite store for agent runs / approval state. Survives restarts, zero setup."""
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

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
    """
    CREATE INDEX IF NOT EXISTS idx_items_kind_status_created ON items(kind, status, created_at)
    """,
)
SCHEMA_VERSION = len(MIGRATIONS)

# Lookups by a field inside the JSON (an issue by fingerprint, a job by name ...) used to scan the whole table.
# These expression indexes need SQLite's JSON functions; on a build without them the lookup falls back to a scan.
_JSON_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_issue_fingerprint ON items(json_extract(data, '$.fingerprint')) WHERE kind = 'issue'",
    "CREATE INDEX IF NOT EXISTS idx_job_name ON items(json_extract(data, '$.name')) WHERE kind = 'job_state'",
    "CREATE INDEX IF NOT EXISTS idx_health_key ON items(json_extract(data, '$.key')) WHERE kind = 'health_state'",
)
_json_indexed: set[str] = set()
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
    if settings.db_path not in _json_indexed:
        try:
            for sql in _JSON_INDEXES:
                c.execute(sql)
        except sqlite3.OperationalError:
            pass  # no JSON functions in this SQLite build: find_by_field scans instead
        _json_indexed.add(settings.db_path)
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


def list_items(kind: str | None = None, status: str | None = None, limit: int | None = None,
               since: datetime | None = None) -> list[dict]:
    """Items newest-first, optionally filtered by kind and/or status, created at or after `since`, and capped
    at `limit`. Hot paths pass `since`/`limit`: the table is only ever pruned, never small."""
    q, args = "SELECT * FROM items WHERE 1=1", []
    if kind:
        q, args = q + " AND kind=?", [*args, kind]
    if status:
        q, args = q + " AND status=?", [*args, status]
    if since is not None:
        q, args = q + " AND created_at>=?", [*args, since.astimezone(timezone.utc).isoformat()]
    q += " ORDER BY created_at DESC"
    if limit is not None:
        q, args = q + " LIMIT ?", [*args, int(limit)]
    with _conn() as c:
        return [_row(r) for r in c.execute(q, args)]


def count_items(kind: str | None = None, status: str | None = None, since: datetime | None = None) -> int:
    q, args = "SELECT COUNT(*) FROM items WHERE 1=1", []
    if kind:
        q, args = q + " AND kind=?", [*args, kind]
    if status:
        q, args = q + " AND status=?", [*args, status]
    if since is not None:
        q, args = q + " AND created_at>=?", [*args, since.astimezone(timezone.utc).isoformat()]
    with _conn() as c:
        return int(c.execute(q, args).fetchone()[0])


def find_by_field(kind: str, field: str, value) -> dict | None:
    """The newest item of `kind` whose JSON field `field` equals `value`, via an index (a scan as the fallback)."""
    if not (_IDENT.match(kind) and _IDENT.match(field)):
        raise ValueError("kind and field must be plain identifiers")
    sql = (f"SELECT * FROM items WHERE kind = '{kind}' AND json_extract(data, '$.{field}') = ? "
           "ORDER BY created_at DESC LIMIT 1")
    try:
        with _conn() as c:
            r = c.execute(sql, (value,)).fetchone()
        return _row(r) if r else None
    except sqlite3.OperationalError:
        return next((i for i in list_items(kind=kind) if i["data"].get(field) == value), None)


def prune(kind: str, older_than: timedelta, statuses: tuple[str, ...] | None = None) -> int:
    """Delete items of `kind` created more than `older_than` ago (only those with one of `statuses`, if given).
    Returns how many were removed."""
    cutoff = (datetime.now(timezone.utc) - older_than).isoformat()
    q, args = "DELETE FROM items WHERE kind=? AND created_at<?", [kind, cutoff]
    if statuses:
        q += f" AND status IN ({','.join('?' * len(statuses))})"
        args += list(statuses)
    with _conn() as c:
        return c.execute(q, args).rowcount


def delete_item(item_id: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM items WHERE id=?", (item_id,))
