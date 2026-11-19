# Changelog

All notable releases of **BenchVision** are documented in this file. `GET /api/system` reads directly from this file so the UI changelog stays synchronized with the installed version.

## [1.0.1] - 2026-11-19
- **Persistent Boot-Service Status Banner & Inline `sudo` Prompt:** Added a dedicated `#service-status-banner` in `5. System` that stays visible during status refreshes and lets you authorize `benchvision.service` and `/etc/sudoers.d/benchvision` with your Raspberry Pi `sudo` password directly in the browser if `./scripts/install.sh` was skipped.
- **GitHub Release, Tag & Branch Update Detection + One-Click Verify:** Enhanced `check_github_release` to detect newer versions across GitHub Releases, Git tags, and the default branch (`master`/`main`), and added a **Re-Download & Test Updater** option in `5. System` so you can test the `.tar.gz` updater even when already on the latest version.

## [1.0.0] - 2026-11-19
- **Clip Deep Inspector (Low-RAM Streaming):** Added Auto-Find Vibrations (`DMD`), Airflow & Swirl Map (`Fluid LIC`), Flex & Stress Map (`Optical Strain`), and Light Flicker & Scanline Sound (`.WAV`) with plain-English summaries, PNG exports, and cancelable progress.
- **Live Neuromorphic, Schlieren & Slit-Scan Modes:** Added live `+ON / -OFF` DVS event camera emulation (`dvs_events`), Schlieren BOS heat/airflow shimmer (`schlieren`), and horizontal/vertical live slit-scan time ribbons.
- **Motion Still Studio:** Composite clips into Rainbow Motion Painting, Stroboscopic Multi-Exposure, Crowd/Ghost Eraser, or Long-Exposure Glow PNGs.
- **Full 4-Tier Pi 5 & Pi 4 Hardware Scaling:** Unlocked `ULTRA` resolution and depth budgets for Raspberry Pi 5 (8 GB RAM) across magnification, Motion Stills, and Deep Inspector while protecting 2 GB/4 GB Pi 4 boards via streaming frame loads and serialization locks.
- **GitHub Release Updater & One-Liner Fresh-Pi Service Installer:** Built-in `Jakob-Lindstrom/BenchVision` release checker & one-click `.tar.gz` updater in `5. System` and `scripts/update.sh`, plus a one-line `curl | bash` installer (`scripts/install.sh`) that installs `benchvision.service` to start on boot with an **Auto-Start on Boot** toggle inside `5. System`.
- **Timeline Clip Trimmer & Capture Hardening:** Added Android-style start/end spike trimming, inline Clip Rename & Notes in the Library, one-click High-FPS capture presets (`320×240 @ 120 FPS` up to `1080p FHD`), hands-off settling countdown, early stop during capture, SQLite WAL mode, Picamera2 colour-gain locking, and flattened `src/` + `run.py` open-source packaging.

## [0.7.0] - 2026-11-12
- Added optical Focus Peaking and Overexposure Zebra live overlays.
- Added One-Click Demo Showcase Clips, Interactive Split-Wipe comparison player, and looping `.GIF` export.
- Added ISO kinematic velocity RMS (mm/s), peak acceleration (g), AC mains lighting alias warnings, 2D/3D Motion Orbits, Vibration Sonification `.WAV` export, and synchronized Dual-Camera (`Cam A + B`) support.

## [0.1.0] - 2026-11-05
- Unified `CameraBackend` plugin architecture (Picamera2 CSI, USB V4L2, Synthetic Oscillator).
- Locked exposure/gain/white-balance/focus capture with monotonic nanosecond timestamps and Nyquist check.
- Offline magnification pipelines (`riesz`, `eulerian`, `phase_steerable`, `optical_flow`, `motion_extraction`, `rppg`) with FFT, Welch PSD, STFT Spectrogram, and Kymograph diagnostics.
