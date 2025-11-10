#!/usr/bin/env bash
set -euo pipefail

echo "[start] Starting patch-builder web service..."

# Ensure PATH includes sbin dirs for non-interactive shells
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH

# Prepare runtime directories
mkdir -p /app/sessions /app/cache/mirrors /app/artifacts /app/usage || true

# Ensure git-lfs is ready, and fetch any LFS objects
git lfs install || true
if [ -d "cc-fw-tools/.git" ]; then
  git -C cc-fw-tools lfs pull || true
fi

echo "[start] Launching gunicorn on 0.0.0.0:8080 (timeout=600)"
exec gunicorn \
  -b 0.0.0.0:8080 \
  --workers 1 \
  --threads 2 \
  --timeout 600 \
  --graceful-timeout 120 \
  --keep-alive 5 \
  --log-level info \
  app:app
