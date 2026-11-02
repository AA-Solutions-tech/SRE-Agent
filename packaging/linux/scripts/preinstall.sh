#!/bin/sh
# Runs before files are unpacked. deb: $1 = install|upgrade   rpm: $1 = 1 (install) | 2 (upgrade)
set -e

if ! getent group sre-agent >/dev/null 2>&1; then
  groupadd --system sre-agent
fi
if ! getent passwd sre-agent >/dev/null 2>&1; then
  useradd --system --gid sre-agent --home-dir /var/lib/sre-agent --no-create-home \
    --shell /usr/sbin/nologin --comment "SRE Agent" sre-agent
fi

# Upgrade: stop a running service (restarted by postinstall) and back up the DB before new code migrates it.
if [ -d /run/systemd/system ] && systemctl is-active --quiet sre-agent 2>/dev/null; then
  mkdir -p /run/sre-agent-pkg && touch /run/sre-agent-pkg/restart
  systemctl stop sre-agent || true
fi

DB=/var/lib/sre-agent/starter.db
OLD_PY=/opt/sre-agent/python/bin/python3
if [ -f "$DB" ] && [ -x "$OLD_PY" ]; then
  OLD_VER=$(cat /opt/sre-agent/VERSION 2>/dev/null || echo unknown)
  BK_DIR=/var/lib/sre-agent/backups
  mkdir -p "$BK_DIR"
  BK="$BK_DIR/starter.db.$OLD_VER.$(date +%Y%m%d%H%M%S)"
  "$OLD_PY" -I -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" "$DB" "$BK" \
    && echo "sre-agent: database backed up to $BK"
  # keep the 5 newest backups
  ls -1t "$BK_DIR"/starter.db.* 2>/dev/null | tail -n +6 | while read -r old; do rm -f "$old"; done
  chown -R sre-agent:sre-agent "$BK_DIR" 2>/dev/null || true
fi
exit 0
