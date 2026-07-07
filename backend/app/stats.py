"""Derived stats. Nothing here is stored — boarding_option and office_station
are inferred from which checkpoint keys appear in a trip's path, and bracket
totals are computed from trusted client timestamps.

Hinge principle: a bracket total counts only when both of its endpoint taps are
present AND trusted. Endpoints are the taps shared across the compared options
(the Yehudit hinge), so the totals are comparable. Intermediate segments are
diagnosis, never the verdict, and any segment touching an untrusted timestamp
is dropped.
"""
import statistics
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import db, graph
from .config import settings

TZ = ZoneInfo("Asia/Jerusalem")

# (start_key, end_key) for each experiment × direction bracket.
BRACKETS = {
    ("boarding", "morning"): ("home", "yehudit_doors_open"),
    ("boarding", "evening"): ("yehudit_doors_close", "home"),
    ("office",   "morning"): ("yehudit_doors_open", "office"),
    ("office",   "evening"): ("office", "yehudit_doors_close"),
}


def _boarding_option(direction: str, path: list[str]) -> str | None:
    """Home-side station verdict. Morning = where you boarded (first boarding
    node); evening = where you alighted (last boarding node)."""
    nodes = graph.nodes(direction)
    boarding_keys = [k for k in path if nodes.get(k, {}).get("boarding")]
    if not boarding_keys:
        return None
    key = boarding_keys[0] if direction == "morning" else boarding_keys[-1]
    return _station_of(direction, key)


def _office_option(direction: str, path: list[str]) -> str | None:
    nodes = graph.nodes(direction)
    for k in path:
        office = nodes.get(k, {}).get("office")
        if office:
            return office
    return None


def _station_of(direction: str, key: str) -> str:
    # station name is the checkpoint key minus its event suffix
    for suffix in ("_doors_close", "_doors_open", "_entrance", "_platform",
                   "_exit", "_gate_in", "_gate", "_doors"):
        if key.endswith(suffix):
            return key[: -len(suffix)]
    return key


def _boundary_ms_of_day(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return (int(h) * 60 + int(m)) * 60_000


def _local_minute_of_day(client_ts_ms: int) -> int:
    dt = datetime.fromtimestamp(client_ts_ms / 1000, tz=TZ)
    return (dt.hour * 60 + dt.minute) * 60_000


def _agg(values: list[int], crowdings: list[int]) -> dict:
    crowd = [c for c in crowdings if c]
    return {
        "count": len(values),
        "mean_ms": round(statistics.mean(values)) if values else None,
        "median_ms": round(statistics.median(values)) if values else None,
        "avg_crowding": round(statistics.mean(crowd), 2) if crowd else None,
    }


def _load_trips(include_anomalous: bool) -> list[dict]:
    with db.connect() as c:
        q = "SELECT * FROM trips WHERE status='done'"
        if not include_anomalous:
            q += " AND anomalous=0"
        trips = c.execute(q).fetchall()
        out = []
        for t in trips:
            taps = c.execute(
                "SELECT * FROM taps WHERE trip_id=? ORDER BY seq", (t["id"],)
            ).fetchall()
            out.append({"trip": t, "taps": [dict(x) for x in taps]})
        return out


def compute(include_anomalous: bool = False) -> dict:
    data = _load_trips(include_anomalous)

    morning_b = _boundary_ms_of_day(settings.TOD_MORNING_BOUNDARY)
    evening_b = _boundary_ms_of_day(settings.TOD_EVENING_BOUNDARY)

    panels: dict = {}
    for (exp, direction), (start_key, end_key) in BRACKETS.items():
        boundary = morning_b if direction == "morning" else evening_b
        # option -> accumulator
        acc: dict[str, dict] = {}
        for row in data:
            trip = row["trip"]
            if trip["direction"] != direction:
                continue
            taps = row["taps"]
            by_key = {tp["checkpoint_key"]: tp for tp in taps}
            path = [tp["checkpoint_key"] for tp in taps]

            option = (_boarding_option if exp == "boarding" else _office_option)(direction, path)
            if not option:
                continue

            a = acc.setdefault(option, {"totals": [], "crowd": [],
                                        "early": [], "early_c": [],
                                        "late": [], "late_c": [],
                                        "segments": {}})

            # bracket total (endpoints must exist & be trusted)
            s, e = by_key.get(start_key), by_key.get(end_key)
            if s and e and s["ts_trusted"] and e["ts_trusted"]:
                total = e["client_ts"] - s["client_ts"]
                if total >= 0:
                    a["totals"].append(total)
                    a["crowd"].append(trip["crowding"])
                    bucket = "early" if _local_minute_of_day(trip["started_at"]) < boundary else "late"
                    a[bucket].append(total)
                    a[bucket + "_c"].append(trip["crowding"])

            # segment diagnosis: consecutive trusted deltas, keyed by checkpoint pair
            for p, cur in zip(taps, taps[1:]):
                if p["ts_trusted"] and cur["ts_trusted"]:
                    seg = (p["checkpoint_key"], cur["checkpoint_key"])
                    a["segments"].setdefault(seg, []).append(cur["client_ts"] - p["client_ts"])

        options_out = {}
        for opt, a in acc.items():
            base = _agg(a["totals"], a["crowd"])
            base["tod"] = {
                "early": _agg(a["early"], a["early_c"]),
                "late": _agg(a["late"], a["late_c"]),
            }
            base["segments"] = [
                {"from": frm, "to": to, "count": len(v), "mean_ms": round(statistics.mean(v))}
                for (frm, to), v in sorted(a["segments"].items())
            ]
            options_out[opt] = base
        panels.setdefault(exp, {})[direction] = {"options": options_out}

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "include_anomalous": include_anomalous,
        "tod_boundaries": {
            "morning": settings.TOD_MORNING_BOUNDARY,
            "evening": settings.TOD_EVENING_BOUNDARY,
        },
        "panels": panels,
    }


def trip_log(limit: int = 200) -> list[dict]:
    with db.connect() as c:
        trips = c.execute(
            "SELECT * FROM trips ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for t in trips:
            taps = c.execute(
                "SELECT checkpoint_key, client_ts, ts_trusted FROM taps WHERE trip_id=? ORDER BY seq",
                (t["id"],),
            ).fetchall()
            path = [tp["checkpoint_key"] for tp in taps]
            direction = t["direction"]
            total = None
            if t["completed_at"] and t["started_at"]:
                total = t["completed_at"] - t["started_at"]
            # ended-elsewhere trip: done but never reached the terminal tap
            partial = (t["status"] == "done"
                       and (not path or path[-1] != graph.terminal(direction)))
            out.append({
                "id": t["id"],
                "direction": direction,
                "date": datetime.fromtimestamp(t["started_at"] / 1000, tz=TZ).isoformat()
                        if t["started_at"] else None,
                "path": path,
                "boarding_option": _boarding_option(direction, path),
                "office_station": _office_option(direction, path),
                "total_ms": total,
                "crowding": t["crowding"],
                "status": t["status"],
                "partial": partial,
                "anomalous": bool(t["anomalous"]),
                "anomaly_reason": t["anomaly_reason"],
                "untrusted_taps": sum(1 for tp in taps if not tp["ts_trusted"]),
            })
        return out
