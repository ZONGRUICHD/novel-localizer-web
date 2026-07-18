#!/bin/sh
set -eu

: "${RESTIC_REPOSITORY:?RESTIC_REPOSITORY must be configured}"
: "${CREDENTIALS_DIRECTORY:?systemd credentials are required}"

export AWS_ACCESS_KEY_ID
export AWS_SECRET_ACCESS_KEY
AWS_ACCESS_KEY_ID=$(tr -d '\r\n' < "$CREDENTIALS_DIRECTORY/r2-access-key-id")
AWS_SECRET_ACCESS_KEY=$(tr -d '\r\n' < "$CREDENTIALS_DIRECTORY/r2-secret-access-key")

DB=/var/lib/shiori/db/shiori.sqlite3
SNAPSHOT_DIR=/var/lib/shiori/backups
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
SNAPSHOT=$SNAPSHOT_DIR/shiori-$STAMP.sqlite3

install -d -m 0700 "$SNAPSHOT_DIR"
python3 - "$DB" "$SNAPSHOT" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
target = sqlite3.connect(sys.argv[2])
with target:
    source.backup(target)
result = target.execute("PRAGMA integrity_check").fetchone()[0]
source.close()
target.close()
if result != "ok":
    raise SystemExit(f"snapshot integrity check failed: {result}")
PY

restic backup \
  "$SNAPSHOT" \
  /var/lib/shiori/uploads \
  /var/lib/shiori/artifacts \
  --tag shiori-daily \
  --exclude-caches

restic forget --prune --keep-daily 7 --keep-weekly 4 --keep-monthly 12
find "$SNAPSHOT_DIR" -xdev -type f -name 'shiori-*.sqlite3' -mtime +7 -delete
