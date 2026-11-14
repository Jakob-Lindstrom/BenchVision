"""Phase (f) tests: Phase-based (Complex Steerable), Riesz, Optical Flow, rPPG, and comparison."""

from __future__ import annotations

import numpy as np
import pytest

from benchvision.cameras.manager import CameraManager
from benchvision.cameras.synthetic import SyntheticCameraBackend
from benchvision.core.config import AppSettings
from benchvision.jobs.manager import JobQueueManager
from benchvision.processing import PROCESSING_REGISTRY, JobParams, list_processing_methods
from benchvision.recording.recorder import RecordingService
from benchvision.storage.db import Database
from benchvision.storage.layout import DataLayout


@pytest.mark.parametrize(
    "method_key",
    [
        "eulerian",
        "riesz",
        "phase_steerable",
        "motion_extraction",
        "posy_rgb_trail",
        "posy_peak_hold",
        "optical_flow",
        "rppg",
    ],
)
def test_all_magnification_methods_recover_synthetic_frequency(method_key: str) -> None:
    target_hz = 2.4  # 144 BPM
    fps = 30.0
    n_frames = 150  # 5 seconds

    cam = SyntheticCameraBackend(
        oscillation_hz=target_hz,
        secondary_hz=0.3,
        amplitude_px=0.9,
        color_pulse_amp=5.0,
        noise_std=0.05,
        realtime=False,
    )
    cam.start(width=80, height=60, fps=int(fps))
    frames = np.stack([cam.get_frame().bgr for _ in range(n_frames)], axis=0)
    cam.stop()

    timestamps_s = np.arange(n_frames, dtype=np.float64) / fps
    params = JobParams(
        method=method_key,
        low_hz=1.4,
        high_hz=3.6,
        alpha=20.0,
        pyramid_levels=3,
        spatial_blur_sigma=1.0,
        filter_type="butterworth",
    )

    cls = PROCESSING_REGISTRY.get(method_key)
    out = cls().process(frames_bgr=frames, timestamps_s=timestamps_s, fps=fps, params=params)

    assert out.magnified_bgr.shape == frames.shape
    assert out.motion_energy.shape == (n_frames, 30, 40)  # half-resolution to save RAM
    assert pytest.approx(out.dominant_hz, abs=0.2) == target_hz
    assert pytest.approx(out.dominant_bpm, abs=12.0) == target_hz * 60.0


def test_method_comparison_on_same_clip(
    app_settings: AppSettings, data_layout: DataLayout
) -> None:
    methods = list_processing_methods()
    assert len(methods) == 8

    db = Database(data_layout.db_path)
    cam_mgr = CameraManager(app_settings, realtime_synthetic=False)
    rec = RecordingService(layout=data_layout, db=db, camera_manager=cam_mgr)
    clip = rec.record_clip(title="Comparison Clip", duration_s=4.0, width=80, height=60, fps=30)
    clip_id = clip["clip_id"]

    job_mgr = JobQueueManager(layout=data_layout, db=db, start_worker=False)
    for m in ("eulerian", "riesz", "phase_steerable"):
        job_mgr.submit_job(
            clip_id=clip_id,
            params=JobParams(method=m, low_hz=1.2, high_hz=3.0, alpha=20.0, pyramid_levels=3),
            run_async=False,
        )

    comp = job_mgr.compare_jobs(clip_id)
    assert comp["job_count"] == 3
    assert comp["frequency_spread_hz"] <= 0.25
    for item in comp["comparisons"]:
        assert pytest.approx(item["dominant_hz"], abs=0.25) == 2.0

