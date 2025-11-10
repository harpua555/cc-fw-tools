#!/usr/bin/env bash
set -euo pipefail

IMAGE="${IMAGE:-patch-builder:latest}"
OUTDIR="${HOME}/OpenCentauri/outputs"
mkdir -p "$OUTDIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "Please install Docker first: https://docs.docker.com/engine/install/"
  exit 1
fi

echo "Pulling ${IMAGE} (if available)..."
docker pull "${IMAGE}" >/dev/null 2>&1 || true

echo "Stopping previous container if running..."
docker rm -f patch-builder >/dev/null 2>&1 || true

echo "Starting on http://localhost:8080 (with privileges for bootlogo patch)"
docker run -d --name patch-builder \
  --privileged \
  -p 8080:8080 \
  -v "${OUTDIR}:/app/artifacts" \
  "${IMAGE}"

if command -v xdg-open >/dev/null; then
  xdg-open http://localhost:8080 >/dev/null 2>&1 || true
elif command -v open >/dev/null; then
  open http://localhost:8080 >/dev/null 2>&1 || true
fi

echo "Running. Use ./stop.sh to stop the container."
