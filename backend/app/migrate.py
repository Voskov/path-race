"""One-time, idempotent data migrations.

Runs at startup (after schema init). Each migration only touches rows keyed by
the *old* checkpoint keys it retires, so re-running is a no-op.

Migration 1 — retire the ticketing-gate node and rename "street" to the real
station Exit/Entrance:

  - The ticketing gate (yehudit_gate / carlebach_gate / yehudit_gate_in /
    carlebach_gate_in) carried no useful data and was removed from the graph.
  - The old shared "street" node (street_morning / street_evening) is the real
    station exit-to-street (morning) / entrance-from-street (evening). It is
    rekeyed to a per-station key that carries the office-station verdict:
      street_morning -> {yehudit,carlebach}_exit
      street_evening -> {yehudit,carlebach}_entrance

  Station is inferred from the trip's own path (a Carlebach trip always taps a
  Carlebach checkpoint). Gate taps are dropped ONLY when a street tap survives
  to carry the marker; a trip with a gate but no street tap has its gate tap
  *promoted* into the Exit/Entrance so no trip ever loses its only marker.

  Preserved untouched: all doors_open/doors_close/home/office taps (the bracket
  totals), timestamps, trust flags, crowding, and trip status.
"""

_GATE_KEYS = {"yehudit_gate", "carlebach_gate", "yehudit_gate_in", "carlebach_gate_in"}
_STREET_KEYS = {"street_morning", "street_evening"}
# Keys whose presence proves the trip used Carlebach.
_CARLEBACH_MARKERS = {
    "carlebach_doors_open", "carlebach_platform", "carlebach_doors_close",
    "carlebach_gate", "carlebach_gate_in",
}


def _station_of_trip(keys: set[str]) -> str:
    return "carlebach" if keys & _CARLEBACH_MARKERS else "yehudit"


def _target_key(direction: str, station: str) -> str:
    suffix = "exit" if direction == "morning" else "entrance"
    return f"{station}_{suffix}"


def migrate_gate_street(conn) -> int:
    """Idempotent. Returns the number of trips changed."""
    old_keys = _GATE_KEYS | _STREET_KEYS
    placeholders = ",".join("?" * len(old_keys))
    trip_ids = [
        r["trip_id"]
        for r in conn.execute(
            f"SELECT DISTINCT trip_id FROM taps WHERE checkpoint_key IN ({placeholders})",
            tuple(old_keys),
        ).fetchall()
    ]

    changed = 0
    for trip_id in trip_ids:
        trip = conn.execute(
            "SELECT direction FROM trips WHERE id=?", (trip_id,)
        ).fetchone()
        if not trip:
            continue
        direction = trip["direction"]
        rows = conn.execute(
            "SELECT id, checkpoint_key FROM taps WHERE trip_id=? ORDER BY seq",
            (trip_id,),
        ).fetchall()
        keys = {r["checkpoint_key"] for r in rows}
        station = _station_of_trip(keys)
        target = _target_key(direction, station)

        street_taps = [r["id"] for r in rows if r["checkpoint_key"] in _STREET_KEYS]
        gate_taps = [r["id"] for r in rows if r["checkpoint_key"] in _GATE_KEYS]

        if street_taps:
            # street becomes the Exit/Entrance; gate taps are redundant, drop.
            conn.execute(
                "UPDATE taps SET checkpoint_key=? WHERE id=?", (target, street_taps[0])
            )
            for extra in street_taps[1:] + gate_taps:
                conn.execute("DELETE FROM taps WHERE id=?", (extra,))
        elif gate_taps:
            # no street tap — promote the gate tap so the marker/timestamp lives.
            conn.execute(
                "UPDATE taps SET checkpoint_key=? WHERE id=?", (target, gate_taps[0])
            )
            for extra in gate_taps[1:]:
                conn.execute("DELETE FROM taps WHERE id=?", (extra,))
        else:
            continue

        _resequence(conn, trip_id)
        changed += 1
    return changed


def _resequence(conn, trip_id: str) -> None:
    """Close seq gaps left by deleted taps (order preserved)."""
    rows = conn.execute(
        "SELECT id FROM taps WHERE trip_id=? ORDER BY seq", (trip_id,)
    ).fetchall()
    for i, r in enumerate(rows):
        conn.execute("UPDATE taps SET seq=? WHERE id=?", (i, r["id"]))


def run_all() -> None:
    from . import db

    with db.connect() as conn:
        migrate_gate_street(conn)
