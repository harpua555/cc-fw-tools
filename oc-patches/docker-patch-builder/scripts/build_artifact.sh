#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:-${FW_VER:-1.1.40}}"
WITH_APP="${2:-0}"
STAMP_MMDD="$(date +%m%d)"
echo "[build] Building artifact for firmware version: ${VERSION}"

# Ensure PATH includes sbin for tool discovery (mksquashfs/unsquashfs)
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH
echo "[build] PATH=$PATH"

# Move to repo root
cd "$(dirname "$0")/.."
ROOT_DIR="$PWD"

cd cc-fw-tools
chmod +x ./fwdl.sh ./build.sh || true

# Create wrapper shims in TOOLS unconditionally so check_tools can fall back even if PATH changes
mkdir -p TOOLS

# mksquashfs shim
BIN_MK=""
if [ -x /usr/sbin/mksquashfs ]; then BIN_MK=/usr/sbin/mksquashfs; fi
if [ -z "${BIN_MK}" ] && [ -x /usr/bin/mksquashfs ]; then BIN_MK=/usr/bin/mksquashfs; fi
if [ -n "${BIN_MK}" ]; then
  printf '#!/usr/bin/env bash\nexec "%s" "$@"\n' "$BIN_MK" > TOOLS/mksquashfs && chmod +x TOOLS/mksquashfs
  echo "[build] Shim ready: TOOLS/mksquashfs -> $BIN_MK"
else
  echo "[build][ERROR] mksquashfs not found in image" >&2
  exit 2
fi

# unsquashfs shim (used by unpack.sh)
BIN_UN=""
if [ -x /usr/sbin/unsquashfs ]; then BIN_UN=/usr/sbin/unsquashfs; fi
if [ -z "${BIN_UN}" ] && [ -x /usr/bin/unsquashfs ]; then BIN_UN=/usr/bin/unsquashfs; fi
if [ -n "${BIN_UN}" ]; then
  printf '#!/usr/bin/env bash\nexec "%s" "$@"\n' "$BIN_UN" > TOOLS/unsquashfs && chmod +x TOOLS/unsquashfs
  echo "[build] Shim ready: TOOLS/unsquashfs -> $BIN_UN"
else
  echo "[build][ERROR] unsquashfs not found in image" >&2
  exit 2
fi

echo "[build] Downloading firmware ${VERSION}..."
./fwdl.sh "${VERSION}"

echo "[build] Running build.sh ${VERSION}..."
FAKE_GIT_CREATED=0
if [ ! -d .git ]; then
  echo "[build] Preparing temporary git repo for set_firmware_version (tag v${VERSION})"
  git init >/dev/null 2>&1 || true
  git config user.email builder@example.invalid || true
  git config user.name builder || true
  git commit --allow-empty -m "temp" >/dev/null 2>&1 || true
  git tag -a "v${VERSION}" -m "v${VERSION}" >/dev/null 2>&1 || true
  FAKE_GIT_CREATED=1
fi

./build.sh "${VERSION}"

if [ "$FAKE_GIT_CREATED" = "1" ]; then
  echo "[build] Cleaning up temporary git repo"
  rm -rf .git || true
fi

echo "[build] Collecting artifact..."
mkdir -p "$ROOT_DIR/artifacts"
cp -f update/update.swu "$ROOT_DIR/artifacts/update.swu"
OUT_ZIP="artifacts/patched-app-custom-${VERSION}-${STAMP_MMDD}.zip"
echo "[build] Zipping firmware artifact to $OUT_ZIP ..."
zip -j -q "$ROOT_DIR/$OUT_ZIP" "$ROOT_DIR/artifacts/update.swu" || true
echo "[build] Artifact ready at $OUT_ZIP"

# Remove intermediate SWU to keep artifacts tidy and avoid permission issues
rm -f "$ROOT_DIR/artifacts/update.swu" || true

# Optionally export patched app binary
if [ "$WITH_APP" = "1" ] || [ "$WITH_APP" = "true" ]; then
  APP_PATH="unpacked/squashfs-root/app/app"
  if [ -f "$APP_PATH" ]; then
    cp -f "$APP_PATH" "$ROOT_DIR/artifacts/app"
    echo "[build] Patched app exported to artifacts/app"
  else
    echo "[build] WARNING: Patched app not found at $APP_PATH"
  fi
fi
