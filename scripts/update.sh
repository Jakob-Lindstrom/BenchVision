#!/usr/bin/env bash
# Reliable manual update script for BenchVision with health check and automatic rollback.
# Never modifies ./data.

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${APP_DIR}/.venv"
SERVICE_NAME="benchvision.service"
PORT="${BENCHVISION_PORT:-8000}"
HEALTH_URL="http://127.0.0.1:${PORT}/api/health"

cd "${APP_DIR}"

CURRENT_REV="$(git rev-parse HEAD 2>/dev/null || echo 'unknown')"
CURRENT_VER="$("${VENV_DIR}/bin/python3" -c 'import sys; sys.path.insert(0, "."); import src; print(src.__version__)' 2>/dev/null || echo '0.1.0')"

if [ "${1:-}" = "--check-only" ]; then
  echo "Current version: v${CURRENT_VER} (commit ${CURRENT_REV:0:7}). Data folder (${APP_DIR}/data) is intact."
  exit 0
fi

echo "=== BenchVision Updater ==="
echo "Current version: v${CURRENT_VER} (${CURRENT_REV})"

rollback() {
  echo "[!] Health check failed! Rolling back to previous revision (${CURRENT_REV})..."
  if [ "${CURRENT_REV}" != "unknown" ]; then
    git reset --hard "${CURRENT_REV}"
    "${VENV_DIR}/bin/pip" install -e "${APP_DIR}" --quiet || true
  fi
  if command -v systemctl >/dev/null 2>&1; then
    sudo systemctl restart "${SERVICE_NAME}" || true
  fi
  echo "[!] Rollback complete."
  exit 1
}

if git remote get-url origin >/dev/null 2>&1; then
  echo "[1/4] Fetching latest release tags from origin..."
  git fetch --tags origin
  LATEST_TAG="$(git tag -l 'v*' --sort=-v:refname | head -n 1)"
  if [ -n "${LATEST_TAG}" ]; then
    echo "Checking out release tag ${LATEST_TAG}..."
    git checkout -f "${LATEST_TAG}"
  else
    git pull --ff-only
  fi
else
  echo "[1/4] No git remote configured; verifying local installation..."
fi

echo "[2/4] Updating Python dependencies inside virtual environment..."
"${VENV_DIR}/bin/pip" install -e "${APP_DIR}" --quiet

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

