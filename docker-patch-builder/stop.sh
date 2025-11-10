#!/usr/bin/env bash
set -euo pipefail
docker stop patch-builder >/dev/null 2>&1 || true
docker rm patch-builder >/dev/null 2>&1 || true
echo "Stopped."

