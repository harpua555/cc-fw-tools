#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:-${FW_VER:-1.1.40}}"
WITH_APP="${2:-0}"
STAMP_MMDD="$(date +%m%d)"
echo "[build] Building artifact for firmware version: ${VERSION}"

# Ensure PATH includes sbin for tool discovery (mksquashfs/unsquashfs)
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH
echo "[build] PATH=$PATH"

# Resolve directories
SCRIPT_ROOT="$(cd "$(dirname "$0")/.."; pwd)"
SESSION_DIR_IN="${SESSION_DIR:-$SCRIPT_ROOT}"
ART_DIR_IN="${ARTIFACTS_DIR:-$SCRIPT_ROOT/artifacts}"
# Normalize to absolute paths
case "$SESSION_DIR_IN" in 
  /*) SESSION_DIR="$SESSION_DIR_IN" ;;
  *)  SESSION_DIR="$SCRIPT_ROOT/${SESSION_DIR_IN#./}" ;;
esac
case "$ART_DIR_IN" in 
  /*) ART_DIR="$ART_DIR_IN" ;;
  *)  ART_DIR="$SCRIPT_ROOT/${ART_DIR_IN#./}" ;;
esac
echo "[build] SESSION_DIR=$SESSION_DIR"
echo "[build] ART_DIR=$ART_DIR"
mkdir -p "$ART_DIR"

# Work inside the session's cc-fw-tools checkout
cd "$SESSION_DIR/cc-fw-tools"
echo "[build] CWD=$(pwd)"
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
TEMP_TAG_CREATED=0
# Ensure tag v${VERSION} is available; if missing, create a temporary tag on HEAD
if [ -d .git ]; then
  if ! git rev-parse -q --verify "refs/tags/v${VERSION}" >/dev/null 2>&1; then
    echo "[build] Tag v${VERSION} not found; creating temporary tag on HEAD for set_firmware_version"
    git tag -a "v${VERSION}" -m "temp-tag v${VERSION} (session)" >/dev/null 2>&1 || true
    TEMP_TAG_CREATED=1
  fi
fi

./build.sh "${VERSION}"

echo "[build] Collecting artifact..."
tmp_swu="$ART_DIR/update.swu"
ls -lh update/update.swu || true
mkdir -p "$ART_DIR" || true
cp -f update/update.swu "$tmp_swu"
OUT_ZIP="$ART_DIR/patched-app-custom-${VERSION}-${STAMP_MMDD}.zip"
echo "[build] Zipping firmware artifact to $OUT_ZIP ..."
zip -j -q "$OUT_ZIP" "$tmp_swu" || true
REL_ZIP="artifacts/${OUT_ZIP#*/artifacts/}"
echo "[build] Artifact ready at $REL_ZIP"

# Remove intermediate SWU to keep artifacts tidy and avoid permission issues
rm -f "$tmp_swu" || true

# Optionally export patched app binary
if [ "$WITH_APP" = "1" ] || [ "$WITH_APP" = "true" ]; then
  APP_PATH="unpacked/squashfs-root/app/app"
  if [ -f "$APP_PATH" ]; then
    cp -f "$APP_PATH" "$ART_DIR/app"
    REL_APP_DIR="artifacts/${ART_DIR#*/artifacts/}"
    echo "[build] Patched app exported to ${REL_APP_DIR}/app"
  else
    echo "[build] WARNING: Patched app not found at $APP_PATH"
  fi
fi
# Clean up temporary tag if we created one
if [ "${TEMP_TAG_CREATED}" = "1" ]; then
  git tag -d "v${VERSION}" >/dev/null 2>&1 || true
fi
