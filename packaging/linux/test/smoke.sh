#!/bin/sh
# Install -> configure -> doctor -> serve -> auth -> upgrade -> remove -> purge, inside a clean distro container.
#   sh smoke.sh /dist/<package> /dist/<newer package>
# Run all distros from the host with packaging/linux/test/run-smoke.sh.
set -eu

PKG=$1
UPG=$2
ENV_FILE=/etc/sre-agent/sre-agent.env
PY=/opt/sre-agent/python/bin/python3

step() { printf '\n==> %s\n' "$*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

case "$PKG" in
  *.deb) FMT=deb ;;
  *.rpm) FMT=rpm ;;
  *) fail "unknown package type: $PKG" ;;
esac

pkg_install() {
  if [ $FMT = deb ]; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$1" >/dev/null
  else
    dnf install -y -q "$1" >/dev/null
  fi
}

http() {  # http METHOD PATH [API_KEY] [JSON] -> prints "<status> <body>"
  "$PY" -I - "$@" <<'PYEOF'
import json, sys, urllib.request, urllib.error
method, path = sys.argv[1], sys.argv[2]
key = sys.argv[3] if len(sys.argv) > 3 else ""
body = sys.argv[4].encode() if len(sys.argv) > 4 else None
req = urllib.request.Request("http://127.0.0.1:8000" + path, data=body, method=method)
if key:
    req.add_header("X-API-Key", key)
if body:
    req.add_header("Content-Type", "application/json")
try:
    with urllib.request.urlopen(req, timeout=5) as r:
        print(r.status, r.read().decode())
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode())
PYEOF
}

start_server() {
  sre-agent serve >/tmp/serve.log 2>&1 &
  i=0
  until http GET /health 2>/dev/null | grep -q '^200'; do
    i=$((i + 1)); [ $i -lt 40 ] || { cat /tmp/serve.log; fail "server did not come up"; }
    sleep 0.5
  done
}

stop_server() {
  pkill -f "sre_agent serve" || true
  i=0
  while pgrep -f "sre_agent serve" >/dev/null; do
    i=$((i + 1)); [ $i -lt 20 ] || fail "server did not stop"; sleep 0.5
  done
}

step "prerequisites ($FMT)"
if [ $FMT = deb ]; then
  apt-get update -qq >/dev/null
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq procps >/dev/null
else
  dnf install -y -q procps-ng >/dev/null
fi

step "install $(basename "$PKG")"
pkg_install "$PKG"

step "layout, user, permissions"
getent passwd sre-agent >/dev/null || fail "user sre-agent missing"
[ -x /usr/bin/sre-agent ] || fail "launcher missing"
[ "$(stat -c '%a %U %G' $ENV_FILE)" = "640 root sre-agent" ] || fail "config perms: $(stat -c '%a %U %G' $ENV_FILE)"
[ "$(stat -c '%U' /var/lib/sre-agent)" = sre-agent ] || fail "SRE_HOME owner"
[ "$(stat -c '%a' /var/lib/sre-agent/.ssh)" = 700 ] || fail ".ssh perms"
grep -q __GENERATED__ $ENV_FILE && fail "secrets not generated"
API_KEY=$(sed -n 's/^SRE_API_KEY=\([^ ]*\).*/\1/p' $ENV_FILE)
[ ${#API_KEY} -eq 64 ] || fail "API key length ${#API_KEY}"
[ "$(sed -n 's/^APPROVAL_SECRET=\([^ ]*\).*/\1/p' $ENV_FILE)" != "$API_KEY" ] || fail "secrets must differ"
sre-agent version
[ "$(cat /opt/sre-agent/VERSION)" != "" ] || fail "VERSION file"

step "configure for a local-file test"
sed -i 's|^MONITOR_SOURCE=.*|MONITOR_SOURCE=local_file|' $ENV_FILE
sed -i 's|^MONITOR_LOG_PATH=.*|MONITOR_LOG_PATH=/var/lib/sre-agent/app.log|' $ENV_FILE
CONF_SUM=$(sha256sum $ENV_FILE | cut -d' ' -f1)

step "doctor"
sre-agent doctor --quick || fail "doctor --quick"
sre-agent doctor || true      # full check prints warnings (no LLM key / notifications in the test)
sre-agent paths

step "serve + API key"
start_server
http GET /health | grep -q '"ok":true' || fail "health"
http GET /signals | grep -q '^401' || fail "/signals without key must be 401"
http GET /signals wrong-key | grep -q '^401' || fail "/signals wrong key must be 401"
http GET /signals "$API_KEY" | grep -q '^200' || fail "/signals with key must be 200"
SID=$(http POST /signals "$API_KEY" '{"service":"smoke","severity":"HIGH","error":"boom"}' \
  | sed -n 's/.*"signal_id":"\([^"]*\)".*/\1/p')
[ -n "$SID" ] || fail "could not create a signal"
sre-agent serve >/tmp/serve2.log 2>&1 && fail "second instance must refuse" || true
grep -q "already running" /tmp/serve2.log || { cat /tmp/serve2.log; fail "lock message"; }
[ "$(stat -c '%U' /var/lib/sre-agent/starter.db)" = sre-agent ] || fail "DB must be owned by sre-agent"
[ -s /var/log/sre-agent/sre.log ] || fail "log file empty"
stop_server

step "upgrade to $(basename "$UPG")"
pkg_install "$UPG"
[ "$(cat /opt/sre-agent/VERSION)" != "" ] || fail "VERSION after upgrade"
echo "now $(cat /opt/sre-agent/VERSION)"
[ "$(sha256sum $ENV_FILE | cut -d' ' -f1)" = "$CONF_SUM" ] || fail "config changed by upgrade"
ls /var/lib/sre-agent/backups/starter.db.* >/dev/null 2>&1 || fail "no DB backup on upgrade"
start_server
http GET "/signals/$SID" "$API_KEY" | grep -q '^200' || fail "signal lost across upgrade"
stop_server

step "remove"
if [ $FMT = deb ]; then
  DEBIAN_FRONTEND=noninteractive apt-get remove -y -qq sre-agent >/dev/null
else
  dnf remove -y -q sre-agent >/dev/null
fi
[ ! -e /opt/sre-agent/python ] || fail "/opt/sre-agent not removed"
[ ! -e /usr/bin/sre-agent ] || fail "launcher not removed"
[ -f $ENV_FILE ] || fail "config must survive remove"
[ -f /var/lib/sre-agent/starter.db ] || fail "data must survive remove"

if [ $FMT = deb ]; then
  step "purge"
  DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq sre-agent >/dev/null
  [ ! -e /etc/sre-agent ] || fail "purge must delete /etc/sre-agent"
  getent passwd sre-agent >/dev/null && fail "purge must delete the user"
  [ -f /var/lib/sre-agent/starter.db ] || fail "data must survive purge"
fi

echo
echo "PASS"
