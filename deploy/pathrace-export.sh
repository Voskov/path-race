#!/usr/bin/env bash
# Snapshot the Path Race DB and push it to Google Drive.
#
# Runs on the deploy host from cron (see pathrace-export.cron). It pulls the
# snapshot straight out of the running container with `docker exec`, so it never
# needs the secret PATH_PREFIX or a published host port. Two files land in the
# Drive folder on every run:
#
#   pathrace-latest.<ext>              overwritten each run — a stable path to read
#   pathrace-export-<stamp>.<ext>      timestamped archive — history over time
#
# One-time setup lives in DRIVE-EXPORT.md (installing rclone + `rclone config`).
#
# Tunables (env vars, all optional):
#   RCLONE_REMOTE   rclone remote name              (default: gdrive)
#   DRIVE_DIR       destination folder in the remote (default: pathrace log)
#   CONTAINER       docker container name           (default: pathrace)
#   FORMAT          csv | json                      (default: csv)
#   STRIP_LOCATION  set to 1 to drop lat/lng/accuracy (default: unset = keep)
set -euo pipefail

RCLONE_REMOTE="${RCLONE_REMOTE:-gdrive}"
DRIVE_DIR="${DRIVE_DIR:-pathrace log}"
CONTAINER="${CONTAINER:-pathrace}"
FORMAT="${FORMAT:-csv}"

strip_flag=()
[ "${STRIP_LOCATION:-0}" = "1" ] && strip_flag=(--strip-location)

stamp="$(TZ=Asia/Jerusalem date +%Y%m%d-%H%M%S)"
tmp="$(mktemp --suffix=".${FORMAT}")"
trap 'rm -f "$tmp"' EXIT

# Dump the whole DB (nothing filtered) to a temp file.
docker exec "$CONTAINER" python -m app.export "$FORMAT" "${strip_flag[@]}" > "$tmp"

dest="${RCLONE_REMOTE}:${DRIVE_DIR}"
rclone copyto "$tmp" "${dest}/pathrace-latest.${FORMAT}"
rclone copyto "$tmp" "${dest}/pathrace-export-${stamp}.${FORMAT}"

echo "$(date -Is) pushed ${FORMAT} snapshot ($(wc -c < "$tmp") bytes) to ${dest}"
