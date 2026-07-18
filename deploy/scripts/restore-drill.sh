#!/bin/sh
set -eu

: "${RESTIC_REPOSITORY:?RESTIC_REPOSITORY must be configured}"
: "${RESTIC_PASSWORD_FILE:?RESTIC_PASSWORD_FILE must be configured}"

TARGET=${1:-/var/lib/shiori/restore-drill}
case "$TARGET" in
  /var/lib/shiori/restore-drill|/var/lib/shiori/restore-drill/*) ;;
  *) echo "restore target must stay inside /var/lib/shiori/restore-drill" >&2; exit 2 ;;
esac

if [ -e "$TARGET" ]; then
  echo "restore target already exists: $TARGET" >&2
  exit 3
fi

install -d -m 0700 "$TARGET"
restic restore latest --target "$TARGET" --tag shiori-daily
RESTORED_DB=$(find "$TARGET" -type f -name 'shiori-*.sqlite3' | sort | tail -n 1)
test -n "$RESTORED_DB"
python3 - "$RESTORED_DB" <<'PY'
import sqlite3
import sys

connection = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
result = connection.execute("PRAGMA integrity_check").fetchone()[0]
connection.close()
if result != "ok":
    raise SystemExit(f"integrity check failed: {result}")
print("RESTORE_DRILL_OK")
PY
