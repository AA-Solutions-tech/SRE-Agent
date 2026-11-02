#!/usr/bin/env bash
# Build Linux packages with Docker from any host (Windows Git Bash, macOS, Linux).
#
#   packaging/build-linux.sh                 # amd64 .deb + .rpm into dist/
#   packaging/build-linux.sh arm64           # arm64 (needs Docker with QEMU/binfmt, default in Docker Desktop)
#   packaging/build-linux.sh lock            # regenerate packaging/common/requirements.lock
set -euo pipefail

cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1   # Git Bash: do not rewrite /work into C:/...

TARGET=${1:-amd64}
UV_VERSION=0.12.24
# Lock only releases at least ~2 weeks old (supply-chain buffer); move the date forward when refreshing the lock.
EXCLUDE_NEWER=${EXCLUDE_NEWER:-2026-10-12}

if [ "$TARGET" = lock ]; then
  docker run --rm -v "$PWD:/work" -w /work python:3.12-slim sh -c "
    pip install --quiet --disable-pip-version-check uv==$UV_VERSION &&
    uv pip compile pyproject.toml --universal --python-version 3.12 --generate-hashes \
      --exclude-newer $EXCLUDE_NEWER --no-header --output-file packaging/common/requirements.lock"
  echo "wrote packaging/common/requirements.lock"
  exit 0
fi

case "$TARGET" in
  amd64|arm64) ;;
  *) echo "usage: $0 [amd64|arm64|lock]" >&2; exit 1 ;;
esac

IMAGE=sre-agent-builder:$TARGET
# Dockerfile and build.sh are streamed in with CR stripped, so a Windows (CRLF) checkout still builds.
tr -d '\r' < packaging/linux/Dockerfile.build | docker build --platform "linux/$TARGET" -t "$IMAGE" -
tr -d '\r' < packaging/linux/build.sh | docker run --rm -i --platform "linux/$TARGET" -v "$PWD:/src" \
  -e ARCH="$TARGET" -e VERSION="${VERSION:-}" -e EXTRA_VERSION="${EXTRA_VERSION:-}" \
  "$IMAGE" sh -s
