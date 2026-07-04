"""SQLite access — schema, connection, and the low-level trip/tap operations.

Client timestamps are authoritative (offline queue makes server receipt time
meaningless). Tap upload is idempotent by client-generated tap id.
"""
import os
import sqlite3
import time
from contextlib import contextmanager

from .config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trips (
    id            TEXT PRIMARY KEY,
    direction     TEXT NOT NULL CHECK (direction IN ('morning','evening')),
    started_at    INTEGER NOT NULL,           -- client ms epoch of first tap
    completed_at  INTEGER,                    -- client ms epoch of terminal tap
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','done','discarded')),
    anomalous     INTEGER NOT NULL DEFAULT 0,
    anomaly_reason TEXT,
    crowding      INTEGER CHECK (crowding IN (1,2,3))
);

CREATE TABLE IF NOT EXISTS taps (
    id             TEXT PRIMARY KEY,          -- client-generated (idempotency key)
    trip_id        TEXT NOT NULL REFERENCES trips(id),
    checkpoint_key TEXT NOT NULL,
    client_ts      INTEGER NOT NULL,          -- device Date.now() at commit
    seq            INTEGER NOT NULL,          -- order within trip
    ts_trusted     INTEGER NOT NULL DEFAULT 1,
    lat            REAL,
    lng            REAL,
    accuracy       REAL,
    received_at    INTEGER NOT NULL           -- server wall clock, debug only
);

CREATE INDEX IF NOT EXISTS idx_taps_trip ON taps(trip_id, seq);
CREATE INDEX IF NOT EXISTS idx_trips_status ON trips(status);
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def init_db() -> None:
    d = os.path.dirname(settings.DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    with connect() as c:
        c.executescript(_SCHEMA)


@contextmanager
def connect():
    conn = sqlite3.connect(settings.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---- trips -----------------------------------------------------------------

def create_trip(trip_id: str, direction: str, started_at: int) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO trips (id, direction, started_at, status) VALUES (?,?,?,'active')",
            (trip_id, direction, started_at),
        )


def get_trip(trip_id: str) -> sqlite3.Row | None:
    with connect() as c:
        return c.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()


def active_trip() -> sqlite3.Row | None:
    with connect() as c:
        return c.execute(
            "SELECT * FROM trips WHERE status='active' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()


def patch_trip(trip_id: str, **fields) -> sqlite3.Row | None:
    allowed = {"status", "anomalous", "anomaly_reason", "crowding", "completed_at"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k}=?")
            vals.append(int(v) if k in ("anomalous", "crowding", "completed_at") else v)
    if not sets:
        return get_trip(trip_id)
    vals.append(trip_id)
    with connect() as c:
        c.execute(f"UPDATE trips SET {','.join(sets)} WHERE id=?", vals)
    return get_trip(trip_id)


# ---- taps ------------------------------------------------------------------

def get_taps(trip_id: str) -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute(
            "SELECT * FROM taps WHERE trip_id=? ORDER BY seq", (trip_id,)
        ).fetchall()


def upsert_taps(trip_id: str, taps: list[dict], threshold_ms: int) -> None:
    """Idempotent batch insert (retries must not duplicate). After insert,
    re-derive ts_trusted across the whole trip: any tap committed < threshold
    after its predecessor marks the EARLIER tap untrusted (rapid-double-tap =
    'I forgot to tap on time')."""
    with connect() as c:
        for t in taps:
            c.execute(
                """INSERT OR IGNORE INTO taps
                   (id, trip_id, checkpoint_key, client_ts, seq, ts_trusted,
                    lat, lng, accuracy, received_at)
                   VALUES (?,?,?,?,?,1,?,?,?,?)""",
                (t["id"], trip_id, t["checkpoint_key"], t["client_ts"], t["seq"],
                 t.get("lat"), t.get("lng"), t.get("accuracy"), _now_ms()),
            )
        _recompute_trust(c, trip_id, threshold_ms)


def delete_tap(trip_id: str, tap_id: str) -> None:
    with connect() as c:
        c.execute("DELETE FROM taps WHERE trip_id=? AND id=?", (trip_id, tap_id))
        _recompute_trust(c, trip_id, settings.DOUBLE_TAP_THRESHOLD_S * 1000)


def delete_last_tap(trip_id: str) -> str | None:
    with connect() as c:
        row = c.execute(
            "SELECT id FROM taps WHERE trip_id=? ORDER BY seq DESC LIMIT 1", (trip_id,)
        ).fetchone()
        if not row:
            return None
        c.execute("DELETE FROM taps WHERE id=?", (row["id"],))
        _recompute_trust(c, trip_id, settings.DOUBLE_TAP_THRESHOLD_S * 1000)
        return row["id"]


def _recompute_trust(conn, trip_id: str, threshold_ms: int) -> None:
    rows = conn.execute(
        "SELECT id, client_ts FROM taps WHERE trip_id=? ORDER BY seq", (trip_id,)
    ).fetchall()
    trusted = {r["id"]: 1 for r in rows}
    for prev, cur in zip(rows, rows[1:]):
        if cur["client_ts"] - prev["client_ts"] < threshold_ms:
            # earlier tap keeps its place in the path but loses timestamp trust
            trusted[prev["id"]] = 0
    for tid, val in trusted.items():
        conn.execute("UPDATE taps SET ts_trusted=? WHERE id=?", (val, tid))
