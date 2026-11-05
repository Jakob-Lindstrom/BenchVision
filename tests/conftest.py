"""Pytest fixtures for isolated data directories and synthetic oscillating video clips."""

from __future__ import annotations

from pathlib import Path

import pytest

from benchvision.cameras.synthetic import SyntheticCameraBackend
from benchvision.core.config import AppSettings, load_or_create_settings
from benchvision.storage.layout import DataLayout


@pytest.fixture()
def temp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide an isolated temporary data directory for each test."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("BENCHVISION_DATA_DIR", str(data_dir))
    return data_dir


@pytest.fixture()
def app_settings(temp_data_dir: Path) -> AppSettings:
    """Provide initialized AppSettings inside the temporary data directory."""
    return load_or_create_settings(temp_data_dir)


@pytest.fixture()
def data_layout(temp_data_dir: Path) -> DataLayout:
    """Provide a DataLayout instance rooted at temp_data_dir."""
    return DataLayout(temp_data_dir)


@pytest.fixture()
def synthetic_cam() -> SyntheticCameraBackend:
    """Provide a fast deterministic synthetic camera oscillating at 2.0 Hz."""
    cam = SyntheticCameraBackend(
        oscillation_hz=2.0,
        secondary_hz=0.35,
        amplitude_px=0.8,
        color_pulse_amp=5.0,
        noise_std=0.1,
        realtime=False,
    )
    cam.start(width=128, height=96, fps=30)
    yield cam
    cam.stop()
