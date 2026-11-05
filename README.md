# BenchVision — Raspberry Pi Video & Motion Magnification Platform

**BenchVision** is a modular offline video and motion magnification application designed to run directly on a **Raspberry Pi 4 / 5** (64-bit Raspberry Pi OS Debian 13 "Trixie", Python 3.11+). It captures lossless/near-lossless video with locked camera controls and nanosecond per-frame timestamps, then processes clips offline using multiple video magnification algorithms to reveal subtle vibrations and periodic motion (human or cat heartbeat, respiration, mechanical vibrations).

---

## Quick Start

1. **Flash Raspberry Pi OS** (64-bit Debian 13 "Trixie") and connect your USB webcam (e.g., Logitech C270) or Raspberry Pi CSI camera (Camera Module 2/3, HQ, or Global Shutter).
2. **Clone or extract BenchVision**:
   ```bash
   cd ~/BenchVision
   ```
3. **Run the installer** (installs apt packages including `python3-picamera2`, creates `.venv` with `--system-site-packages`, and registers `benchvision.service`):
   ```bash
   ./scripts/install.sh
   ```
   *(Or start directly in the foreground at any time with `python3 test.py`)*
4. **Open in any browser on your local network**:
   ```text
   http://<pi-ip>:8000
   ```

---

## Workflow: Record &rarr; Process &rarr; Review &amp; Compare

1. **Record (Quality-First Capture)**:
   - Auto-detects connected USB V4L2 webcams, Raspberry Pi CSI cameras (`Picamera2`), and a built-in **Synthetic Reference Oscillator** (`2.0 Hz / 120 BPM`).
   - Surfaces **rolling-shutter vs. global-shutter** advisories (recommending the IMX296 Global Shutter Camera for high-frequency vibration analysis).
   - **Locks exposure, gain, white balance, and focus** prior to capture (`lock_controls`) and clamps exposure time below the frame period so USB webcams never silently drop FPS.
   - Validates sampling rate against the **Nyquist criterion** ($f_s \ge 2 f_{\max}$), checks disk space, records per-frame nanosecond monotonic timestamps (`timestamps.csv`), and reports the **true achieved FPS** and jitter.
2. **Process (Swappable Offline Plugins)**:
   - **Linear Eulerian Video Magnification (`eulerian`)**: Spatial Laplacian pyramid + temporal bandpass (`butterworth`, `ideal` FFT, or causal `iir`).
   - **Riesz Pyramid Phase Magnification (`riesz`)**: Fast 3-tap monogenic phase shift designed for Raspberry Pi performance.
   - **Phase-Based Complex Steerable Pyramid (`phase_steerable`)**: 4-orientation complex quadrature Gabor/steerable phase magnification.
   - **Optical Flow Displacement (`optical_flow`)**: Measures actual sub-pixel motion in pixels over time via dense Farnebäck flow and warps frames.
   - **Remote Photoplethysmography (`rppg`)**: Plane-Orthogonal-to-Skin (POS) + green-channel absorption for skin pulse analysis.
3. **Analyze &amp; Compare**:
   - Interactive **FFT**, **Welch PSD**, and **2D STFT Spectrogram** plots, dominant frequency in **Hz &amp; BPM**, **Auto-Find Dominant Band**, motion-energy heatmaps, side-by-side comparison player, and **CSV signal export**.

---

## Data Sandbox &amp; Updates

- All user data lives strictly inside `./data` (or `BENCHVISION_DATA_DIR`): `config.json`, `benchvision.db`, `recordings/`, and `logs/`.
- Update safely with automatic health check and rollback:
  ```bash
  ./scripts/update.sh
  ```

---

## Running Tests &amp; Linter

```bash
.venv/bin/pytest
.venv/bin/ruff check src tests test.py
```

