# BenchVision — Raspberry Pi 4 & 5 Optical Vibrometer & Motion Magnification Studio

**BenchVision** is a self-contained optical vibrometer, sub-pixel motion magnification platform, and creative/nature motion extraction studio built specifically for **Raspberry Pi 4 and Raspberry Pi 5** (64-bit Raspberry Pi OS Bookworm / Trixie).

It combines real-time motion visualization (Posy-style time-shifted frame cancellation and chrono-chromatic rainbow trails), quality-first lossless capture with locked sensor controls and nanosecond timestamps, and offline multi-scale video magnification with a full spectral diagnostic suite—accessible from any browser on your local network (`http://<pi-ip>:8000`).

---

## Key Features

### 1. Live Preview & Real-Time Motion Extraction
- **Live Posy Motion Extraction & Rainbow Trails:** Real-time frame-shift gray cancellation (`Subtle`, `Medium`, `Extreme`), **Posy RGB Chrono-Chromatic Trails** ($R=t, G=t-k, B=t-2k$), **Peak-Hold Motion Persistence**, and **Live Software Stroboscope** for aliasing rapid mechanical oscillations down to slow motion.
- **Live Schlieren BOS (`schlieren`), Slit-Scan (`slit_scan`, `slit_scan_h`) & Neuromorphic Event Cam (`dvs_events`):** Background-Oriented Schlieren contrast-amplified heat/airflow shimmer visualization against a captured tare frame (`Capture Tare`), vertical and horizontal live slit-scan time ribbons, and a simulated `+ON / -OFF` asynchronous log-intensity DVS event camera with decaying time-surfaces and `kEv/s` spike telemetry.
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
- **Clip Deep Inspector (`Deep Inspect` — Low-RAM Streaming):**
  - **1. Auto-Find Vibrations (DMD):** Automatically discovers hidden resonant frequencies, damping ratios, and in-phase vs. anti-phase spatial deflection maps with one-click magnification handoff.
  - **2. Airflow & Swirl Map (`Fluid LIC`):** Dense optical flow curl (`∇ × v`), divergence, and Line Integral Convolution flow streamlines for smoke, convection plumes, and airflow.
  - **3. Flex & Stress Map (`Optical Strain`):** Differentiates sub-pixel displacement fields into a 2D von Mises stress concentration heatmap and stretch vs. squeeze map.
  - **4. Light Flicker & Sound (`Rolling-Shutter Mic`):** Turns CMOS rolling-shutter row banding into super-Nyquist (`50 Hz – 12 kHz`) flicker/acoustic spectra with downloadable 16 kHz PCM `.WAV` audio.
- **Printable Diagnostic Reports, Motion Stills & Animated `.GIF` Export:** One-click engineering report generation with metadata, peak metrics, annotated heatmaps, and 4-panel spectral plots, plus a built-in **Motion Still Studio** (`🎨 Stills` in the Clip Library) that collapses any video into **Rainbow Motion Painting** (`chrono_paint`), **Stroboscopic Multi-Exposure** (`strobe_ghost`), **Crowd / Ghost Eraser** (`median_bg`), or **Long-Exposure Motion Glow** (`motion_trails`) PNGs.

---

## Quick Start (Raspberry Pi 4 & Raspberry Pi 5)

### Option A: Manual Setup (`venv` + `requirements.txt`)

On 64-bit Raspberry Pi OS (Bookworm / Trixie), create the virtual environment with `--system-site-packages` so the apt-installed `python3-picamera2` and `python3-libcamera` bindings are available inside the venv:

```bash
git clone https://github.com/Jakob-Lindstrom/BenchVision.git
cd BenchVision

# Install Raspberry Pi system dependencies (Picamera2, libcamera, FFmpeg, V4L2 utilities)
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

### Option B: Automated Installer (`scripts/install.sh`)

Run `./scripts/install.sh` to automatically install Raspberry Pi `apt` dependencies, create `.venv` with `--system-site-packages`, and register `benchvision.service` with `systemd`:

```bash
./scripts/install.sh
```

Once started, open the printed URL in your browser:
- **On the Pi directly:** `http://localhost:8000`
- **From any device on your local network (Headless Pi 4 / Pi 5):** `http://<pi-ip>:8000`

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

## Hardware & RAM Recommendations (Adaptive Pi 4 & Pi 5 Scaling)

BenchVision automatically inspects `/proc/device-tree/model` and `/proc/meminfo` on startup (`detect_hardware_profile()`) and selects one of four **Adaptive Performance Tiers** so a **Raspberry Pi 5 (8 GB RAM)** runs completely unthrottled at full HD while a **Raspberry Pi 4 (2 GB / 4 GB)** stays protected from out-of-memory (OOM) crashes:

| Hardware Target | RAM | Adaptive Tier | Default Processing Width | Pyramid Levels | Deep Inspector &amp; Motion Stills | Optical Flow Quiver & Side-by-Side | Dual-Camera Mode |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Raspberry Pi 5 (Recommended Target)** | **8 GB** | `ULTRA` | **1920 px (Full 1080p)** | **5 levels** | **`1920 px` Stills (80 frames), `224 px / 240f` DMD, `640 px` Flow/Strain, `1080 px` Flicker** | High-density `1/2` pyramid flow, `960 px` SBS panels | **Tier 3:** Unthrottled 1080p / 120 FPS |
| **Raspberry Pi 5 / Pi 4** | **4 GB / 8 GB** | `HIGH` | **1280 px (720p HD)** | **4 levels** | `1280 px` Stills (64f), `160 px / 160f` DMD, `480 px` Flow/Strain, `720 px` Flicker | High-density `1/2` flow (Pi 5), `640 px` SBS panels | **Tier 3 (Pi 5)** / **Tier 2 (Pi 4)** |
| **Raspberry Pi 4** | **4 GB** | `BALANCED` | **960 px** | **4 levels** | `800 px` Stills (48f), `128 px / 120f` DMD, `360 px` Flow/Strain, `480 px` Flicker | Balanced `1/3` flow, `640 px` SBS panels | **Tier 2:** Capped `640×480 @ 30 FPS` |
| **Raspberry Pi 4 (Minimum)** | **2 GB** | `CONSTRAINED` | **640 px (VGA)** | **4 levels** | `540 px` Stills (48f), `96 px / 90f` DMD, `240 px` Flow/Strain, `360 px` Flicker | Fast `1/4` single-level flow, `480 px` SBS panels | **Tier 2:** Capped `640×480 @ 30 FPS` |

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
├── LICENSE                         # MIT open-source license
├── CHANGELOG.md                    # Release changelog (parsed live by /api/system)
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
│   │   ├── updater.py              # GitHub Release update checker & safe rollback archive installer
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
│   │   ├── stills.py               # Motion Still Studio (chrono paint, strobe ghost, median eraser, glow)
│   │   ├── frontier.py             # Clip Deep Inspector (DMD vibration finder, airflow LIC, flex stress, flicker mic)
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
