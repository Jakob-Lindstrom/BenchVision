"""Phase (c) tests: Nyquist validation, storage estimation, preview, and lossless recording."""

from __future__ import annotations

from pathlib import Path

import pytest

from benchvision.cameras.manager import CameraManager
from benchvision.core.config import AppSettings
from benchvision.recording.preview import LivePreviewService
from benchvision.recording.recorder import RecordingService, load_clip_frames_and_timestamps
from benchvision.recording.validators import (
    estimate_recording_storage,
    validate_nyquist_and_duration,
)
from benchvision.storage.db import Database
from benchvision.storage.layout import DataLayout


def test_nyquist_validation() -> None:
    ok_res = validate_nyquist_and_duration(fps=30.0, max_freq_hz=3.0, min_freq_hz=0.8, duration_s=10.0)
    assert ok_res["valid"] is True
    assert ok_res["nyquist_limit_hz"] == 15.0

    bad_res = validate_nyquist_and_duration(fps=15.0, max_freq_hz=10.0, min_freq_hz=2.0, duration_s=5.0)
    assert bad_res["valid"] is False
    assert any("Nyquist violation" in w for w in bad_res["warnings"])


def test_storage_estimation(temp_data_dir: Path) -> None:
    temp_data_dir.mkdir(parents=True, exist_ok=True)
    est = estimate_recording_storage(
        width=640,
        height=480,
        fps=30.0,
        duration_s=5.0,
        storage_format="mjpeg_q100",
        data_dir=temp_data_dir,
        roi=(100, 100, 200, 200),
    )
    assert est["effective_width"] == 200
    assert est["effective_height"] == 200
    assert est["total_frames"] == 150
    assert est["estimated_bytes"] > 0
    assert est["sufficient_space"] is True


def test_live_preview_and_lossless_recording(
    app_settings: AppSettings, data_layout: DataLayout
) -> None:
    db = Database(data_layout.db_path)
    cam_mgr = CameraManager(app_settings, realtime_synthetic=False)
    preview = LivePreviewService(cam_mgr)
    preview.set_roi((16, 16, 64, 48))

    jpg = preview.grab_preview_jpeg(draw_roi=True)
    assert jpg.startswith(b"\xff\xd8")
    assert len(preview.get_live_signal()) >= 1

    rec = RecordingService(layout=data_layout, db=db, camera_manager=cam_mgr, preview_service=preview)
    meta = rec.record_clip(
        title="Test Pulse Capture",
        duration_s=2.0,
        width=128,
        height=96,
        fps=30,
        storage_format="mjpeg_q100",
        roi=(16, 16, 64, 48),
        controls={"exposure_us": 8000, "gain": 2.0},
    )

    assert meta["width"] == 64
    assert meta["height"] == 48
    assert meta["frame_count"] == 60
    assert meta["controls"]["locked"] is True
    assert pytest.approx(meta["achieved_fps"], rel=1e-2) == 30.0

    clip_dir = data_layout.clip_dir(meta["clip_id"])
    assert (clip_dir / "timestamps.csv").exists()
    assert (clip_dir / "raw_frames.npz").exists()
    assert (clip_dir / "raw.avi").exists()
    assert (clip_dir / "thumb.jpg").exists()

    frames, t_sec, fs = load_clip_frames_and_timestamps(clip_dir)
    assert frames.shape == (60, 48, 64, 3)
    assert len(t_sec) == 60
    assert pytest.approx(fs, rel=1e-2) == 30.0

    listed = db.list_recordings()
    assert len(listed) == 1
    assert listed[0]["clip_id"] == meta["clip_id"]
