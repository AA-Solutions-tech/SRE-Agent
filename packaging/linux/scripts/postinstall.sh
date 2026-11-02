#!/bin/sh
# Runs after files are unpacked. deb: $1 = configure, $2 = previous version (empty on first install)
#                                rpm: $1 = 1 (install) | 2 (upgrade)
set -e

ENV_FILE=/etc/sre-agent/sre-agent.env
TEMPLATE=/usr/share/sre-agent/sre-agent.env.example
PY=/opt/sre-agent/python/bin/python3

first_install=false
case "$1" in
  configure) [ -z "${2:-}" ] && first_install=true ;;
  1) first_install=true ;;
esac

install -d -m 0750 -o sre-agent -g sre-agent /var/lib/sre-agent /var/log/sre-agent
install -d -m 0700 -o sre-agent -g sre-agent /var/lib/sre-agent/.ssh
install -d -m 0750 -o root -g sre-agent /etc/sre-agent

# Config is created once from the template and never overwritten (also survives remove + reinstall).
if [ ! -f "$ENV_FILE" ]; then
  install -m 0640 -o root -g sre-agent "$TEMPLATE" "$ENV_FILE"
  echo "sre-agent: created $ENV_FILE"
fi
# Each __GENERATED__ placeholder gets its own random secret (unique per server, never shipped).
while grep -q "__GENERATED__" "$ENV_FILE"; do
  SECRET=$("$PY" -I -c "import secrets; print(secrets.token_hex(32))")
  sed -i "0,/__GENERATED__/s//$SECRET/" "$ENV_FILE"
done
chown root:sre-agent "$ENV_FILE"
chmod 0640 "$ENV_FILE"

if [ -d /run/systemd/system ]; then
  systemctl daemon-reload || true
  if [ "$first_install" = true ]; then
    systemctl enable sre-agent >/dev/null 2>&1 || true
  fi
  if [ -f /run/sre-agent-pkg/restart ]; then
    rm -f /run/sre-agent-pkg/restart
    systemctl start sre-agent || echo "sre-agent: service failed to start, see: journalctl -u sre-agent"
  fi
fi

if [ "$first_install" = true ]; then
  cat <<MSG

SRE Agent $(cat /opt/sre-agent/VERSION) installed. The service is enabled but NOT started yet.
  Configure it:   sudo sre-agent setup
                  (asks for the LLM key, where to send alerts and which logs to watch, checks the
                   result and starts the service; API key + approval secret are already generated)
  Or edit by hand: sudo nano $ENV_FILE  ->  sudo sre-agent doctor  ->  sudo systemctl start sre-agent
  Logs: journalctl -u sre-agent -f   |   /var/log/sre-agent/sre.log

MSG
fi
exit 0
