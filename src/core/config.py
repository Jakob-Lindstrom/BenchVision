"""Configuration models, environment overrides, and automatic schema migration."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from src import SCHEMA_VERSION, __version__

PACKAGE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = PACKAGE_DIR.parent
DEFAULT_DATA_DIR = REPO_ROOT / "data"


class LensConfig(BaseModel):
    """Per-camera optical and sensor defaults."""

    focal_length_mm: float = Field(default=4.0, ge=0.5, le=500.0)
    fov_degrees: float = Field(default=60.0, ge=5.0, le=220.0)
    target_distance_cm: float = Field(default=50.0, ge=1.0, le=5000.0)
    ruler_mm_per_pixel: float | None = Field(default=None, ge=0.0, le=100.0)
    ruler_ref_width_px: int = Field(default=640, ge=16, le=7680)
    focus_mode: str = Field(default="fixed", pattern="^(fixed|manual|autofocus)$")
    default_exposure_us: int = Field(default=10000, ge=100, le=500000)
    default_gain: float = Field(default=1.0, ge=0.0, le=64.0)
    default_wb_temp_k: int = Field(default=4600, ge=2000, le=10000)


class FrequencyPreset(BaseModel):
    """Named frequency band preset for motion/color magnification."""

    id: str
    name: str
    description: str
    low_hz: float = Field(ge=0.02, le=500.0)
    high_hz: float = Field(ge=0.05, le=500.0)
    default_alpha: float = Field(default=20.0, ge=1.0, le=200.0)
    recommended_method: str = "riesz"
    min_duration_s: float = Field(default=8.0, ge=1.0, le=600.0)


DEFAULT_PRESETS: list[FrequencyPreset] = [
    FrequencyPreset(
        id="human_heart",
        name="Human Resting Heartbeat",
        description="Typical adult resting pulse (48-180 BPM)",
        low_hz=0.8,
        high_hz=3.0,
        default_alpha=25.0,
        recommended_method="eulerian",
        min_duration_s=10.0,
    ),
    FrequencyPreset(
        id="cat_heart",
        name="Cat Heartbeat",
        description="Feline resting pulse (120-240 BPM)",
        low_hz=2.0,
        high_hz=4.0,
        default_alpha=25.0,
        recommended_method="riesz",
        min_duration_s=8.0,
    ),
    FrequencyPreset(
        id="breathing",
        name="Breathing / Respiration",
        description="Subtle chest/abdominal respiration motion (6-30 breaths/min)",
        low_hz=0.1,
        high_hz=0.5,
        default_alpha=15.0,
        recommended_method="riesz",
        min_duration_s=15.0,
    ),
    FrequencyPreset(
        id="machine_vibration",
        name="Machine / Motor Vibration",
        description="Mechanical oscillation, fans, pumps, bearings (5-14 Hz @ 30+ FPS)",
        low_hz=5.0,
        high_hz=14.0,
        default_alpha=30.0,
        recommended_method="phase_steerable",
        min_duration_s=5.0,
    ),
    FrequencyPreset(
        id="posy_motion_extract",
        name="Motion Extraction (Posy Gray Cancellation)",
        description="Cancels static background to flat 50% gray, isolating only moving edges & vibrations",
        low_hz=0.8,
        high_hz=6.0,
        default_alpha=15.0,
        recommended_method="motion_extraction",
        min_duration_s=5.0,
    ),
    FrequencyPreset(
        id="posy_rgb_trail",
        name="Posy RGB Chrono-Chromatic Trail",
        description="Maps R=t, G=t-k, B=t-2k so stationary objects stay normal color while moving edges split into rainbow trails",
        low_hz=0.8,
        high_hz=6.0,
        default_alpha=20.0,
        recommended_method="posy_rgb_trail",
        min_duration_s=5.0,
    ),
    FrequencyPreset(
        id="posy_peak_hold",
        name="Posy Motion Persistence (Peak-Hold Envelope)",
        description="Accumulates frame differences over a decaying window so rapid vibrations leave a glowing motion envelope",
        low_hz=1.0,
        high_hz=8.0,
        default_alpha=20.0,
        recommended_method="posy_peak_hold",
        min_duration_s=5.0,
    ),
    FrequencyPreset(
        id="auto_find",
        name="Auto-Find Dominant Band",
        description="Automatically detects the strongest periodic peak in the ROI spectrum",
        low_hz=0.5,
        high_hz=10.0,
        default_alpha=20.0,
        recommended_method="riesz",
        min_duration_s=8.0,
    ),
]


class AppSettings(BaseSettings):
    """Runtime settings persisted inside <data_dir>/config.json with ENV overrides."""

    model_config = SettingsConfigDict(
        env_prefix="BENCHVISION_",
        extra="ignore",
    )

    schema_version: int = SCHEMA_VERSION
    app_version: str = __version__
    data_dir: Path = Field(default_factory=lambda: DEFAULT_DATA_DIR)
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    # First-run & active camera configuration
    setup_completed: bool = False
    active_camera_id: str = "synthetic:0"
    dual_camera_enabled: bool = False
    secondary_camera_id: str = "synthetic:1"
    dual_sync_mode: str = Field(default="software", pattern="^(hardware|software|independent)$")
    default_width: int = 640
    default_height: int = 480
    default_fps: int = 30
    default_storage_format: str = "mjpeg_q100"  # mjpeg_q100 | ffv1_mkv | raw_npz

    camera_lenses: dict[str, LensConfig] = Field(
        default_factory=lambda: {
            "synthetic:0": LensConfig(
                focal_length_mm=6.0, fov_degrees=55.0, focus_mode="fixed"
            ),
            "synthetic:1": LensConfig(
                focal_length_mm=6.0, fov_degrees=55.0, focus_mode="fixed"
            ),
            "v4l2:/dev/video0": LensConfig(
                focal_length_mm=4.0, fov_degrees=60.0, focus_mode="fixed"
            ),
        }
    )
    presets: list[FrequencyPreset] = Field(
        default_factory=lambda: [p.model_copy() for p in DEFAULT_PRESETS]
    )


def migrate_config_dict(raw: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Migrate older config dictionaries to the current SCHEMA_VERSION."""
    migrated = dict(raw)
    changed = False
    version = int(migrated.get("schema_version", 1))

    if version < 2:
        migrated.setdefault("setup_completed", False)
        migrated.setdefault("default_storage_format", "mjpeg_q100")
        migrated.setdefault("camera_lenses", {})
        if "presets" not in migrated or not migrated["presets"]:
            migrated["presets"] = [p.model_dump() for p in DEFAULT_PRESETS]
        migrated["schema_version"] = SCHEMA_VERSION
        changed = True

    if "presets" in migrated and isinstance(migrated["presets"], list):
        existing_ids = {p.get("id") for p in migrated["presets"] if isinstance(p, dict)}
        for dp in DEFAULT_PRESETS:
            if dp.id not in existing_ids:
                migrated["presets"].append(dp.model_dump())
                changed = True

    if migrated.get("app_version") != __version__:
        migrated["app_version"] = __version__
        changed = True

    return migrated, changed


def load_or_create_settings(data_dir: Path | str | None = None) -> AppSettings:
    """Load settings from <data_dir>/config.json, migrating if needed, and apply env overrides."""
    raw_dir = data_dir or os.environ.get("BENCHVISION_DATA_DIR")
    if raw_dir is None or str(raw_dir).strip() in ("", "./data", "data"):
        resolved_dir = DEFAULT_DATA_DIR.resolve()
    else:
        resolved_dir = Path(raw_dir).resolve()
    resolved_dir.mkdir(parents=True, exist_ok=True)
    config_path = resolved_dir / "config.json"

    file_data: dict[str, Any] = {}
    should_save = not config_path.exists()

    if config_path.exists():
        try:
            file_data = json.loads(config_path.read_text(encoding="utf-8"))
            file_data, migrated = migrate_config_dict(file_data)
            if migrated:
                should_save = True
        except (OSError, ValueError, TypeError):
            should_save = True

    file_data["data_dir"] = str(resolved_dir)
    settings = AppSettings(**file_data)

    # Apply explicit environment variable overrides on top of file values
    if "BENCHVISION_PORT" in os.environ:
        settings.port = int(os.environ["BENCHVISION_PORT"])
    if "BENCHVISION_HOST" in os.environ:
        settings.host = os.environ["BENCHVISION_HOST"]
    if "BENCHVISION_ACTIVE_CAMERA_ID" in os.environ:
        settings.active_camera_id = os.environ["BENCHVISION_ACTIVE_CAMERA_ID"]

    if should_save:
        save_settings(settings)

    return settings


def save_settings(settings: AppSettings) -> Path:
    """Persist settings to <data_dir>/config.json."""
    resolved_dir = Path(settings.data_dir).resolve()
    resolved_dir.mkdir(parents=True, exist_ok=True)
    config_path = resolved_dir / "config.json"
    payload = settings.model_dump(mode="json")
    payload["data_dir"] = str(resolved_dir)
    config_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return config_path
