#!/usr/bin/env bash
# Idempotent Raspberry Pi one-line installer for BenchVision (Raspberry Pi OS 64-bit Bookworm / Trixie).
# Usage on a brand-new Pi:
#   curl -fsSL https://raw.githubusercontent.com/Jakob-Lindstrom/BenchVision/master/scripts/install.sh | bash
# Safe to re-run at any time. Never touches user data inside ./data.

set -euo pipefail

GITHUB_REPO="${BENCHVISION_GITHUB_REPO:-Jakob-Lindstrom/BenchVision}"
REPO_URL="https://github.com/${GITHUB_REPO}.git"
TARGET_USER="${SUDO_USER:-$(whoami)}"
TARGET_HOME="$(getent passwd "${TARGET_USER}" 2>/dev/null | cut -d: -f6 || echo "${HOME}")"
SERVICE_NAME="benchvision.service"

if [ "${EUID:-$(id -u)}" -eq 0 ]; then
  SUDO=""
else
  SUDO="sudo"
fi

# Detect whether invoked from inside an existing checkout or via curl | bash
LOCAL_REPO=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  CAND_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
  if [ -f "${CAND_DIR}/pyproject.toml" ]; then
    LOCAL_REPO="${CAND_DIR}"
  fi
fi

APP_DIR="${BENCHVISION_INSTALL_DIR:-${LOCAL_REPO:-${TARGET_HOME}/BenchVision}}"

echo "=== BenchVision Installer (${GITHUB_REPO}) ==="
echo "Install directory: ${APP_DIR}"
echo "Target user:       ${TARGET_USER}"

if command -v apt-get >/dev/null 2>&1; then
  echo "[1/6] Installing Raspberry Pi system dependencies via apt..."
  ${SUDO} apt-get update -qq
  ${SUDO} apt-get install -y --no-install-recommends \
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

if [ ! -f "${APP_DIR}/pyproject.toml" ]; then
  echo "[2/6] Cloning BenchVision into ${APP_DIR}..."
  if [ -d "${APP_DIR}/.git" ]; then
    git -C "${APP_DIR}" pull --ff-only || true
  else
    git clone "${REPO_URL}" "${APP_DIR}"
  fi
else
  echo "[2/6] Using existing BenchVision source in ${APP_DIR}."
fi

cd "${APP_DIR}"

echo "[3/6] Creating Python virtual environment with --system-site-packages..."
python3 -m venv --system-site-packages "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install --upgrade pip setuptools wheel
"${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"
"${APP_DIR}/.venv/bin/pip" install -e "${APP_DIR}" --no-build-isolation --no-deps

echo "[4/6] Ensuring data directory and video group permissions..."
mkdir -p "${APP_DIR}/data"
${SUDO} usermod -aG video "${TARGET_USER}" 2>/dev/null || true

SERVICE_TEMPLATE="${APP_DIR}/scripts/benchvision.service"
if command -v systemctl >/dev/null 2>&1 && [ -f "${SERVICE_TEMPLATE}" ]; then
  echo "[5/6] Installing and enabling systemd boot service (${SERVICE_NAME})..."
  TMP_UNIT="$(mktemp)"
  sed \
    -e "s|__USER__|${TARGET_USER}|g" \
    -e "s|__APP_DIR__|${APP_DIR}|g" \
    "${SERVICE_TEMPLATE}" > "${TMP_UNIT}"
  ${SUDO} cp "${TMP_UNIT}" "/etc/systemd/system/${SERVICE_NAME}"
  rm -f "${TMP_UNIT}"
  ${SUDO} systemctl daemon-reload
  ${SUDO} systemctl enable "${SERVICE_NAME}"
  ${SUDO} systemctl restart "${SERVICE_NAME}" || true

  echo "[6/6] Granting ${TARGET_USER} passwordless UI toggle permission for ${SERVICE_NAME}..."
  TMP_SUDO="$(mktemp)"
  cat > "${TMP_SUDO}" <<EOF
${TARGET_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl enable ${SERVICE_NAME}, /usr/bin/systemctl disable ${SERVICE_NAME}, /usr/bin/systemctl start ${SERVICE_NAME}, /usr/bin/systemctl stop ${SERVICE_NAME}, /usr/bin/systemctl restart ${SERVICE_NAME}, /usr/bin/systemctl daemon-reload, /bin/systemctl enable ${SERVICE_NAME}, /bin/systemctl disable ${SERVICE_NAME}, /bin/systemctl start ${SERVICE_NAME}, /bin/systemctl stop ${SERVICE_NAME}, /bin/systemctl restart ${SERVICE_NAME}, /bin/systemctl daemon-reload
EOF
  if ! command -v visudo >/dev/null 2>&1 || ${SUDO} visudo -cf "${TMP_SUDO}" >/dev/null 2>&1; then
    ${SUDO} install -m 0440 "${TMP_SUDO}" "/etc/sudoers.d/benchvision"
  fi
  rm -f "${TMP_SUDO}"
fi

IP_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'localhost')"
echo "=== BenchVision Installation Complete! ==="
echo "Auto-start on boot is ENABLED (toggle anytime in 5. System -> Auto-Start on Boot)."
echo "Open BenchVision in your browser: http://${IP_ADDR}:8000 (or http://localhost:8000 on the Pi)"
