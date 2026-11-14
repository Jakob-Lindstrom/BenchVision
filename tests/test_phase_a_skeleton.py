"""Phase (a) tests: project skeleton, config migration, storage sandbox, and synthetic camera."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from src import SCHEMA_VERSION
from src.cameras import CAMERA_REGISTRY
from src.cameras.synthetic import SyntheticCameraBackend
from src.core.config import load_or_create_settings
from src.core.logging import configure_logging, read_recent_logs
from src.storage.layout import DataLayout


def test_config_creation_and_migration(temp_data_dir: Path) -> None:
    temp_data_dir.mkdir(parents=True, exist_ok=True)
    legacy_cfg = {
        "schema_version": 1,
        "port": 8123,
        "active_camera_id": "synthetic:0",
    }
    (temp_data_dir / "config.json").write_text(json.dumps(legacy_cfg), encoding="utf-8")

    settings = load_or_create_settings(temp_data_dir)
    assert settings.schema_version == SCHEMA_VERSION
    assert settings.port == 8123
    assert len(settings.presets) >= 4
    saved = json.loads((temp_data_dir / "config.json").read_text(encoding="utf-8"))
    assert saved["schema_version"] == SCHEMA_VERSION


def test_data_layout_prevents_path_traversal(data_layout: DataLayout) -> None:
    assert data_layout.recordings_dir.exists()
    assert data_layout.logs_dir.exists()
    with pytest.raises(ValueError, match="escapes data directory"):
        data_layout.resolve_safe_path("../../etc/passwd")


def test_structured_logging_writes_inside_data_dir(temp_data_dir: Path) -> None:
    import logging

    log_path = configure_logging(temp_data_dir, level="INFO")
    logger = logging.getLogger("benchvision.test")
    logger.info("Phase A structured log check")

    assert log_path.exists()
    entries = read_recent_logs(temp_data_dir)
    assert any("Phase A structured log check" in e["message"] for e in entries)


def test_camera_registry_and_synthetic_oscillation(synthetic_cam: SyntheticCameraBackend) -> None:
    cls = CAMERA_REGISTRY.get("synthetic")
    assert cls is SyntheticCameraBackend

    caps = synthetic_cam.list_capabilities()
    assert caps.shutter_type == "global"
    assert (640, 480) in caps.resolutions

    # Lock controls and ensure exposure is clamped below 1/30s (30000 us)
    locked = synthetic_cam.lock_controls({"fps": 30, "exposure_us": 90000})
    assert locked.locked is True
    assert locked.auto_exposure is False
    assert locked.auto_wb is False
    assert locked.autofocus is False
    assert locked.exposure_us <= 30000

    # Capture 150 frames (5 seconds @ 30 FPS) and verify 2.0 Hz oscillation peak
    n_frames = 150
    roi_means = []
    timestamps_ns = []
    for _ in range(n_frames):
        pkt = synthetic_cam.get_frame()
        timestamps_ns.append(pkt.timestamp_monotonic_ns)
        h, w, _ = pkt.bgr.shape
        roi = pkt.bgr[h // 4 : 3 * h // 4, w // 4 : 3 * w // 4, 1]  # green channel
        roi_means.append(float(np.mean(roi)))

    dt_s = np.diff(timestamps_ns) / 1e9
    achieved_fps = 1.0 / float(np.mean(dt_s))
    assert pytest.approx(achieved_fps, rel=1e-2) == 30.0

    sig = np.array(roi_means) - np.mean(roi_means)
    freqs = np.fft.rfftfreq(n_frames, d=1.0 / achieved_fps)
    spectrum = np.abs(np.fft.rfft(sig))
    peak_hz = float(freqs[int(np.argmax(spectrum[1:])) + 1])
    assert pytest.approx(peak_hz, abs=0.2) == 2.0

