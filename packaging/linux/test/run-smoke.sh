#!/usr/bin/env bash
# Run packaging/linux/test/smoke.sh in clean containers for every supported distro.
#
#   packaging/linux/test/run-smoke.sh            # all distros, amd64
#   packaging/linux/test/run-smoke.sh ubuntu:24.04 rockylinux/rockylinux:9
#
# Needs dist/ to hold the current package and a newer one for the upgrade test. Build both with:
#   EXTRA_VERSION=<current>.99 packaging/build-linux.sh amd64
set -euo pipefail

cd "$(dirname "$0")/../../.."
export MSYS_NO_PATHCONV=1

ARCH=${ARCH:-amd64}
RPM_ARCH=$([ "$ARCH" = amd64 ] && echo x86_64 || echo aarch64)
VERSION=$(tr -d '\r' < sre_agent/__init__.py | sed -n 's/^__version__ = "\(.*\)"$/\1/p')
UPGRADE_VERSION=${UPGRADE_VERSION:-$VERSION.99}

DEB=dist/sre-agent_${VERSION}-1_${ARCH}.deb
DEB_UP=dist/sre-agent_${UPGRADE_VERSION}-1_${ARCH}.deb
RPM=dist/sre-agent-${VERSION}-1.${RPM_ARCH}.rpm
RPM_UP=dist/sre-agent-${UPGRADE_VERSION}-1.${RPM_ARCH}.rpm
for f in "$DEB" "$DEB_UP" "$RPM" "$RPM_UP"; do
  [ -f "$f" ] || { echo "missing $f (see header of this script)" >&2; exit 1; }
done

DISTROS=("$@")
if [ ${#DISTROS[@]} -eq 0 ]; then
  DISTROS=(ubuntu:22.04 ubuntu:24.04 debian:12 rockylinux/rockylinux:8 rockylinux/rockylinux:9 amazonlinux:2023)
fi

# LF copy of the test script (a Windows checkout may have CRLF), mounted read-only into each container.
mkdir -p build/test
tr -d '\r' < packaging/linux/test/smoke.sh > build/test/smoke.sh

declare -A RESULT
for image in "${DISTROS[@]}"; do
  case "$image" in
    ubuntu*|debian*) pkg=$DEB; up=$DEB_UP ;;
    *) pkg=$RPM; up=$RPM_UP ;;
  esac
  echo "################ $image"
  if docker run --rm --platform "linux/$ARCH" -v "$PWD/dist:/dist:ro" -v "$PWD/build/test:/t:ro" \
       "$image" sh /t/smoke.sh "/$pkg" "/$up"; then
    RESULT[$image]=PASS
  else
    RESULT[$image]=FAIL
  fi
done

echo
echo "================ summary ($ARCH)"
rc=0
for image in "${DISTROS[@]}"; do
  printf '%-28s %s\n' "$image" "${RESULT[$image]}"
  [ "${RESULT[$image]}" = PASS ] || rc=1
done
exit $rc
