import os
import tempfile
import uuid

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["PATH_PREFIX"] = "race-test"
os.environ["DOUBLE_TAP_THRESHOLD_S"] = "3"

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
    # options are home's out-edges: surface boardings go via the platform node
    keys = {o["key"] for o in state["options"]}
    assert "pinsker_platform" in keys and "kiryat_arye_entrance" in keys
    assert "pinsker_doors_close" not in keys  # reachable only via the platform


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
    for key in ["pinsker_platform", "pinsker_doors_close", "yehudit_doors_open",
                "yehudit_gate", "street_morning", "office"]:
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
    # platform -> doors_close 2s apart (< 3s threshold) -> earlier tap untrusted
    client.post(API(f"/trips/{trip_id}/taps"), json={"taps": [
        _tap("pinsker_platform", 1_300_000, 1),
        _tap("pinsker_doors_close", 1_302_000, 2),
    ]})
    taps = {t["checkpoint_key"]: t for t in client.get(API("/state")).json()["taps"]}
    assert taps["pinsker_platform"]["ts_trusted"] is False
    assert taps["pinsker_doors_close"]["ts_trusted"] is True
    # home->platform was 300s so home stays trusted
    assert taps["home"]["ts_trusted"] is True


def test_short_honest_wait_stays_trusted():
    trip_id, first, _ = start_morning()
    # train already at platform: 5s wait is honest and must survive at N=3s
    client.post(API(f"/trips/{trip_id}/taps"), json={"taps": [
        _tap("pinsker_platform", 1_300_000, 1),
        _tap("pinsker_doors_close", 1_305_000, 2),
    ]})
    taps = {t["checkpoint_key"]: t for t in client.get(API("/state")).json()["taps"]}
    assert taps["pinsker_platform"]["ts_trusted"] is True
    assert taps["pinsker_doors_close"]["ts_trusted"] is True


def test_stats_bracket_and_inference():
    # legacy-shaped trip WITHOUT a platform tap (pre-migration data): still a
    # full valid trip, bracket counts, home->doors_close stays an unsplit segment
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
    segs = {(x["from"], x["to"]) for x in boarding["pinsker"]["segments"]}
    assert ("home", "pinsker_doors_close") in segs  # unsplit, not backfilled
    office = s["panels"]["office"]["morning"]["options"]
    # yehudit office bracket yehudit_doors_open->office = 1_900_000-1_600_000=300_000
    assert "yehudit" in office
    assert office["yehudit"]["mean_ms"] == 300_000


def test_stats_scoot_wait_ride_split():
    # platform-era trip: scoot / wait / ride appear as separate segments and the
    # bracket total still spans home -> yehudit_doors_open (wait stays inside)
    trip_id, first, _ = start_morning(1_000_000)
    plan = [("pinsker_platform", 1_300_000),      # scoot 300s
            ("pinsker_doors_close", 1_420_000),   # wait 120s
            ("yehudit_doors_open", 1_900_000),    # ride 480s
            ("yehudit_gate", 1_960_000),
            ("street_morning", 2_020_000),
            ("office", 2_200_000)]
    seq = 1
    for key, ts in plan:
        client.post(API(f"/trips/{trip_id}/taps"), json={"taps": [_tap(key, ts, seq)]})
        seq += 1

    s = client.get(API("/stats")).json()
    pinsker = s["panels"]["boarding"]["morning"]["options"]["pinsker"]
    assert pinsker["mean_ms"] == 900_000  # 1_900_000 - 1_000_000, wait included
    segs = {(x["from"], x["to"]): x["mean_ms"] for x in pinsker["segments"]}
    assert segs[("home", "pinsker_platform")] == 300_000
    assert segs[("pinsker_platform", "pinsker_doors_close")] == 120_000
    assert segs[("pinsker_doors_close", "yehudit_doors_open")] == 480_000


def test_stats_excludes_untrusted_bracket():
    trip_id, first, _ = start_morning(1_000_000)
    # make yehudit_doors_open untrusted by tapping office-side hinge <3s later
    plan = [("pinsker_doors_close", 1_060_000),
            ("yehudit_doors_open", 1_600_000),
            ("yehudit_gate", 1_602_000)]  # 2s -> yehudit_doors_open untrusted
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


def test_end_elsewhere_partial_trip():
    # evening trip that diverges after the hinge (going somewhere else after
    # work): client ends it via PATCH status=done with a truncated path.
    trip_id = str(uuid.uuid4())
    first = _tap("office", 1_000_000, 0)
    r = client.post(API("/trips"), json={"id": trip_id, "first_tap": first})
    assert r.status_code == 200, r.text
    plan = [("street_evening", 1_060_000), ("yehudit_gate_in", 1_240_000),
            ("yehudit_platform", 1_300_000), ("yehudit_doors_close", 1_420_000)]
    seq = 1
    for key, ts in plan:
        client.post(API(f"/trips/{trip_id}/taps"), json={"taps": [_tap(key, ts, seq)]})
        seq += 1
    client.patch(API(f"/trips/{trip_id}"),
                 json={"status": "done", "completed_at": 1_420_000})

    s = client.get(API("/stats")).json()
    # office-station bracket (office -> yehudit_doors_close) is intact
    office = s["panels"]["office"]["evening"]["options"]
    assert office["yehudit"]["count"] == 1
    assert office["yehudit"]["mean_ms"] == 420_000
    # boarding experiment: no boarding tap -> trip contributes nothing
    assert s["panels"]["boarding"]["evening"]["options"] == {}

    # trip is no longer active; log flags it partial
    assert client.get(API("/state")).json()["trip"] is None
    row = next(t for t in client.get(API("/trips")).json()["trips"]
               if t["id"] == trip_id)
    assert row["status"] == "done"
    assert row["partial"] is True
    assert row["total_ms"] == 420_000


def test_full_trip_not_partial():
    trip_id, first, _ = start_morning()
    seq, ts = 1, 1_000_000
    for key in ["pinsker_platform", "pinsker_doors_close", "yehudit_doors_open",
                "yehudit_gate", "street_morning", "office"]:
        ts += 60_000
        client.post(API(f"/trips/{trip_id}/taps"), json={"taps": [_tap(key, ts, seq)]})
        seq += 1
    row = next(t for t in client.get(API("/trips")).json()["trips"]
               if t["id"] == trip_id)
    assert row["partial"] is False


# ---- trip editor (desktop corrections) --------------------------------------

def _done_trip(t0=1_000_000):
    """Complete a full morning trip; returns (trip_id, {key: tap})."""
    trip_id, first, _ = start_morning(t0)
    seq, ts = 1, t0
    for key in ["pinsker_platform", "pinsker_doors_close", "yehudit_doors_open",
                "yehudit_gate", "street_morning", "office"]:
        ts += 60_000
        client.post(API(f"/trips/{trip_id}/taps"), json={"taps": [_tap(key, ts, seq)]})
        seq += 1
    taps = {t["checkpoint_key"]: t
            for t in client.get(API(f"/trips/{trip_id}")).json()["taps"]}
    return trip_id, taps


def test_trip_detail():
    trip_id, taps = _done_trip()
    r = client.get(API(f"/trips/{trip_id}"))
    assert r.status_code == 200
    body = r.json()
    assert body["trip"]["id"] == trip_id and body["trip"]["status"] == "done"
    assert [t["checkpoint_key"] for t in body["taps"]][0] == "home"
    assert client.get(API("/trips/nope")).status_code == 404


def test_edit_retimes_and_rederives_bookkeeping():
    trip_id, taps = _done_trip(1_000_000)
    # first tap -60s, terminal tap -20s
    r = client.post(API(f"/trips/{trip_id}/edit"), json={"taps": [
        {"id": taps["home"]["id"], "client_ts": 1_000_000 - 60_000},
        {"id": taps["office"]["id"], "client_ts": taps["office"]["client_ts"] - 20_000},
    ]})
    assert r.status_code == 200, r.text
    trip = db.get_trip(trip_id)
    assert trip["started_at"] == 940_000
    assert trip["completed_at"] == taps["office"]["client_ts"] - 20_000
    # stats bracket follows the edited hinge endpoints
    s = client.get(API("/stats")).json()
    boarding = s["panels"]["boarding"]["morning"]["options"]["pinsker"]
    assert boarding["mean_ms"] == (taps["yehudit_doors_open"]["client_ts"] - 940_000)


def test_edit_restores_trust():
    trip_id, first, _ = start_morning()
    # rapid double-tap: platform untrusted (2s < 3s threshold)
    client.post(API(f"/trips/{trip_id}/taps"), json={"taps": [
        _tap("pinsker_platform", 1_300_000, 1),
        _tap("pinsker_doors_close", 1_302_000, 2),
    ]})
    client.patch(API(f"/trips/{trip_id}"), json={"status": "done", "completed_at": 1_302_000})
    taps = {t["checkpoint_key"]: t
            for t in client.get(API(f"/trips/{trip_id}")).json()["taps"]}
    assert taps["pinsker_platform"]["ts_trusted"] is False
    # fix the platform tap's real time -> spacing is honest again -> trusted
    r = client.post(API(f"/trips/{trip_id}/edit"), json={"taps": [
        {"id": taps["pinsker_platform"]["id"], "client_ts": 1_290_000},
    ]})
    taps = {t["checkpoint_key"]: t for t in r.json()["taps"]}
    assert taps["pinsker_platform"]["ts_trusted"] is True


def test_edit_deletes_tap():
    trip_id, taps = _done_trip()
    r = client.post(API(f"/trips/{trip_id}/edit"),
                    json={"taps": [{"id": taps["yehudit_gate"]["id"], "delete": True}]})
    keys = [t["checkpoint_key"] for t in r.json()["taps"]]
    assert "yehudit_gate" not in keys and len(keys) == 6


def test_edit_metadata_and_reason_clear():
    trip_id, _ = _done_trip()
    r = client.post(API(f"/trips/{trip_id}/edit"),
                    json={"crowding": 3, "anomalous": True, "anomaly_reason": "breakdown"})
    assert r.json()["trip"]["crowding"] == 3
    assert r.json()["trip"]["anomalous"] is True
    r = client.post(API(f"/trips/{trip_id}/edit"),
                    json={"anomalous": False, "anomaly_reason": ""})
    assert r.json()["trip"]["anomalous"] is False
    assert r.json()["trip"]["anomaly_reason"] is None


def test_edit_rejections():
    # active trip: hands off
    trip_id, first, _ = start_morning()
    assert client.post(API(f"/trips/{trip_id}/edit"), json={}).status_code == 409
    assert client.post(API("/trips/nope/edit"), json={}).status_code == 404
    client.patch(API(f"/trips/{trip_id}"), json={"status": "discarded"})
    taps = client.get(API(f"/trips/{trip_id}")).json()["taps"]
    # foreign tap id
    r = client.post(API(f"/trips/{trip_id}/edit"),
                    json={"taps": [{"id": "not-a-tap", "client_ts": 1}]})
    assert r.status_code == 400
    # deleting every tap
    r = client.post(API(f"/trips/{trip_id}/edit"),
                    json={"taps": [{"id": taps[0]["id"], "delete": True}]})
    assert r.status_code == 400


def test_edit_rejects_out_of_order_timestamps():
    trip_id, taps = _done_trip(1_000_000)
    before = {t["checkpoint_key"]: t["client_ts"]
              for t in client.get(API(f"/trips/{trip_id}")).json()["taps"]}
    # push yehudit_doors_open past the gate tap that follows it
    r = client.post(API(f"/trips/{trip_id}/edit"), json={"taps": [
        {"id": taps["yehudit_doors_open"]["id"],
         "client_ts": taps["yehudit_gate"]["client_ts"] + 1},
    ]})
    assert r.status_code == 400
    # rejected atomically — nothing changed
    after = {t["checkpoint_key"]: t["client_ts"]
             for t in client.get(API(f"/trips/{trip_id}")).json()["taps"]}
    assert after == before


# ---- raw export -------------------------------------------------------------

def test_export_json_is_complete_and_unfiltered():
    # a normal done trip, a discarded one, and a rapid-double-tap (untrusted)
    done_id, _ = _done_trip(1_000_000)
    disc_id, _, _ = start_morning(5_000_000)
    client.patch(API(f"/trips/{disc_id}"), json={"status": "discarded"})
    dbl_id, _, _ = start_morning(9_000_000)
    client.post(API(f"/trips/{dbl_id}/taps"), json={"taps": [
        _tap("pinsker_platform", 9_300_000, 1),
        _tap("pinsker_doors_close", 9_302_000, 2),  # 2s -> earlier untrusted
    ]})

    body = client.get(API("/export.json")).json()
    assert body["tz"] == "Asia/Jerusalem"
    trips = {t["id"]: t for t in body["trips"]}
    # nothing filtered: done AND discarded trips both present
    assert done_id in trips and disc_id in trips and dbl_id in trips
    assert body["trip_count"] == 3
    # oldest-first ordering by started_at
    assert [t["id"] for t in body["trips"]] == [done_id, disc_id, dbl_id]
    # taps nested under their trip, untrusted tap preserved (not dropped)
    dbl_taps = {t["checkpoint_key"]: t for t in trips[dbl_id]["taps"]}
    assert dbl_taps["pinsker_platform"]["ts_trusted"] is False
    # local ISO strings sit alongside raw ms
    assert trips[done_id]["started_at_local"].startswith("1970-01-01")
    assert dbl_taps["pinsker_platform"]["client_ts"] == 9_300_000
    # download filename hint
    cd = client.get(API("/export.json")).headers["content-disposition"]
    assert cd.startswith("attachment") and cd.endswith('.json"')


def test_export_csv_row_per_tap():
    trip_id, _ = _done_trip(1_000_000)
    r = client.get(API("/export.csv"))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    import csv as _csv
    import io as _io
    rows = list(_csv.DictReader(_io.StringIO(r.text)))
    mine = [row for row in rows if row["trip_id"] == trip_id]
    assert len(mine) == 7  # home + 6 taps, one row each
    assert mine[0]["checkpoint_key"] == "home"
    assert mine[0]["direction"] == "morning"
    assert mine[0]["client_ts"] == "1000000"


def test_trip_log_and_discard_toggle():
    trip_id, first, _ = start_morning()
    client.patch(API(f"/trips/{trip_id}"), json={"status": "discarded"})
    log = client.get(API("/trips")).json()["trips"]
    row = next(t for t in log if t["id"] == trip_id)
    assert row["status"] == "discarded"
