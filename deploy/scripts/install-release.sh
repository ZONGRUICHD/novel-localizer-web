#!/bin/sh
set -eu

SHA=${1:-}

case "$SHA" in
  ???????*) ;;
  *) echo "invalid git SHA" >&2; exit 2 ;;
esac
case "$SHA" in *[!0-9a-f]*) echo "invalid git SHA" >&2; exit 2 ;; esac

INCOMING_DIR=/var/tmp/shiori-deploy/$SHA
ARCHIVE=$INCOMING_DIR/shiori-source.tar.gz
set -- "$INCOMING_DIR"/*.whl
WHEEL=${1:-}
if [ "$#" -ne 1 ] || [ ! -f "$ARCHIVE" ] || [ ! -f "$WHEEL" ] || [ -L "$ARCHIVE" ] || [ -L "$WHEEL" ]; then
  echo "expected one non-symlink source archive and wheel under $INCOMING_DIR" >&2
  exit 2
fi

RELEASE_ROOT=/opt/shiori/releases
RELEASE_DIR=$RELEASE_ROOT/$SHA
STAGING_DIR=$RELEASE_ROOT/.staging-$SHA

if [ -e "$RELEASE_DIR" ] || [ -e "$STAGING_DIR" ]; then
  echo "release already exists: $SHA" >&2
  exit 3
fi

if ! tar -tzf "$ARCHIVE" | awk '
  /^\// || /(^|\/)\.\.($|\/)/ || !/^(backend|deploy)\// { exit 1 }
  END { if (NR == 0) exit 1 }
'; then
  echo "archive contains an unsafe or unexpected path" >&2
  exit 4
fi

# A release is extracted before any root-owned service starts from it.  Links
# inside a tar archive can turn an otherwise allowed relative name into an
# escape from the staging tree, so release archives deliberately contain only
# ordinary files and directories.
if ! tar -tvzf "$ARCHIVE" | awk '
  substr($1, 1, 1) == "l" || substr($1, 1, 1) == "h" { exit 1 }
'; then
  echo "archive must not contain symbolic or hard links" >&2
  exit 4
fi

install -d -o shiori -g shiori -m 0755 "$STAGING_DIR"
tar -xzf "$ARCHIVE" -C "$STAGING_DIR" --no-same-owner --no-same-permissions
test -f "$STAGING_DIR/backend/pyproject.toml"
test -f "$STAGING_DIR/deploy/scripts/install-release.sh"
chown -R shiori:shiori "$STAGING_DIR"
runuser -u shiori -- python3 -m venv "$STAGING_DIR/venv"
runuser -u shiori -- "$STAGING_DIR/venv/bin/pip" install --disable-pip-version-check --no-input "$WHEEL"
chown -R root:root "$STAGING_DIR"

mv "$STAGING_DIR" "$RELEASE_DIR"
PREVIOUS=$(readlink -f /opt/shiori/current 2>/dev/null || true)
ln -s "$RELEASE_DIR" "$RELEASE_ROOT/.current-$SHA"
mv -Tf "$RELEASE_ROOT/.current-$SHA" /opt/shiori/current
if ! systemctl restart shiori-api.service shiori-worker.service; then
  if [ -n "$PREVIOUS" ] && [ -d "$PREVIOUS" ]; then
    ln -s "$PREVIOUS" "$RELEASE_ROOT/.rollback-$SHA"
    mv -Tf "$RELEASE_ROOT/.rollback-$SHA" /opt/shiori/current
    systemctl restart shiori-api.service shiori-worker.service || true
  fi
  echo "release failed health/start checks; current link rolled back when possible" >&2
  exit 5
fi
systemctl is-active --quiet shiori-api.service && systemctl is-active --quiet shiori-worker.service

echo "deployed $SHA"
