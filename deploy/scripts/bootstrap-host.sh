#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "bootstrap-host.sh must run as root after the read-only preflight" >&2
  exit 2
fi

if [ "${SHIORI_BOOTSTRAP_CONFIRM:-}" != "INSTALL_SHIORI_ONLY" ]; then
  echo "set SHIORI_BOOTSTRAP_CONFIRM=INSTALL_SHIORI_ONLY after password rotation and SSH-key setup" >&2
  exit 3
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEPLOY_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

"$SCRIPT_DIR/preflight.sh"

if ! getent group shiori >/dev/null 2>&1; then
  groupadd --system shiori
fi
if ! getent passwd shiori >/dev/null 2>&1; then
  useradd --system --gid shiori --home-dir /var/lib/shiori --shell /usr/sbin/nologin shiori
fi
if ! getent group cloudflared >/dev/null 2>&1; then
  groupadd --system cloudflared
fi
if ! getent passwd cloudflared >/dev/null 2>&1; then
  useradd --system --gid cloudflared --home-dir /var/lib/cloudflared --shell /usr/sbin/nologin cloudflared
fi

install -d -o root -g root -m 0755 /opt/shiori/releases
install -d -o shiori -g shiori -m 0700 \
  /var/lib/shiori \
  /var/lib/shiori/db \
  /var/lib/shiori/uploads \
  /var/lib/shiori/artifacts \
  /var/lib/shiori/tmp \
  /var/lib/shiori/backups \
  /var/lib/shiori/restore-drill
install -d -o root -g shiori -m 0750 /etc/shiori /etc/shiori/credentials

install -o root -g root -m 0755 "$SCRIPT_DIR/install-release.sh" /usr/local/sbin/shiori-install-release
if [ -f "$DEPLOY_DIR/sudoers.d/shiori-deploy" ]; then
  install -o root -g root -m 0440 "$DEPLOY_DIR/sudoers.d/shiori-deploy" /etc/sudoers.d/shiori-deploy
  visudo -cf /etc/sudoers.d/shiori-deploy
fi
for unit in "$DEPLOY_DIR"/systemd/*; do
  install -o root -g root -m 0644 "$unit" "/etc/systemd/system/$(basename "$unit")"
done

if [ ! -f /etc/shiori/shiori.env ]; then
  install -o root -g shiori -m 0640 "$DEPLOY_DIR/shiori.env.example" /etc/shiori/shiori.env
fi

systemctl daemon-reload
echo "BOOTSTRAP_OK"
echo "Next: edit /etc/shiori/shiori.env, create encrypted credentials, install cloudflared, and deploy a tested release."
echo "No service was started and no firewall, SSH, port 11435, DNS, or existing Tunnel was changed."
