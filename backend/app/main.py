"""FastAPI app. Whole thing served under a single unguessable path prefix.
No auth beyond the path. Single user."""
import os
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db, graph, stats
from .config import settings
from .schemas import CreateTripIn, PatchTripIn, TapsBatchIn

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Path Race", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)


# ---- helpers ---------------------------------------------------------------

def _trip_dict(trip) -> dict:
    return {
        "id": trip["id"],
        "direction": trip["direction"],
        "started_at": trip["started_at"],
        "completed_at": trip["completed_at"],
        "status": trip["status"],
        "anomalous": bool(trip["anomalous"]),
        "anomaly_reason": trip["anomaly_reason"],
        "crowding": trip["crowding"],
    }


def _state_for(trip) -> dict:
    """Active trip + its taps + the options for the current node (for reconcile
    and cold-reload restore)."""
    if trip is None:
        return {
            "trip": None,
            "taps": [],
            "current_key": None,
            "options": graph.options_for(None, None),
        }
    taps = [dict(t) for t in db.get_taps(trip["id"])]
    current_key = taps[-1]["checkpoint_key"] if taps else None
    direction = trip["direction"]
    terminal = graph.terminal(direction)
    is_terminal = current_key == terminal
    return {
        "trip": _trip_dict(trip),
        "taps": [
            {
                "id": t["id"], "checkpoint_key": t["checkpoint_key"],
                "client_ts": t["client_ts"], "seq": t["seq"],
                "ts_trusted": bool(t["ts_trusted"]),
                "lat": t["lat"], "lng": t["lng"], "accuracy": t["accuracy"],
            }
            for t in taps
        ],
        "current_key": current_key,
        "is_terminal": is_terminal,
        "options": [] if is_terminal else graph.options_for(direction, current_key),
    }


def _autocomplete_if_terminal(trip_id: str, direction: str):
    taps = db.get_taps(trip_id)
    if not taps:
        return
    last = taps[-1]
    if last["checkpoint_key"] == graph.terminal(direction):
        trip = db.get_trip(trip_id)
        if trip and trip["status"] == "active":
            db.patch_trip(trip_id, status="done", completed_at=last["client_ts"])


# ---- API -------------------------------------------------------------------

api = APIRouter(prefix=f"{settings.prefix}/api")


@api.get("/config")
def get_config():
    return {"config": settings.client_config(), "graph": graph.as_config()}


@api.get("/state")
def get_state():
    return _state_for(db.active_trip())


@api.post("/trips")
def create_trip(body: CreateTripIn):
    existing = db.get_trip(body.id)
    if existing:  # idempotent retry
        return _state_for(existing)

    active = db.active_trip()
    if active and active["status"] == "active":
        # one active trip at a time — client must discard or complete the old one
        raise HTTPException(
            status_code=409,
            detail={"reason": "active_trip_exists", "active": _state_for(active)},
        )

    direction = graph.direction_of_start(body.first_tap.checkpoint_key)
    if direction is None:
        raise HTTPException(400, f"'{body.first_tap.checkpoint_key}' is not a valid start")

    db.create_trip(body.id, direction, body.first_tap.client_ts)
    db.upsert_taps(body.id, [body.first_tap.model_dump()],
                   settings.DOUBLE_TAP_THRESHOLD_S * 1000)
    _autocomplete_if_terminal(body.id, direction)
    return _state_for(db.get_trip(body.id))


@api.post("/trips/{trip_id}/taps")
def add_taps(trip_id: str, body: TapsBatchIn):
    trip = db.get_trip(trip_id)
    if not trip:
        raise HTTPException(404, "unknown trip")
    db.upsert_taps(trip_id, [t.model_dump() for t in body.taps],
                   settings.DOUBLE_TAP_THRESHOLD_S * 1000)
    _autocomplete_if_terminal(trip_id, trip["direction"])
    return _state_for(db.get_trip(trip_id))


@api.post("/trips/{trip_id}/undo")
def undo(trip_id: str):
    trip = db.get_trip(trip_id)
    if not trip:
        raise HTTPException(404, "unknown trip")
    removed = db.delete_last_tap(trip_id)
    # undoing the terminal tap re-opens the trip
    if trip["status"] == "done":
        with db.connect() as c:
            c.execute("UPDATE trips SET status='active', completed_at=NULL WHERE id=?", (trip_id,))
    state = _state_for(db.get_trip(trip_id))
    state["removed_tap_id"] = removed
    return state


@api.patch("/trips/{trip_id}")
def patch_trip(trip_id: str, body: PatchTripIn):
    trip = db.get_trip(trip_id)
    if not trip:
        raise HTTPException(404, "unknown trip")
    fields = body.model_dump(exclude_none=True)
    if "anomalous" in fields:
        fields["anomalous"] = 1 if fields["anomalous"] else 0
    db.patch_trip(trip_id, **fields)
    return _state_for(db.get_trip(trip_id))


@api.get("/stats")
def get_stats(include_anomalous: bool = Query(False)):
    return stats.compute(include_anomalous)


@api.get("/trips")
def list_trips(limit: int = Query(200, ge=1, le=1000)):
    return {"trips": stats.trip_log(limit)}


app.include_router(api)


# ---- static / pages --------------------------------------------------------
# PFX is "" when PATH_PREFIX is unset (app served at the domain root) or
# "/race-xxxx" when a secret prefix is configured.
PFX = settings.prefix


@app.get(f"{PFX}/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get(f"{PFX}/stats")
def stats_page():
    return FileResponse(os.path.join(STATIC_DIR, "stats.html"))


@app.get(f"{PFX}/manifest.webmanifest")
def manifest():
    return FileResponse(os.path.join(STATIC_DIR, "manifest.webmanifest"),
                        media_type="application/manifest+json")


@app.get(f"{PFX}/sw.js")
def service_worker():
    # served at the app scope so it can control the whole prefix
    return FileResponse(os.path.join(STATIC_DIR, "sw.js"),
                        media_type="application/javascript")


app.mount(f"{PFX}/static", StaticFiles(directory=STATIC_DIR), name="static")

if PFX:
    # secret-prefix mode: no-slash prefix also serves the app; bare root hides everything
    @app.get(PFX)
    def index_noslash():
        return index()

    @app.get("/")
    def root_redirect():
        return JSONResponse({"ok": True}, status_code=200)
