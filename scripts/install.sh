#!/usr/bin/env bash
# Idempotent Raspberry Pi installer for BenchVision (Debian 13 "Trixie" 64-bit).
# Safe to re-run at any time. Never touches user data inside ./data.

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${APP_DIR}/.venv"
SERVICE_NAME="benchvision.service"
SERVICE_TEMPLATE="${APP_DIR}/scripts/benchvision.service"
TARGET_USER="${SUDO_USER:-$(whoami)}"

echo "=== BenchVision Installer ==="
echo "Application directory: ${APP_DIR}"
echo "Target user:           ${TARGET_USER}"

if command -v apt-get >/dev/null 2>&1; then
  echo "[1/4] Installing Raspberry Pi system dependencies via apt..."
  sudo apt-get update -qq
  sudo apt-get install -y --no-install-recommends \
    python3 \
    python3-venv \
    python3-pip \
    python3-picamera2 \
    python3-libcamera \
    v4l-utils \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    curl \
    git || true
fi

echo "[2/4] Creating Python virtual environment with --system-site-packages..."
python3 -m venv --system-site-packages "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --upgrade pip setuptools wheel
"${VENV_DIR}/bin/pip" install -e "${APP_DIR}"

echo "[3/4] Ensuring data directory exists (${APP_DIR}/data)..."
mkdir -p "${APP_DIR}/data"

if command -v systemctl >/dev/null 2>&1 && [ -f "${SERVICE_TEMPLATE}" ]; then
  echo "[4/4] Installing and enabling systemd service (${SERVICE_NAME})..."
  TMP_UNIT="$(mktemp)"
  sed \
    -e "s|__USER__|${TARGET_USER}|g" \
    -e "s|__APP_DIR__|${APP_DIR}|g" \
    "${SERVICE_TEMPLATE}" > "${TMP_UNIT}"
  sudo cp "${TMP_UNIT}" "/etc/systemd/system/${SERVICE_NAME}"
  rm -f "${TMP_UNIT}"
  sudo systemctl daemon-reload
  sudo systemctl enable "${SERVICE_NAME}"
  sudo systemctl restart "${SERVICE_NAME}" || true
fi

IP_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'localhost')"
echo "=== Installation Complete! ==="
echo "Open BenchVision in your browser: http://${IP_ADDR}:8000"

