#!/bin/sh
# deb: $1 = remove|purge|upgrade|...   rpm: $1 = 0 (erase) | 1 (upgrade)
# Data in /var/lib/sre-agent (DB, repo clone, backups) is never deleted automatically.
set -e
case "$1" in
  remove|0)
    [ -d /run/systemd/system ] && systemctl daemon-reload || true
    echo "sre-agent: removed. Config kept in /etc/sre-agent, data kept in /var/lib/sre-agent."
    ;;
  purge)
    rm -rf /etc/sre-agent /var/log/sre-agent /run/sre-agent-pkg
    if getent passwd sre-agent >/dev/null 2>&1; then userdel sre-agent || true; fi
    if getent group sre-agent >/dev/null 2>&1; then groupdel sre-agent || true; fi
    echo "sre-agent: purged config and logs. Data left in /var/lib/sre-agent (delete it yourself if unneeded)."
    ;;
esac
exit 0
