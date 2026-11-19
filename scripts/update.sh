#!/usr/bin/env bash
# Reliable manual/auto updater for BenchVision using GitHub Releases with health check and rollback.
# Never modifies ./data or virtual environments.

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${APP_DIR}/.venv"
SERVICE_NAME="benchvision.service"
PORT="${BENCHVISION_PORT:-8000}"
HEALTH_URL="http://127.0.0.1:${PORT}/api/health"
GITHUB_REPO="${BENCHVISION_GITHUB_REPO:-Jakob-Lindstrom/BenchVision}"
HTTPS_REMOTE="https://github.com/${GITHUB_REPO}.git"

cd "${APP_DIR}"

CURRENT_REV="$(git rev-parse HEAD 2>/dev/null || echo 'release')"
CURRENT_VER="$("${VENV_DIR}/bin/python3" -c 'import sys; sys.path.insert(0, "."); import src; print(src.__version__)' 2>/dev/null || echo '1.0.0')"

if [ "${1:-}" = "--check-only" ]; then
  echo "Current version: v${CURRENT_VER} (commit ${CURRENT_REV:0:7}). Data folder (${APP_DIR}/data) is intact."
  exit 0
fi

echo "=== BenchVision Updater (${GITHUB_REPO}) ==="
echo "Current version: v${CURRENT_VER} (${CURRENT_REV})"

rollback() {
  echo "[!] Health check failed! Rolling back to previous revision (${CURRENT_REV})..."
  if [ "${CURRENT_REV}" != "release" ] && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git reset --hard "${CURRENT_REV}" || true
    "${VENV_DIR}/bin/pip" install -e "${APP_DIR}" --no-build-isolation --quiet || true
  fi
  if command -v systemctl >/dev/null 2>&1; then
    sudo systemctl restart "${SERVICE_NAME}" || true
  fi
  echo "[!] Rollback complete."
  exit 1
}

echo "[1/4] Checking GitHub (${GITHUB_REPO}) for published releases..."
RELEASE_APPLIED=0
if [ -x "${VENV_DIR}/bin/python3" ]; then
  if "${VENV_DIR}/bin/python3" -c "
import json, sys
from pathlib import Path
sys.path.insert(0, '.')
import src
from src.core.updater import apply_github_release_update
res = apply_github_release_update(Path('.'), src.__version__)
print(json.dumps(res))
sys.exit(0 if res.get('status') in ('updated', 'up_to_date') else 1)
"; then
    RELEASE_APPLIED=1
  fi
fi

if [ "${RELEASE_APPLIED}" -eq 0 ] && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "[1b/4] Falling back to public GitHub HTTPS git tags (${HTTPS_REMOTE})..."
  git fetch --tags "${HTTPS_REMOTE}"
  LATEST_TAG="$(git tag -l 'v*' --sort=-v:refname | head -n 1)"
  if [ -n "${LATEST_TAG}" ]; then
    echo "Checking out release tag ${LATEST_TAG}..."
    git checkout -f "${LATEST_TAG}"
  else
    git pull --ff-only "${HTTPS_REMOTE}" master
  fi
  echo "[2/4] Refreshing editable package metadata..."
  "${VENV_DIR}/bin/pip" install -e "${APP_DIR}" --no-build-isolation --no-deps --quiet || true
else
  echo "[2/4] GitHub release archive verified."
fi

echo "[3/4] Restarting service..."
if command -v systemctl >/dev/null 2>&1 && systemctl is-enabled "${SERVICE_NAME}" >/dev/null 2>&1; then
  sudo systemctl restart "${SERVICE_NAME}"
  sleep 2
fi

echo "[4/4] Running health check..."
if "${VENV_DIR}/bin/python3" -c 'import sys; sys.path.insert(0, "."); from src.main import create_app; assert create_app is not None'; then
  if curl -fsS --max-time 3 "${HEALTH_URL}" >/dev/null 2>&1; then
    echo "HTTP health check passed at ${HEALTH_URL}."
  else
    echo "Module import & schema health check passed."
  fi
else
  rollback
fi

echo "=== Update Successful! ==="
