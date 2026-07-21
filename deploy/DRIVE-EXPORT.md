# Automated DB snapshots → Google Drive

The trip data lives in a SQLite file inside the container's `/data` volume, so
it isn't in git and isn't reachable for offline analysis. This job pushes a full
snapshot to a Google Drive folder twice a day (01:00 and 13:00 Asia/Jerusalem),
so an analysis session (e.g. Claude with a Drive connector) can read fresh data
with **no manual step** once it's set up.

```
container `pathrace`  ──docker exec──►  export CSV  ──rclone──►  Drive: "pathrace log"
        (cron on the host, 01:00 & 13:00)                         Einstine1984@gmail.com
```

Nothing is filtered — discarded/anomalous trips, untrusted taps and the debug
fields are all included (same as the `/export.csv` endpoint), so every stat can
be re-derived from the file.

## One-time setup (the only manual part)

Do this once on the deploy host, as the user cron will run as.

1. **Install rclone** — `curl https://rclone.org/install.sh | sudo bash`
   (or your distro's package).

2. **Authorize Google Drive.** Run `rclone config` and add a remote:
   - name it **`gdrive`** (matches the script default; override with
     `RCLONE_REMOTE` otherwise),
   - storage type **`drive`**,
   - accept the default (full) scope,
   - complete the browser OAuth **as `Einstine1984@gmail.com`** — this must be
     the same Google account the analysis/Drive connector reads from, or the
     files won't be visible to it.

   On a headless box, run `rclone authorize "drive"` on a machine with a browser
   and paste the token back (rclone prompts for this).

3. **Confirm the target folder** is reachable. rclone creates it on first write,
   but you can pre-make it: `rclone mkdir "gdrive:pathrace log"`.

4. **Place the script** where the cron entry expects it (or edit the path in the
   cron file). The cron file assumes `/opt/path-race/deploy/pathrace-export.sh`:
   ```
   sudo install -m 755 deploy/pathrace-export.sh /opt/path-race/deploy/pathrace-export.sh
   ```

5. **Install the schedule:**
   ```
   crontab -l 2>/dev/null | cat - deploy/pathrace-export.cron | crontab -
   ```

6. **Smoke-test it now:**
   ```
   /opt/path-race/deploy/pathrace-export.sh
   rclone ls "gdrive:pathrace log"
   ```
   You should see `pathrace-latest.csv` and a timestamped archive copy.

## What lands in Drive

| File | Purpose |
|---|---|
| `pathrace-latest.csv` | Overwritten each run — a **stable path** to always read the newest data |
| `pathrace-export-<stamp>.csv` | Timestamped archive — keeps history so you can see how the data grew |

## Tunables

All optional env vars, read by `pathrace-export.sh`:

| Var | Default | Meaning |
|---|---|---|
| `RCLONE_REMOTE` | `gdrive` | rclone remote name from `rclone config` |
| `DRIVE_DIR` | `pathrace log` | destination folder in the remote |
| `CONTAINER` | `pathrace` | docker container to dump from |
| `FORMAT` | `csv` | `csv` or `json` |
| `STRIP_LOCATION` | *(unset)* | set `1` to drop `lat`/`lng`/`accuracy` before upload |

Since the Drive folder is your own private account the raw GPS fields are kept
by default. If you'd rather they never leave the box, set `STRIP_LOCATION=1` in
the cron environment (`python -m app.export csv --strip-location`).
