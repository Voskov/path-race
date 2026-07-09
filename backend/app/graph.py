"""Static checkpoint graph — single source of truth for both directions.

The UI renders the outgoing edges of the current node as tappable options; the
path taken infers every choice (line / boarding station / office station). No
choice is ever declared up front.

Node attributes:
  display   : English label shown in the UI
  next      : outgoing edges (checkpoint keys)
  optional  : ride-through / reserve node — skippable, treated specially in stats
  hinge     : shared bracket endpoint. "main" = passed on every trip in the
              direction; "secondary" = shared by the R1 boarding family only.
  boarding  : True if this node represents a home-side boarding/alighting event
              (used to infer the boarding_option experiment).
  office    : "yehudit" | "carlebach" when the node reveals the office-station
              choice (used to infer the office_station experiment).
  lat, lng  : approximate coordinates for location-plausibility ranking. Rough
              on purpose — location only demotes obviously-wrong options, it
              never removes one and never auto-commits.
"""

# Coordinates (WGS84) for location-plausibility ranking only — never removes an
# option, so precision is not critical. Station values marked (real) are the
# Google Maps place coordinates; (est) are estimates pending a real fix.
_C = {
    "home":            (32.107083, 34.879899),  # (real) Shraga Refa'eli St 9, Petah Tikva
    "office":          (32.066724, 34.785536),  # (real) HaMelacha area, Tel Aviv
    "pinsker":         (32.093731, 34.882202),  # (real)
    "kroll":           (32.091911, 34.877506),  # (real)
    "dankner":         (32.090950, 34.872166),  # (real)
    "beilinson":       (32.091461, 34.866802),  # (real)
    "shaham":          (32.092670, 34.853252),  # (real, labelled "Shenkar" on maps)
    "kiryat_arye":     (32.105930, 34.861897),  # (real)
    "yehudit":         (32.070168, 34.788406),  # (real)
    "carlebach":       (32.065273, 34.782825),  # (real)
    "yehudit_street":   (32.070168, 34.788406),  # (est) street exit above Yehudit
    "carlebach_street": (32.065273, 34.782825),  # (est) street exit above Carlebach
    "street_evening":  (32.0600, 34.7900),   # (est)
}


def _n(display, nxt, station=None, optional=False, hinge=None,
       boarding=False, office=None):
    lat, lng = _C.get(station, (None, None))
    return {
        "display": display,
        "next": nxt,
        "optional": optional,
        "hinge": hinge,
        "boarding": boarding,
        "office": office,
        "lat": lat,
        "lng": lng,
    }


MORNING = {
    "home": _n("Home", [
        "pinsker_platform", "kiryat_arye_entrance", "shaham_platform",
        "kroll_platform", "dankner_platform", "beilinson_platform",
    ], station="home"),

    # Surface platform arrivals — mandatory boundary between the scoot segment
    # (home → platform) and the wait segment (platform → doors_close). The
    # optional flag on reserve stations is the reserve-station styling cue only;
    # once on a path the platform tap is required by the wiring.
    "pinsker_platform":   _n("Pinsker · platform",   ["pinsker_doors_close"],   station="pinsker"),
    "shaham_platform":    _n("Shaham · platform",    ["shaham_doors_close"],    station="shaham"),
    "kroll_platform":     _n("Kroll · platform",     ["kroll_doors_close"],     station="kroll",     optional=True),
    "dankner_platform":   _n("Dankner · platform",   ["dankner_doors_close"],   station="dankner",   optional=True),
    "beilinson_platform": _n("Beilinson · platform", ["beilinson_doors_close"], station="beilinson", optional=True),

    # R1 boardings — the boarding tap is doors_close, reached via the platform
    # node. Each may optionally ride through Shaham (direct edge, no platform),
    # or skip straight to the Yehudit hinge.
    "pinsker_doors_close":   _n("Pinsker · doors close",   ["shaham_doors_close", "yehudit_doors_open"], station="pinsker", boarding=True),
    "kroll_doors_close":     _n("Kroll · doors close",     ["shaham_doors_close", "yehudit_doors_open"], station="kroll",     boarding=True, optional=True),
    "dankner_doors_close":   _n("Dankner · doors close",   ["shaham_doors_close", "yehudit_doors_open"], station="dankner",   boarding=True, optional=True),
    "beilinson_doors_close": _n("Beilinson · doors close", ["shaham_doors_close", "yehudit_doors_open"], station="beilinson", boarding=True, optional=True),

    # Shaham: last surface stop before the tunnel. Primary boarding AND the
    # secondary hinge shared by the whole R1 family.
    "shaham_doors_close": _n("Shaham · doors close", ["yehudit_doors_open"], station="shaham", boarding=True, hinge="secondary"),

    # Kiryat Arye (R3 terminus) has internal structure worth diagnosing.
    "kiryat_arye_entrance":    _n("Kiryat Arye · entrance",    ["kiryat_arye_platform"],    station="kiryat_arye"),
    "kiryat_arye_platform":    _n("Kiryat Arye · platform",    ["kiryat_arye_doors_close"], station="kiryat_arye"),
    "kiryat_arye_doors_close": _n("Kiryat Arye · doors close", ["yehudit_doors_open"],      station="kiryat_arye", boarding=True),

    # Main hinge — every morning trip passes it.
    "yehudit_doors_open": _n("Yehudit · doors open", ["yehudit_gate", "carlebach_doors_open"], station="yehudit", hinge="main"),

    # Office-station choice. Each station has its own street node — the
    # platform→street climb and the street exit location differ per station.
    "yehudit_gate":         _n("Yehudit · exit gate",  ["yehudit_street"], station="yehudit",   office="yehudit"),
    "carlebach_doors_open": _n("Carlebach · doors open", ["carlebach_gate"], station="carlebach", office="carlebach"),
    "carlebach_gate":       _n("Carlebach · exit gate", ["carlebach_street"], station="carlebach", office="carlebach"),

    "yehudit_street":   _n("Yehudit · street (scoot to office)",   ["office"], station="yehudit_street",   office="yehudit"),
    "carlebach_street": _n("Carlebach · street (scoot to office)", ["office"], station="carlebach_street", office="carlebach"),
    "office":           _n("Office", [], station="office"),  # terminal
}


EVENING = {
    "office": _n("Office", ["street_evening"], station="office"),

    "street_evening": _n("Street (scoot to station)", ["yehudit_gate_in", "carlebach_gate_in"], station="street_evening"),

    # Office-station choice (reverse).
    "yehudit_gate_in":   _n("Yehudit · entrance gate",   ["yehudit_platform"],   station="yehudit",   office="yehudit"),
    "carlebach_gate_in": _n("Carlebach · entrance gate", ["carlebach_platform"], station="carlebach", office="carlebach"),

    "yehudit_platform":   _n("Yehudit · platform",   ["yehudit_doors_close"],   station="yehudit"),
    "carlebach_platform": _n("Carlebach · platform", ["carlebach_doors_close"], station="carlebach"),

    # Carlebach branch must ride through the Yehudit hinge — mandatory tap.
    "carlebach_doors_close": _n("Carlebach · doors close", ["yehudit_doors_close"], station="carlebach", office="carlebach", optional=True),

    # Main hinge — every evening trip passes it.
    "yehudit_doors_close": _n("Yehudit · doors close", [
        "shaham_doors_open", "pinsker_doors_open", "kiryat_arye_doors_open",
        "kroll_doors_open", "dankner_doors_open", "beilinson_doors_open",
    ], station="yehudit", hinge="main"),

    # Secondary hinge (ride-through / R1 alighting family).
    "shaham_doors_open": _n("Shaham · doors open", ["pinsker_doors_open", "home"], station="shaham", boarding=True, hinge="secondary"),

    "pinsker_doors_open":   _n("Pinsker · doors open",   ["home"], station="pinsker",   boarding=True),
    "kroll_doors_open":     _n("Kroll · doors open",     ["home"], station="kroll",     boarding=True, optional=True),
    "dankner_doors_open":   _n("Dankner · doors open",   ["home"], station="dankner",   boarding=True, optional=True),
    "beilinson_doors_open": _n("Beilinson · doors open", ["home"], station="beilinson", boarding=True, optional=True),

    "kiryat_arye_doors_open": _n("Kiryat Arye · doors open", ["kiryat_arye_exit"], station="kiryat_arye", boarding=True),
    "kiryat_arye_exit":       _n("Kiryat Arye · exit",       ["home"],             station="kiryat_arye"),

    "home": _n("Home", [], station="home"),  # terminal
}


GRAPH = {
    "morning": {"start": "home",   "terminal": "office", "nodes": MORNING},
    "evening": {"start": "office", "terminal": "home",   "nodes": EVENING},
}

# The two entry options shown when no trip is active. First tap fixes direction.
ENTRY_OPTIONS = [
    {"key": "home",   "direction": "morning", "display": "Home → Office (morning)"},
    {"key": "office", "direction": "evening", "display": "Office → Home (evening)"},
]


def direction_of_start(key: str) -> str | None:
    for opt in ENTRY_OPTIONS:
        if opt["key"] == key:
            return opt["direction"]
    return None


def nodes(direction: str) -> dict:
    return GRAPH[direction]["nodes"]


def terminal(direction: str) -> str:
    return GRAPH[direction]["terminal"]


def node(direction: str, key: str) -> dict | None:
    return GRAPH[direction]["nodes"].get(key)


def options_for(direction: str | None, current_key: str | None) -> list[dict]:
    """Outgoing edges of the current node as option dicts. If no trip is active
    (direction is None), returns the two entry options."""
    if direction is None or current_key is None:
        out = []
        for opt in ENTRY_OPTIONS:
            n = node(opt["direction"], opt["key"])
            out.append(_opt_dict(opt["key"], n))
        return out
    n = node(direction, current_key)
    if not n:
        return []
    return [_opt_dict(k, node(direction, k)) for k in n["next"]]


def _opt_dict(key: str, n: dict) -> dict:
    return {
        "key": key,
        "display": n["display"],
        "optional": n["optional"],
        "hinge": n["hinge"],
        "lat": n["lat"],
        "lng": n["lng"],
    }


def as_config() -> dict:
    """Full graph serialized for the client (drives the state machine there too,
    so the UI can render options offline)."""
    def dump(direction):
        return {
            "start": GRAPH[direction]["start"],
            "terminal": GRAPH[direction]["terminal"],
            "nodes": {
                k: {
                    "display": v["display"],
                    "next": v["next"],
                    "optional": v["optional"],
                    "hinge": v["hinge"],
                    "lat": v["lat"],
                    "lng": v["lng"],
                }
                for k, v in GRAPH[direction]["nodes"].items()
            },
        }
    return {
        "morning": dump("morning"),
        "evening": dump("evening"),
        "entry_options": ENTRY_OPTIONS,
    }
