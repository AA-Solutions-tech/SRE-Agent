#!/bin/sh
# Service lifecycle under a real systemd (install -> start -> API -> upgrade while running -> remove).
# Run inside a systemd container, e.g.:
#   printf 'FROM debian:12\nRUN apt-get update && apt-get install -y systemd systemd-sysv procps curl\nCMD ["/lib/systemd/systemd"]\n' \
#     | docker build -t sre-systemd-test -
#   docker run -d --name sre-systemd-test --privileged --cgroupns=host --tmpfs /run --tmpfs /run/lock \
#     -v "$PWD/dist:/dist:ro" -v "$PWD/build/test:/t:ro" sre-systemd-test
#   docker exec sre-systemd-test sh /t/systemd.sh /dist/sre-agent_1.0.0-1_amd64.deb /dist/sre-agent_1.0.0.99-1_amd64.deb
set -u
PKG=$1
UPG=$2
fail() { echo "FAIL: $*" >&2; exit 1; }

DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$PKG" >/dev/null 2>&1 || fail install
[ "$(systemctl is-enabled sre-agent)" = enabled ] || fail "not enabled"
[ "$(systemctl is-active sre-agent)" != active ] || fail "must not start on first install"

systemctl start sre-agent; sleep 4
[ "$(systemctl is-active sre-agent)" = active ] || { journalctl -u sre-agent --no-pager | tail -20; fail start; }
curl -fs localhost:8000/health >/dev/null || fail health
KEY=$(sed -n "s/^SRE_API_KEY=\([^ ]*\).*/\1/p" /etc/sre-agent/sre-agent.env)
[ "$(curl -s -o /dev/null -w '%{http_code}' localhost:8000/signals)" = 401 ] || fail "auth"
[ "$(curl -s -o /dev/null -w '%{http_code}' -H "X-API-Key: $KEY" localhost:8000/signals)" = 200 ] || fail "key"
[ "$(ps -o user= -C python3 | head -1)" = sre-age+ ] || [ "$(ps -o user= -C python3 | head -1)" = sre-agent ] || fail "not running as sre-agent"

DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$UPG" >/dev/null 2>&1 || fail upgrade
sleep 4
[ "$(systemctl is-active sre-agent)" = active ] || fail "not restarted after upgrade"

apt-get remove -y -qq sre-agent >/dev/null 2>&1
[ "$(systemctl is-active sre-agent)" != active ] || fail "still running after remove"
echo PASS
