"""Raw data export — the whole DB as an analysis-friendly snapshot.

Unlike stats.py this filters *nothing*: discarded and anomalous trips,
untrusted taps, and the debug fields (received_at, lat/lng/accuracy) are all
included, so an export is a faithful copy you can re-derive every stat from.
Local (Asia/Jerusalem) ISO strings sit alongside the raw epoch-ms values —
the raw ms stay authoritative, the ISO strings are just there to read by eye.
"""
import csv
import io
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import db

TZ = ZoneInfo("Asia/Jerusalem")

# Stable column order for the flat CSV: one row per tap, with the parent trip's
# fields denormalized onto every row so a single file is self-contained.
CSV_COLUMNS = [
    "trip_id", "direction", "trip_status", "anomalous", "anomaly_reason",
    "crowding", "trip_started_at", "trip_completed_at",
    "tap_id", "seq", "checkpoint_key", "client_ts", "client_ts_local",
    "ts_trusted", "lat", "lng", "accuracy", "received_at",
]


def _iso_local(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=TZ).isoformat()


def _all_rows() -> tuple[list[dict], list[dict]]:
    """(trips, taps) as raw dict lists — trips oldest-first, taps in seq order."""
    with db.connect() as c:
        trips = [dict(r) for r in
                 c.execute("SELECT * FROM trips ORDER BY started_at ASC").fetchall()]
        taps = [dict(r) for r in
                c.execute("SELECT * FROM taps ORDER BY trip_id, seq").fetchall()]
    return trips, taps


def bundle() -> dict:
    """Nested JSON snapshot: every trip with its taps. Nothing filtered."""
    trips, taps = _all_rows()
    by_trip: dict[str, list] = {}
    for t in taps:
        t["ts_trusted"] = bool(t["ts_trusted"])
        t["client_ts_local"] = _iso_local(t["client_ts"])
        by_trip.setdefault(t["trip_id"], []).append(t)
    for tr in trips:
        tr["anomalous"] = bool(tr["anomalous"])
        tr["started_at_local"] = _iso_local(tr["started_at"])
        tr["completed_at_local"] = _iso_local(tr["completed_at"])
        tr["taps"] = by_trip.get(tr["id"], [])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tz": "Asia/Jerusalem",
        "trip_count": len(trips),
        "tap_count": len(taps),
        "trips": trips,
    }


def csv_text() -> str:
    """Flat CSV, one row per tap with the parent trip's fields on each row."""
    trips, taps = _all_rows()
    trip_by_id = {t["id"]: t for t in trips}
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for tp in taps:
        tr = trip_by_id.get(tp["trip_id"], {})
        w.writerow({
            "trip_id": tp["trip_id"],
            "direction": tr.get("direction"),
            "trip_status": tr.get("status"),
            "anomalous": tr.get("anomalous"),
            "anomaly_reason": tr.get("anomaly_reason"),
            "crowding": tr.get("crowding"),
            "trip_started_at": tr.get("started_at"),
            "trip_completed_at": tr.get("completed_at"),
            "tap_id": tp["id"],
            "seq": tp["seq"],
            "checkpoint_key": tp["checkpoint_key"],
            "client_ts": tp["client_ts"],
            "client_ts_local": _iso_local(tp["client_ts"]),
            "ts_trusted": tp["ts_trusted"],
            "lat": tp["lat"],
            "lng": tp["lng"],
            "accuracy": tp["accuracy"],
            "received_at": tp["received_at"],
        })
    return buf.getvalue()


def filename(ext: str) -> str:
    stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    return f"pathrace-export-{stamp}.{ext}"
