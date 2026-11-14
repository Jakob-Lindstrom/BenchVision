"""Phase (e) tests: FFT, Welch PSD, STFT spectrogram, and Auto-Find dominant frequency band."""

from __future__ import annotations

import numpy as np
import pytest

from benchvision.analysis.spectrum import compute_roi_spectrum_analysis, compute_signal_spectrum
from benchvision.cameras.synthetic import SyntheticCameraBackend


def test_signal_spectrum_welch_and_spectrogram() -> None:
    fps = 30.0
    n = 180
    t = np.arange(n, dtype=np.float64) / fps
    sig = 2.5 * np.sin(2.0 * np.pi * 2.6 * t) + 0.1 * np.cos(2.0 * np.pi * 7.0 * t)

    res = compute_signal_spectrum(
        signal_1d=sig,
        filtered_1d=sig,
        timestamps_s=t,
        fps=fps,
        low_hz=1.5,
        high_hz=4.0,
    )
    assert pytest.approx(res["dominant_hz"], abs=0.2) == 2.6
    assert pytest.approx(res["dominant_bpm"], abs=12.0) == 156.0
    assert res["snr_db"] > 10.0
    assert len(res["welch_freqs_hz"]) > 4
    assert len(res["spectrogram"]["times_s"]) >= 2
    assert res["suggested_band"]["low_hz"] < 2.6 < res["suggested_band"]["high_hz"]


def test_auto_find_band_from_synthetic_video() -> None:
    cam = SyntheticCameraBackend(oscillation_hz=3.0, amplitude_px=0.9, realtime=False)
    cam.start(width=96, height=72, fps=30)
    frames = np.stack([cam.get_frame().bgr for _ in range(150)], axis=0)
    cam.stop()

    analysis = compute_roi_spectrum_analysis(frames_bgr=frames, fps=30.0, low_hz=0.5, high_hz=10.0)
    sug = analysis["suggested_band"]
    assert pytest.approx(sug["peak_hz"], abs=0.2) == 3.0
    assert pytest.approx(sug["peak_bpm"], abs=12.0) == 180.0
    assert sug["low_hz"] < 3.0 < sug["high_hz"]


def test_frame0_transient_spike_suppression() -> None:
    from benchvision.processing.filters import apply_temporal_bandpass

    fps = 30.0
    n = 180
    t = np.arange(n, dtype=np.float64) / fps
    sig = 1.0 * np.sin(2.0 * np.pi * 0.4 * t)
    # Inject a 25x Frame-0/Frame-1 exposure jump
    sig[0] = 25.0
    sig[1] = -18.0
    sig[2] = 12.0

    filtered = apply_temporal_bandpass(sig, fps=fps, low_hz=0.15, high_hz=1.0).astype(np.float64)
    res = compute_signal_spectrum(
        signal_1d=sig,
        filtered_1d=filtered,
        timestamps_s=t,
        fps=fps,
        low_hz=0.15,
        high_hz=1.0,
    )
    raw_ts = np.asarray(res["time_series"]["raw"])
    filt_ts = np.asarray(res["time_series"]["filtered"])
    # Ensure the initial 3 frames no longer spike above the steady-state oscillation amplitude
    assert np.max(np.abs(raw_ts[:4])) <= 1.25
    assert np.max(np.abs(filt_ts[:4])) <= 1.25

    # Verify high-overlap zero-padded STFT spectrogram has fine time/frequency resolution and no t=0 wall
    n240 = 240
    t240 = np.arange(n240, dtype=np.float64) / fps
    sig240 = 1.0 * np.sin(2.0 * np.pi * 0.4 * t240)
    sig240[0] = 50.0
    sig240[1] = -35.0
    res240 = compute_signal_spectrum(
        signal_1d=sig240,
        filtered_1d=sig240,
        timestamps_s=t240,
        fps=fps,
        low_hz=0.15,
        high_hz=1.0,
    )
    spec_db = np.asarray(res240["spectrogram"]["power_db"], dtype=np.float64)
    assert spec_db.shape[0] >= 129  # zero-padded nfft >= 256 -> >= 129 rfft bins
    assert spec_db.shape[1] >= 35  # 90% overlap yields >= 35 time columns on 240 frames
    # Column 0 broadband median must not exceed Column 1 broadband median + 1.6 dB
    assert float(np.median(spec_db[:, 0])) <= float(np.median(spec_db[:, 1])) + 1.6



