# BenchVision — Optical Vibrometer & Motion Magnification Studio

**BenchVision** is a self-contained optical vibrometer, sub-pixel motion magnification platform, and creative/nature motion extraction studio designed for **Raspberry Pi 4 / 5** and desktop workstations (Linux, macOS, and Windows).

It combines real-time motion visualization (Posy-style time-shifted frame cancellation and chrono-chromatic rainbow trails), quality-first lossless capture with locked sensor controls and nanosecond timestamps, and offline multi-scale video magnification with a full spectral diagnostic suite—all accessible from any browser on your local network.

---

## Key Features

### 1. Live Preview & Real-Time Motion Extraction
- **Live Posy Motion Extraction & Rainbow Trails:** Real-time frame-shift gray cancellation (`Subtle`, `Medium`, `Extreme`), **Posy RGB Chrono-Chromatic Trails** ($R=t, G=t-k, B=t-2k$), **Peak-Hold Motion Persistence**, and **Live Software Stroboscope** for aliasing rapid mechanical oscillations down to slow motion.
- **Optical Assist Overlays:** Real-time **Focus Peaking** (Laplacian edge highlight in neon green) and **Overexposure Zebras** ($\ge 250$ luma clipping warnings) to ensure sharp, unclipped edges before recording.
- **Dual-Camera Synchronized Capture:** Hardware-aware 3-tier gating for synchronized two-angle recording (`Camera A + Camera B` or `Side-by-Side`).

### 2. Quality-First Lossless Capture
- **Locked Camera Controls:** Locks auto-exposure, analog/digital gain, white balance, and autofocus prior to recording, clamping exposure time below the frame period so cameras never silently drop frames.
- **Pre-Roll Circular Buffer:** Continuous 3-second RAM ring buffer (`Save Last 3s + Record`) to capture transient mechanical impacts, wildlife movement, or intermittent faults before pressing record.
- **High-Speed ROI Crop & FPS Boost:** Hardware sensor crop (`320×240` / `640×480`) to unlock `60–120+ FPS` high-Nyquist vibration capture.
- **Lossless Storage & Telemetry:** Supports `MJPEG Q100`, lossless `FFV1 (.mkv)`, and uncompressed `NPZ` with per-frame monotonic nanosecond timestamps (`timestamps.csv`) and jitter diagnostics.

### 3. Offline Magnification & Motion Extraction Engines
- **Riesz Pyramid (`riesz`):** Fast 3-tap monogenic phase-shift pyramid optimized for Raspberry Pi CPU/NEON throughput.
- **Linear Eulerian (`eulerian`):** Spatial Laplacian pyramid with configurable temporal bandpass filters (`butterworth`, `ideal` FFT, or causal `iir`) and chromatic attenuation.
- **Phase-Based Complex Steerable (`phase_steerable`):** 4-orientation quadrature Gabor/steerable pyramid for low-noise structural vibration magnification (`0.5×` half-octave or `0.25×` quarter-octave).
- **Dense Optical Flow & Quiver Vectors (`optical_flow`):** Farnebäck sub-pixel displacement estimation with directional HSV color wheel and motion vector arrows.
- **Offline Posy Modes (`motion_extraction`, `posy_rgb_trail`, `posy_peak_hold`):** Temporal gray cancellation, RGB chrono-chromatic separation, and decaying peak-hold envelopes with optional frequency bandpass pre-filtering.
- **Remote Photoplethysmography (`rppg`):** Plane-Orthogonal-to-Skin (POS) and green-channel absorption extraction for non-contact pulse/respiration measurement.

### 4. Spectral Analysis & Engineering Diagnostics
- **Interactive Spectral Plots:** **Click-to-Tune FFT** (click any harmonic spike to auto-set the bandpass filter around that peak), **Welch PSD**, and **2D STFT Spectrogram** with cursor readouts in **Hz** and **BPM**.
- **Line-Scan Kymograph ($X\text{-}t$ / $Y\text{-}t$):** Extract a 1px spatial slice across all frames to visualize wave propagation, frequency drift, and sub-pixel displacement over time.
- **Dual-ROI Differential Phase Probes (A vs. B):** Compare two regions simultaneously to measure **Cross-Spectral Coherence** ($0\text{–}100\%$), **Phase Lag ($\Delta\phi^\circ$)**, and **In-Phase vs. Anti-Phase Resonance**.
- **2D Lissajous & 3D Isometric Motion Orbits:** Sub-pixel $(X(t), Y(t), \text{Time})$ trajectory visualization with 2-point physical ruler calibration ($\text{mm/px}$ and peak-to-peak $\mu\text{m}/\text{mm}$ displacement).
- **Vibration Sonification & `.WAV` Export:** Pitch-shifted audio synthesis (`1×` direct acoustic, `10×` structural, `30×` biological pulse) with real-time WebAudio playback and downloadable 16-bit PCM `.wav` files.
- **Printable Diagnostic Reports & Animated `.GIF` Export:** One-click engineering report generation with metadata, peak metrics, annotated heatmaps, and 4-panel spectral plots.

---

## Quick Start

### Raspberry Pi (Raspberry Pi OS Bookworm / Trixie)

On Raspberry Pi OS, create the virtual environment with `--system-site-packages` so the apt-installed `python3-picamera2` and `python3-libcamera` bindings are available inside the venv:

```bash
git clone https://github.com/your-org/BenchVision.git
cd BenchVision

# Install system dependencies (Picamera2, FFmpeg, V4L2 utilities)
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip python3-picamera2 python3-libcamera v4l-utils ffmpeg libgl1 libglib2.0-0

# Create virtual environment with access to system Picamera2 packages
python3 -m venv --system-site-packages .venv
source .venv/bin/activate

# Install Python runtime dependencies
pip install -r requirements.txt

# Launch BenchVision
python run.py
```

*(Alternatively, run `./scripts/install.sh` to automate apt package setup, `.venv` creation, and optional `systemd` background service registration.)*

### Linux, macOS, and Windows (Desktop / Laptop)

```bash
git clone https://github.com/your-org/BenchVision.git
cd BenchVision

python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

pip install -r requirements.txt
python run.py
```

Once started, open the printed URL in your browser:
- **Local Machine:** `http://localhost:8000`
- **Local Network (Headless Pi):** `http://<pi-ip>:8000`

### CLI Options (`run.py`)

```text
python run.py [--host 0.0.0.0] [--port 8000] [--data-dir ./data] [--reload]
```

| Flag | Default | Description |
| :--- | :--- | :--- |
| `--host` | `0.0.0.0` | Bind address (`0.0.0.0` exposes the UI across your local network) |
| `--port`, `-p` | `8000` | HTTP server port (automatically falls back to the next open port if busy) |
| `--data-dir` | `./data` | Self-contained directory for SQLite DB, recordings, jobs, and logs |
| `--reload` | `False` | Enable Uvicorn auto-reload for local development |

---

## Raspberry Pi Camera Setup (`Picamera2` & Global Shutter / V4L2)

- **Why `--system-site-packages` Matters on Bookworm/Trixie:** `Picamera2` depends on Raspberry Pi's compiled `libcamera` C++ bindings distributed via `apt` (`python3-picamera2` and `python3-libcamera`). Creating your virtual environment with `python3 -m venv --system-site-packages .venv` allows BenchVision to use the native CSI camera stack without compiling `libcamera` from source.
- **Global Shutter vs. Rolling Shutter:**
  - **Raspberry Pi Global Shutter Camera (Sony IMX296):** Strongly recommended for mechanical vibrometry and high-frequency analysis—exposes all pixels simultaneously, eliminating rolling-shutter geometric skew.
  - **Raspberry Pi Camera Module 2 / 3 & HQ Camera:** Supported out of the box via `Picamera2` with locked exposure time (`ExposureTime`), analog gain (`AnalogueGain`), and white balance (`ColourGains`).
  - **USB Webcams (V4L2):** Auto-detected on `/dev/video*`. BenchVision automatically switches V4L2 exposure to manual mode and clamps exposure time below $1/\text{FPS}$ so indoor lighting never throttles the capture frame rate.
- **No Camera Connected?** BenchVision includes a built-in **Synthetic Reference Camera** (`2.0 Hz / 120 BPM` primary oscillation + `0.35 Hz` secondary mode) and **One-Click Built-In Demo Clips** so you can explore every magnification and spectral tool immediately.

---

## Project Directory Structure

```text
BenchVision/
├── run.py                          # Clean root CLI launcher (--host, --port, --data-dir, --reload)
├── requirements.txt                # Runtime Python dependencies
├── pyproject.toml                  # Package metadata, Ruff linter, and Pytest configuration
├── src/                            # Main application package
│   ├── __init__.py                 # Package version and schema constants
│   ├── main.py                     # FastAPI application factory, lifespan state, and CLI runner
│   ├── analysis/
│   │   └── spectrum.py             # FFT, Welch PSD, STFT spectrogram, kymograph, dual-ROI coherence & orbits
│   ├── api/
│   │   └── routes.py               # REST endpoints, MJPEG live/playback streams, WAV/GIF export & demos
│   ├── cameras/
│   │   ├── base.py                 # Abstract camera interface, FramePacket, and capability models
│   │   ├── manager.py              # Camera discovery, active backend lifecycle, and dual-camera pairing
│   │   ├── picamera2_backend.py    # Raspberry Pi CSI camera backend (IMX296 Global Shutter, v2, v3, HQ)
│   │   ├── v4l2_usb.py             # Linux V4L2 / OpenCV USB webcam backend with manual exposure lock
│   │   └── synthetic.py            # Deterministic synthetic reference oscillator (single & dual-angle)
│   ├── core/
│   │   ├── config.py               # Pydantic settings, frequency presets, and config.json migration
│   │   ├── hardware.py             # Raspberry Pi 4/5 detection, thermal telemetry, and mm/px geometry
│   │   ├── logging.py              # Rotating structured logger inside <data_dir>/logs
│   │   └── registry.py             # Plugin registry for cameras and magnification algorithms
│   ├── jobs/
│   │   └── manager.py              # Background processing worker, job artifacts, and ROI re-analysis
│   ├── processing/
│   │   ├── base.py                 # Memory-bounded temporal chunking and spatial tiling helpers
│   │   ├── eulerian.py             # Linear Eulerian Laplacian pyramid magnification
│   │   ├── riesz.py                # Riesz pyramid monogenic phase magnification
│   │   ├── phase_steerable.py      # Multi-orientation complex steerable phase magnification
│   │   ├── optical_flow.py         # Dense Farnebäck optical flow warping & quiver vector overlays
│   │   ├── motion_extraction.py    # Posy frame-shift cancellation, RGB trails & peak-hold persistence
│   │   ├── rppg.py                 # Remote photoplethysmography (POS & green-channel pulse extraction)
│   │   └── filters.py              # Temporal Butterworth, ideal FFT, and IIR bandpass filters
│   ├── recording/
│   │   ├── preview.py              # Live MJPEG preview stream, Posy modes, stroboscope & focus/zebra overlays
│   │   ├── recorder.py             # Lossless capture loop, pre-roll ring buffer, dual-cam & external upload
│   │   └── validators.py           # Nyquist criterion, exposure-vs-FPS, and disk space validators
│   ├── storage/
│   │   ├── db.py                   # SQLite metadata store for recordings and processing jobs
│   │   └── layout.py               # Path-traversal-safe sandboxed directory layout
│   └── ui/
│       └── static/                 # Single-page web UI (index.html, app.js, styles.css)
├── scripts/
│   ├── install.sh                  # Idempotent Raspberry Pi setup & systemd installer
│   ├── update.sh                   # Safe updater with health check and automatic git rollback
│   └── benchvision.service         # systemd unit template
├── tests/                          # Automated unit & integration test suite (Pytest)
└── data/                           # Sandboxed runtime data directory (git-ignored)
```

---

## Running Tests & Linter

```bash
pytest
ruff check src tests run.py
```
