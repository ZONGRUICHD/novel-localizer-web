#!/bin/sh
set -eu

DATA_PATH=${SHIORI_PREFLIGHT_DATA_PATH:-/var/lib/shiori}
MIN_AVAILABLE_KIB=10485760
PORT=18740

echo "Shiori read-only preflight"

if ! command -v systemctl >/dev/null 2>&1; then
  echo "BLOCKED: systemd is required" >&2
  exit 10
fi

if [ -r /etc/os-release ]; then
  sed -n 's/^\(PRETTY_NAME\|ID\|VERSION_ID\)=/\1=/p' /etc/os-release
else
  echo "BLOCKED: cannot read /etc/os-release" >&2
  exit 11
fi

CHECK_PATH=$DATA_PATH
if [ ! -e "$CHECK_PATH" ]; then
  CHECK_PATH=/var/lib
fi
AVAILABLE_KIB=$(df -Pk "$CHECK_PATH" | awk 'NR == 2 { print $4 }')
echo "available_kib=$AVAILABLE_KIB path=$CHECK_PATH"
if [ -z "$AVAILABLE_KIB" ] || [ "$AVAILABLE_KIB" -lt "$MIN_AVAILABLE_KIB" ]; then
  echo "BLOCKED: less than 10 GiB available" >&2
  exit 12
fi

if ss -ltnH "sport = :$PORT" 2>/dev/null | grep -q .; then
  echo "BLOCKED: 127.0.0.1:$PORT is already in use" >&2
  ss -ltnp "sport = :$PORT" || true
  exit 13
fi

echo "listeners (read-only):"
ss -ltnp 2>/dev/null || true

echo "UFW status (read-only):"
if command -v ufw >/dev/null 2>&1; then
  ufw status verbose || true
else
  echo "ufw not installed"
fi

echo "existing cloudflared units (read-only):"
systemctl list-unit-files --type=service --no-legend 2>/dev/null | grep -i cloudflared || true

echo "existing Shiori-name collisions (read-only):"
systemctl list-unit-files --type=service --no-legend 2>/dev/null | grep -E '^shiori-|^cloudflared-shiori' || true

echo "existing service on 11435 (must remain untouched):"
ss -ltnp "sport = :11435" 2>/dev/null || true

echo "PRECHECK_OK: no changes were made"
