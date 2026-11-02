#!/bin/sh
# Build the .deb and .rpm for one architecture. Runs INSIDE the builder container (see packaging/build-linux.sh)
# with the repo mounted at /src. Works on a copy in the container filesystem (fast, and line endings are
# normalised to LF so a Windows checkout still produces working install scripts).
# Output: /src/dist/sre-agent_<ver>-1_<arch>.deb and /src/dist/sre-agent-<ver>-1.<rpm arch>.rpm
#
#   ARCH=amd64|arm64      must match the container platform (the bundled Python runs pip during the build)
#   VERSION=x.y.z         default: sre_agent/__init__.py
#   EXTRA_VERSION=x.y.z   also package the same build under this version (upgrade tests)
set -eu

ARCH=${ARCH:-amd64}
PBS_RELEASE=20261003
PBS_PY=3.12.15
case "$ARCH" in
  amd64) PBS_ARCH=x86_64;  PBS_SHA=f937814031eab4698ca6d07ec606ede1825768f3f3e99af76d9db3900bee03c5 ;;
  arm64) PBS_ARCH=aarch64; PBS_SHA=95c01982c9fcb9d95b0acfdb5eb8a6e0099dd11edf062314a474228d93f2b765 ;;
  *) echo "ARCH must be amd64 or arm64" >&2; exit 1 ;;
esac

SRC=/src
WORK=/build
CACHE=$SRC/build/cache

echo "==> copy sources"
rm -rf "$WORK" && mkdir -p "$WORK" "$CACHE" "$SRC/dist"
tar -C "$SRC" -cf - pyproject.toml README.md sre_agent packaging \
  --exclude='__pycache__' --exclude='*.pyc' | tar -C "$WORK" -xf -
find "$WORK/packaging" -type f ! -name '*.lock' -exec sed -i 's/\r$//' {} +
cd "$WORK"

VERSION=${VERSION:-$(sed -n 's/^__version__ = "\(.*\)"\r*$/\1/p' sre_agent/__init__.py)}
[ -n "$VERSION" ] || { echo "cannot read version" >&2; exit 1; }
export ARCH

APP=build/stage/opt/sre-agent
PY=$APP/python/bin/python3
export PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore
PIP="$PY -m pip"

echo "==> runtime: CPython $PBS_PY ($PBS_RELEASE) $PBS_ARCH"
mkdir -p "$APP"
TARBALL="$CACHE/cpython-$PBS_PY+$PBS_RELEASE-$PBS_ARCH-unknown-linux-gnu-install_only.tar.gz"
if [ ! -f "$TARBALL" ]; then
  curl -fsSL -o "$TARBALL.part" \
    "https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_RELEASE/$(basename "$TARBALL" | sed 's/+/%2B/')"
  mv "$TARBALL.part" "$TARBALL"
fi
echo "$PBS_SHA  $TARBALL" | sha256sum -c -
tar -xzf "$TARBALL" -C "$APP"            # -> $APP/python/

echo "==> dependencies (hash-pinned, binary wheels only)"
$PIP install --quiet --no-compile --require-hashes --only-binary=:all: -r packaging/common/requirements.lock

echo "==> sre-agent $VERSION"
$PIP wheel --quiet --no-deps -w build/wheel .
$PIP install --quiet --no-compile --no-deps build/wheel/*.whl

echo "==> trim"
LIB="$APP/python/lib/python3.12"
rm -rf "$LIB/test" "$LIB/idlelib" "$LIB/tkinter" "$LIB/turtledemo" "$LIB/ensurepip" "$LIB/lib2to3"
rm -rf "$APP/python/share" "$APP/python/include"
# console scripts written by pip carry a build-path shebang; the launcher only uses python3 -m sre_agent
find "$APP/python/bin" -type f ! -name 'python3*' ! -name 'python' -delete
find "$APP" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$APP" -name '*.so*' -type f -exec strip --strip-unneeded {} + 2>/dev/null || true

echo "==> byte-compile (/opt is read-only for the service)"
"$PY" -m compileall -q -j 0 -s build/stage -p / "$APP/python/lib" >/dev/null
"$PY" -I -m sre_agent version

package() {  # package <version>   (written locally first, then copied: bind mounts on Windows are flaky)
  echo "$1" > "$APP/VERSION"
  VERSION=$1 nfpm pkg -f packaging/linux/nfpm.yaml -p deb -t build/dist/
  VERSION=$1 nfpm pkg -f packaging/linux/nfpm.yaml -p rpm -t build/dist/
}

echo "==> packages"
mkdir -p build/dist
package "$VERSION"
[ -z "${EXTRA_VERSION:-}" ] || package "$EXTRA_VERSION"
cp build/dist/* "$SRC/dist/"
ls -lh "$SRC/dist/"
