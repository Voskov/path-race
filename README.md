# Path Race

A personal commute experiment. One user, comparing ways of getting from home
(Petah Tikva) to the office (HaMelacha St, Tel Aviv) on the Dankal Red Line +
electric scooter. A mobile-web tap logger: tap checkpoints as you pass them, the
server stores timestamps, a stats page compares options over time.

No accounts. The app is protected only by an **unguessable URL path prefix** —
treat that prefix like a password.

## Stack

- **Backend:** FastAPI + SQLite (single file, volume-mounted), uvicorn.
- **Frontend:** single-page offline-first PWA (vanilla JS). Slide-to-commit taps,
  localStorage offline queue, cold-reload restore, service-worker app shell.
- **Deploy:** Docker Compose, behind your existing nginx/HTTPS.

## How it works

The checkpoint graph (`backend/app/graph.py`) is the single source of truth for
both directions. The UI renders the outgoing edges of the current node as
slide-to-commit options; the path taken infers every choice (line, boarding
station, office station) — nothing is declared up front. Direction is inferred
from the first tap (Home → morning, Office → evening); the trip completes on the
terminal tap.

Two experiments, each measured both directions, both bracketed by the **Yehudit
hinge** so totals are comparable:

| Experiment | Question | Morning bracket | Evening bracket |
|---|---|---|---|
| Boarding option | Which home-side station is fastest? | Home → Yehudit doors open | Yehudit doors close → Home |
| Office station | Yehudit or Carlebach? | Yehudit doors open → Office | Office → Yehudit doors close |

Client timestamps are authoritative (the network is a background sync that never
blocks the UI). Tap upload is idempotent by client-generated tap id. Two taps
committed less than `DOUBLE_TAP_THRESHOLD_S` apart mark the **earlier** tap
`ts_trusted=false` — it keeps its place in the path (needed for branch
inference) but is excluded from segment stats; bracket totals survive as long as
their endpoint taps are trusted.

## Run locally (dev)

```bash
python -m venv .venv
.venv/Scripts/pip install -r backend/requirements-dev.txt   # Windows
# source .venv/bin/activate && pip install -r backend/requirements-dev.txt   # *nix
cd backend
PATH_PREFIX=race-dev DB_PATH=../data/dev.db python -m uvicorn app.main:app --reload
```

Open `http://localhost:8000/race-dev/` (logger) and `/race-dev/stats` (stats).
Geolocation needs HTTPS, so the location-ranking filter simply stays off on
plain-HTTP localhost — every other feature works.

Run the tests:

```bash
cd backend && ../.venv/Scripts/python -m pytest -q
```

## Deploy (Docker Compose behind a shared nginx proxy)

The container publishes **no host port** — it joins the external `infra` Docker
network and is reached by name (`pathrace:8000`) from a shared nginx reverse
proxy that terminates TLS. Adapt if your proxy differs.

```bash
cp .env.example .env        # then edit — set your own secret PATH_PREFIX
docker compose up -d --build
```

The DB lives in the `pathrace-db` volume. Everything is served under
`/${PATH_PREFIX}/`; the bare site exposes nothing, so the prefix is the secret.

### nginx (reverse proxy)

A ready subdomain server block is in [`deploy/pathrace.conf`](deploy/pathrace.conf)
— it proxies `location /` to `http://pathrace:8000` (the full URI, including the
prefix, is passed through). Point a DNS record at the host, drop the conf into
your proxy's `conf.d/`, bootstrap the cert, and reload. If you instead front the
app on the host, `proxy_pass http://127.0.0.1:8000;` works too.

HTTPS is required for geolocation and for the PWA/service worker to install.

## Configuration (env vars)

| Var | Default | Meaning |
|---|---|---|
| `PATH_PREFIX` | `race-e97f41eb35828b03` | Unguessable path prefix (the only protection) |
| `PORT` | `8000` | Listen port |
| `DB_PATH` | `data/pathrace.db` | SQLite file path |
| `DOUBLE_TAP_THRESHOLD_S` | `7` | Rapid-double-tap timestamp-invalidation window |
| `UNDO_TOAST_MS` | `5000` | Post-commit undo toast duration |
| `LOCATION_MAX_ACCURACY_M` | `75` | Worse GPS accuracy ⇒ location filter off |
| `LOCATION_STALE_MS` | `30000` | Older fix ⇒ location filter off |
| `LOCATION_FOLD_SIZE` | `3` | Options kept above the "more…" fold |
| `TOD_MORNING_BOUNDARY` | `08:30` | Morning time-of-day split (Asia/Jerusalem) |
| `TOD_EVENING_BOUNDARY` | `12:00` | Evening time-of-day split (Asia/Jerusalem) |

## API

```
GET  /{prefix}/api/config              graph + client config
GET  /{prefix}/api/state               active trip + taps (reconcile / cold-reload)
POST /{prefix}/api/trips               create (first tap implies direction)
POST /{prefix}/api/trips/{id}/taps     idempotent batch tap upload (offline queue)
POST /{prefix}/api/trips/{id}/undo     remove last tap
PATCH /{prefix}/api/trips/{id}         crowding / status / anomalous / complete
GET  /{prefix}/api/stats               everything the stats page needs
GET  /{prefix}/api/trips               trip log
```

Data loss is acceptable, friction is not — hence the offline queue and the
single-tap post-commit undo instead of heavier safeguards.
