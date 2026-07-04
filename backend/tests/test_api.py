import os
import tempfile
import uuid

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["PATH_PREFIX"] = "race-test"
os.environ["DOUBLE_TAP_THRESHOLD_S"] = "7"

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import db

PREFIX = "/race-test"
API = lambda p: f"{PREFIX}/api{p}"


@pytest.fixture(autouse=True)
def fresh_db():
    # wipe tables between tests
    db.init_db()
    with db.connect() as c:
        c.execute("DELETE FROM taps")
        c.execute("DELETE FROM trips")
    yield


client = TestClient(app)


def _tap(key, ts, seq, **kw):
    return {"id": str(uuid.uuid4()), "checkpoint_key": key, "client_ts": ts,
            "seq": seq, **kw}


def start_morning(t0=1_000_000):
    trip_id = str(uuid.uuid4())
    first = _tap("home", t0, 0)
    r = client.post(API("/trips"), json={"id": trip_id, "first_tap": first})
    assert r.status_code == 200, r.text
    return trip_id, first, r.json()


def test_config_and_graph():
    r = client.get(API("/config"))
    assert r.status_code == 200
    body = r.json()
    assert "morning" in body["graph"] and "evening" in body["graph"]
    assert body["config"]["prefix"] == PREFIX


def test_create_infers_direction():
    _, _, state = start_morning()
    assert state["trip"]["direction"] == "morning"
    assert state["current_key"] == "home"
    # options are home's out-edges
    keys = {o["key"] for o in state["options"]}
    assert "pinsker_doors_close" in keys and "kiryat_arye_entrance" in keys


def test_one_active_trip_conflict():
    start_morning()
    other = str(uuid.uuid4())
    r = client.post(API("/trips"),
                    json={"id": other, "first_tap": _tap("office", 2_000_000, 0)})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "active_trip_exists"


def test_idempotent_taps():
    trip_id, first, _ = start_morning()
    t = _tap("pinsker_doors_close", 1_060_000, 1)
    body = {"taps": [t]}
    client.post(API(f"/trips/{trip_id}/taps"), json=body)
    r = client.post(API(f"/trips/{trip_id}/taps"), json=body)  # resend
    taps = r.json()["taps"]
    assert len(taps) == 2  # home + pinsker, no duplicate


def test_undo_removes_last():
    trip_id, first, _ = start_morning()
    client.post(API(f"/trips/{trip_id}/taps"),
                json={"taps": [_tap("pinsker_doors_close", 1_060_000, 1)]})
    r = client.post(API(f"/trips/{trip_id}/undo"))
    assert r.json()["current_key"] == "home"
    assert r.json()["removed_tap_id"] is not None


def test_terminal_autocompletes():
    trip_id, first, _ = start_morning()
    seq = 1
    ts = 1_000_000
    for key in ["pinsker_doors_close", "yehudit_doors_open", "yehudit_gate",
                "street_morning", "office"]:
        ts += 60_000
        client.post(API(f"/trips/{trip_id}/taps"),
                    json={"taps": [_tap(key, ts, seq)]})
        seq += 1
    r = client.get(API("/state"))
    # active trip is gone (completed)
    assert r.json()["trip"] is None
    trip = db.get_trip(trip_id)
    assert trip["status"] == "done"
    assert trip["completed_at"] == ts


def test_double_tap_invalidation():
    trip_id, first, _ = start_morning()
    # two taps 3s apart (< 7s threshold) -> earlier one untrusted
    client.post(API(f"/trips/{trip_id}/taps"), json={"taps": [
        _tap("pinsker_doors_close", 1_060_000, 1),
        _tap("yehudit_doors_open", 1_063_000, 2),
    ]})
    taps = {t["checkpoint_key"]: t for t in client.get(API("/state")).json()["taps"]}
    assert taps["pinsker_doors_close"]["ts_trusted"] is False
    assert taps["yehudit_doors_open"]["ts_trusted"] is True
    # home->pinsker was 60s so home stays trusted
    assert taps["home"]["ts_trusted"] is True


def test_stats_bracket_and_inference():
    # full clean morning trip via Pinsker to Yehudit(office)
    trip_id, first, _ = start_morning(1_000_000)
    plan = [("pinsker_doors_close", 1_060_000), ("yehudit_doors_open", 1_600_000),
            ("yehudit_gate", 1_660_000), ("street_morning", 1_720_000),
            ("office", 1_900_000)]
    seq = 1
    for key, ts in plan:
        client.post(API(f"/trips/{trip_id}/taps"), json={"taps": [_tap(key, ts, seq)]})
        seq += 1
    client.patch(API(f"/trips/{trip_id}"), json={"crowding": 2})

    s = client.get(API("/stats")).json()
    boarding = s["panels"]["boarding"]["morning"]["options"]
    assert "pinsker" in boarding
    # bracket home(1_000_000) -> yehudit_doors_open(1_600_000) = 600_000ms
    assert boarding["pinsker"]["mean_ms"] == 600_000
    office = s["panels"]["office"]["morning"]["options"]
    # yehudit office bracket yehudit_doors_open->office = 1_900_000-1_600_000=300_000
    assert "yehudit" in office
    assert office["yehudit"]["mean_ms"] == 300_000


def test_stats_excludes_untrusted_bracket():
    trip_id, first, _ = start_morning(1_000_000)
    # make yehudit_doors_open untrusted by tapping office-side hinge <7s later
    plan = [("pinsker_doors_close", 1_060_000),
            ("yehudit_doors_open", 1_600_000),
            ("yehudit_gate", 1_603_000)]  # 3s -> yehudit_doors_open untrusted
    seq = 1
    for key, ts in plan:
        client.post(API(f"/trips/{trip_id}/taps"), json={"taps": [_tap(key, ts, seq)]})
        seq += 1
    for key, ts in [("street_morning", 1_720_000), ("office", 1_900_000)]:
        client.post(API(f"/trips/{trip_id}/taps"), json={"taps": [_tap(key, ts, seq)]}); seq += 1
    s = client.get(API("/stats")).json()
    boarding = s["panels"]["boarding"]["morning"]["options"]
    # yehudit_doors_open endpoint untrusted -> boarding bracket invalid -> count 0
    assert boarding.get("pinsker", {}).get("count", 0) == 0


def test_trip_log_and_discard_toggle():
    trip_id, first, _ = start_morning()
    client.patch(API(f"/trips/{trip_id}"), json={"status": "discarded"})
    log = client.get(API("/trips")).json()["trips"]
    row = next(t for t in log if t["id"] == trip_id)
    assert row["status"] == "discarded"
