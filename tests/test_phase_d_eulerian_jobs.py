"""Phase (d) tests: temporal filters, Linear Eulerian magnification, and background job queue."""

from __future__ import annotations

import hashlib

import numpy as np
import pytest

from benchvision.cameras.manager import CameraManager
from benchvision.core.config import AppSettings
from benchvision.jobs.manager import JobQueueManager
from benchvision.processing.base import JobParams
from benchvision.processing.filters import apply_temporal_bandpass, find_dominant_frequency
from benchvision.recording.recorder import RecordingService
from benchvision.storage.db import Database
from benchvision.storage.layout import DataLayout


@pytest.mark.parametrize("filter_type", ["ideal", "butterworth", "iir"])
def test_temporal_filters_isolate_target_frequency(filter_type: str) -> None:
    fps = 30.0
    t = np.arange(180, dtype=np.float32) / fps  # 6 seconds
    # Signal has slow 0.25 Hz breathing + 2.0 Hz pulse + 9.0 Hz high-freq vibration
    sig = (
        5.0 * np.sin(2.0 * np.pi * 0.25 * t)
        + 1.5 * np.sin(2.0 * np.pi * 2.0 * t)
        + 2.0 * np.sin(2.0 * np.pi * 9.0 * t)
    )
    filtered = apply_temporal_bandpass(sig, fps=fps, low_hz=1.2, high_hz=3.2, filter_type=filter_type)
    dom_hz, dom_bpm = find_dominant_frequency(filtered, fps=fps, low_hz=0.1, high_hz=12.0)
    assert pytest.approx(dom_hz, abs=0.2) == 2.0
    assert pytest.approx(dom_bpm, abs=12.0) == 120.0


def test_eulerian_magnification_and_job_queue(
    app_settings: AppSettings, data_layout: DataLayout
) -> None:
    db = Database(data_layout.db_path)
    cam_mgr = CameraManager(app_settings, realtime_synthetic=False)
    rec = RecordingService(layout=data_layout, db=db, camera_manager=cam_mgr)

    # Record 5 seconds @ 30 FPS (150 frames) of synthetic 2.0 Hz oscillator
    clip_meta = rec.record_clip(
        title="Synthetic 2Hz Reference",
        duration_s=5.0,
        width=96,
        height=72,
        fps=30,
    )
    clip_id = clip_meta["clip_id"]
    clip_dir = data_layout.clip_dir(clip_id)
    raw_hash_before = hashlib.sha256((clip_dir / "raw_frames.npz").read_bytes()).hexdigest()

    job_mgr = JobQueueManager(layout=data_layout, db=db, start_worker=False)
    params = JobParams(
        method="eulerian",
        preset_id="human_heart",
        low_hz=1.2,
        high_hz=3.0,
        alpha=20.0,
        pyramid_levels=3,
        filter_type="butterworth",
    )

    job = job_mgr.submit_job(clip_id=clip_id, params=params, run_async=False)
    assert job["status"] == "completed"
    assert job["progress"] == 1.0
    assert pytest.approx(job["result"]["dominant_hz"], abs=0.2) == 2.0
    assert pytest.approx(job["result"]["dominant_bpm"], abs=12.0) == 120.0

    # Verify original raw recording was never modified
    raw_hash_after = hashlib.sha256((clip_dir / "raw_frames.npz").read_bytes()).hexdigest()
    assert raw_hash_before == raw_hash_after

    res_dir = data_layout.job_result_dir(clip_id, job["job_id"])
    assert (res_dir / "magnified_share.mp4").exists()
    assert (res_dir / "comparison_sbs.mp4").exists()
    assert (res_dir / "heatmap.mp4").exists()
    assert (res_dir / "magnified_lossless.avi").exists()
    assert (res_dir / "signal.csv").exists()

    # Test job cancellation before execution
    queued_job = job_mgr.submit_job(clip_id=clip_id, params=params, run_async=True)
    cancelled = job_mgr.cancel_job(queued_job["job_id"])
    assert cancelled is not None
    assert cancelled["status"] == "cancelled"
