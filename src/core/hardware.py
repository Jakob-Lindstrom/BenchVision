"""Hardware capability detection for Raspberry Pi 4, Raspberry Pi 5, and development hosts."""

from __future__ import annotations

import math
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any


def read_pi_thermal_telemetry() -> dict[str, Any]:
    """Read Raspberry Pi CPU temperature (C) and vcgencmd throttling flags."""
    cpu_temp_c = 42.0
    tz_path = Path("/sys/class/thermal/thermal_zone0/temp")
    if tz_path.exists():
        try:
            raw_millideg = int(tz_path.read_text(encoding="utf-8").strip())
            cpu_temp_c = round(raw_millideg / 1000.0, 1)
        except (OSError, ValueError):
            pass

    throttled_hex = "0x0"
    flags = 0
    vcgencmd_bin = shutil.which("vcgencmd")
    if vcgencmd_bin:
        try:
            out = subprocess.check_output(
                [vcgencmd_bin, "get_throttled"],
                text=True,
                timeout=1.5,
                stderr=subprocess.DEVNULL,
            ).strip()
            if "=" in out:
                throttled_hex = out.split("=", 1)[1].strip()
                flags = int(throttled_hex, 16)
        except Exception:  # noqa: BLE001, S110
            pass

    under_voltage = bool(flags & 0x1)
    freq_capped = bool(flags & 0x2)
    currently_throttled = bool(flags & 0x4)
    soft_temp_limit = bool(flags & 0x8)
    is_throttled = currently_throttled or freq_capped or under_voltage or cpu_temp_c >= 80.0

    if is_throttled:
        thermal_status = "throttling"
    elif cpu_temp_c >= 70.0 or soft_temp_limit:
        thermal_status = "warm"
    else:
        thermal_status = "nominal"

    return {
        "cpu_temp_c": cpu_temp_c,
        "throttled_hex": throttled_hex,
        "is_throttled": is_throttled,
        "under_voltage": under_voltage,
        "freq_capped": freq_capped,
        "thermal_status": thermal_status,
    }


def compute_mm_per_pixel(
    target_distance_cm: float,
    fov_degrees: float,
    frame_width_px: int,
    ruler_mm_per_pixel: float | None = None,
    ruler_ref_width_px: int = 640,
) -> float:
    """Compute physical millimeters per pixel using 2-point ruler override or pinhole FOV geometry."""
    if ruler_mm_per_pixel is not None and ruler_mm_per_pixel > 0:
        ref_w = max(16, int(ruler_ref_width_px or frame_width_px))
        return float(ruler_mm_per_pixel) * (ref_w / float(max(frame_width_px, 1)))
    dist_mm = max(10.0, float(target_distance_cm) * 10.0)
    fov_rad = math.radians(max(5.0, min(175.0, float(fov_degrees))))
    scene_width_mm = 2.0 * dist_mm * math.tan(fov_rad / 2.0)
    return scene_width_mm / float(max(frame_width_px, 1))


def build_hardware_profile(
    model_str: str,
    total_ram_bytes: int,
    avail_ram_bytes: int | None = None,
    cpu_cores: int | None = None,
    telemetry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute hardware tier, adaptive processing widths, and Deep Inspector RAM budgets."""
    total_ram_gb = round(total_ram_bytes / (1024**3), 2)
    available_ram_gb = round((avail_ram_bytes or total_ram_bytes) / (1024**3), 2)
    eff_cores = cpu_cores if cpu_cores is not None else (os.cpu_count() or 4)
    is_pi5 = "Raspberry Pi 5" in model_str
    is_pi4 = "Raspberry Pi 4" in model_str
    low_memory_mode = total_ram_gb < 3.5

    # Adaptive hardware tiering so Pi 5 (8GB) runs unthrottled while Pi 4 (2GB) stays OOM-safe:
    # - ultra:       Pi 5 (8GB+ RAM) -> 1920px Full HD, 5 pyramid levels, 1:1 motion energy, full Deep Inspector depth
    # - high:        Pi 5 (4GB RAM) or Pi 4 (8GB RAM) -> 1280px HD, 4 pyramid levels, high-res Inspector
    # - balanced:    Pi 4 (4GB RAM) -> 960px, 4 pyramid levels, 1/2 motion energy, balanced Inspector
    # - constrained: Pi 4 (2GB RAM) -> 640px, 4 pyramid levels, strict low-RAM streaming Inspector budgets
    if is_pi5 and total_ram_gb >= 7.0:
        performance_tier = "ultra"
        recommended_max_width = 1920
        recommended_pyramid_levels = 5
        motion_energy_scale = 1
        quiver_sub_scale = 2
        sbs_panel_width = 960
        fft_zero_pad_factor = 8
        inspector_budgets = {
            "still_max_width": 1920,
            "still_max_frames": 80,
            "dmd_max_dim": 224,
            "dmd_max_frames": 240,
            "dmd_max_rank": 18,
            "fluid_max_width": 640,
            "fluid_pairs": 24,
            "strain_max_width": 640,
            "strain_scan_frames": 40,
            "rolling_max_width": 1080,
            "rolling_frames": 48,
            "rolling_wav_frames": 64,
            "autoband_max_width": 480,
            "autoband_max_frames": 360,
        }
    elif (is_pi5 and total_ram_gb >= 3.5) or total_ram_gb >= 7.0:
        performance_tier = "high"
        recommended_max_width = 1280
        recommended_pyramid_levels = 4
        motion_energy_scale = 1 if total_ram_gb >= 7.0 else 2
        quiver_sub_scale = 2 if is_pi5 else 3
        sbs_panel_width = 640
        fft_zero_pad_factor = 6
        inspector_budgets = {
            "still_max_width": 1280,
            "still_max_frames": 64,
            "dmd_max_dim": 160,
            "dmd_max_frames": 160,
            "dmd_max_rank": 16,
            "fluid_max_width": 480,
            "fluid_pairs": 16,
            "strain_max_width": 480,
            "strain_scan_frames": 30,
            "rolling_max_width": 720,
            "rolling_frames": 36,
            "rolling_wav_frames": 48,
            "autoband_max_width": 360,
            "autoband_max_frames": 240,
        }
    elif total_ram_gb >= 3.5:
        performance_tier = "balanced"
        recommended_max_width = 960
        recommended_pyramid_levels = 4
        motion_energy_scale = 2
        quiver_sub_scale = 3
        sbs_panel_width = 640
        fft_zero_pad_factor = 4
        inspector_budgets = {
            "still_max_width": 800,
            "still_max_frames": 48,
            "dmd_max_dim": 128,
            "dmd_max_frames": 120,
            "dmd_max_rank": 14,
            "fluid_max_width": 360,
            "fluid_pairs": 12,
            "strain_max_width": 360,
            "strain_scan_frames": 24,
            "rolling_max_width": 480,
            "rolling_frames": 28,
            "rolling_wav_frames": 36,
            "autoband_max_width": 320,
            "autoband_max_frames": 200,
        }
    else:
        performance_tier = "constrained"
        recommended_max_width = 640
        recommended_pyramid_levels = 4
        motion_energy_scale = 2
        quiver_sub_scale = 4
        sbs_panel_width = 480
        fft_zero_pad_factor = 4
        inspector_budgets = {
            "still_max_width": 540,
            "still_max_frames": 48,
            "dmd_max_dim": 96,
            "dmd_max_frames": 90,
            "dmd_max_rank": 14,
            "fluid_max_width": 240,
            "fluid_pairs": 10,
            "strain_max_width": 240,
            "strain_scan_frames": 21,
            "rolling_max_width": 360,
            "rolling_frames": 24,
            "rolling_wav_frames": 32,
            "autoband_max_width": 240,
            "autoband_max_frames": 180,
        }

    # Pi 5 has RP1 southbridge + Cortex-A76 (~2.6x faster SIMD/NEON than Pi 4 Cortex-A72)
    speed_multiplier = 2.6 if is_pi5 else 1.0
    chunk_cols = 32768 if total_ram_gb >= 7.0 else (16384 if total_ram_gb >= 3.5 else 4096)
    strip_rows = 256 if total_ram_gb >= 7.0 else (128 if total_ram_gb >= 3.5 else 64)
    therm = telemetry if telemetry is not None else read_pi_thermal_telemetry()

    return {
        "model": model_str,
        "is_pi4": is_pi4,
        "is_pi5": is_pi5,
        "performance_tier": performance_tier,
        "architecture": platform.machine(),
        "cpu_cores": eff_cores,
        "cpu_count": eff_cores,
        "total_ram_bytes": total_ram_bytes,
        "total_ram_gb": total_ram_gb,
        "available_ram_gb": available_ram_gb,
        "low_memory_mode": low_memory_mode,
        "speed_multiplier": speed_multiplier,
        "chunk_cols": chunk_cols,
        "strip_rows": strip_rows,
        "recommended_max_width": recommended_max_width,
        "recommended_pyramid_levels": recommended_pyramid_levels,
        "motion_energy_scale": motion_energy_scale,
        "quiver_sub_scale": quiver_sub_scale,
        "sbs_panel_width": sbs_panel_width,
        "fft_zero_pad_factor": fft_zero_pad_factor,
        "inspector_budgets": inspector_budgets,
        **therm,
    }


def detect_hardware_profile() -> dict[str, Any]:
    """Detect Pi model (Pi 4 vs Pi 5), total RAM, and adaptive processing tuning parameters."""
    model_str = "Generic Linux Host"
    dt_model = Path("/proc/device-tree/model")
    if dt_model.exists():
        try:
            model_str = dt_model.read_bytes().decode("utf-8", errors="ignore").rstrip("\x00").strip()
        except OSError:
            pass

    total_ram_bytes = 2 * (1024**3)
    avail_ram_bytes: int | None = None
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        try:
            for line in meminfo.read_text(encoding="utf-8").splitlines():
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    total_ram_bytes = kb * 1024
                elif line.startswith("MemAvailable:"):
                    kb_avail = int(line.split()[1])
                    avail_ram_bytes = kb_avail * 1024
        except (OSError, ValueError, IndexError):
            pass

    return build_hardware_profile(
        model_str=model_str,
        total_ram_bytes=total_ram_bytes,
        avail_ram_bytes=avail_ram_bytes,
        cpu_cores=os.cpu_count() or 4,
    )


def evaluate_dual_camera_tier(
    discovered_cameras: list[dict[str, Any]],
    hw_profile: dict[str, Any] | None = None,
    cam_a_id: str = "",
    cam_b_id: str = "",
) -> dict[str, Any]:
    """Evaluate 3-tier hardware capability gating for Dual-Camera Mode."""
    hw = hw_profile or detect_hardware_profile()
    total_ram_gb = float(hw.get("total_ram_gb", 2.0))
    is_pi4 = bool(hw.get("is_pi4", False))
    is_pi5 = bool(hw.get("is_pi5", False))
    cam_count = len(discovered_cameras)

    if cam_count < 2 or total_ram_gb < 1.8:
        return {
            "tier": 1,
            "allowed": False,
            "capped": True,
            "max_width": 640,
            "max_height": 480,
            "max_fps": 30,
            "tier_label": "Tier 1 (Disabled)",
            "reason": "Requires 2 connected cameras (or 1 camera + Synthetic Reference) and >= 2 GB RAM.",
        }

    # Check if mixed CSI + USB or Pi 4 or < 3.8 GB RAM -> Tier 2 (Capped to 640x480 @ 30 FPS)
    backend_a = cam_a_id.split(":", 1)[0] if ":" in cam_a_id else ""
    backend_b = cam_b_id.split(":", 1)[0] if ":" in cam_b_id else ""
    mixed_usb_csi = (
        backend_a != ""
        and backend_b != ""
        and {backend_a, backend_b} == {"picamera2", "v4l2"}
    ) or ("v4l2" in (backend_a, backend_b) and not is_pi5)

    if is_pi4 or total_ram_gb < 3.8 or mixed_usb_csi:
        return {
            "tier": 2,
            "allowed": True,
            "capped": True,
            "max_width": 640,
            "max_height": 480,
            "max_fps": 30,
            "tier_label": "Tier 2 (Capped: 640x480 @ 30 FPS)",
            "reason": "Dual-stream capture capped to 640x480 @ 30 FPS to protect USB/CSI bus bandwidth and RAM.",
        }

    return {
        "tier": 3,
        "allowed": True,
        "capped": False,
        "max_width": 1920,
        "max_height": 1080,
        "max_fps": 120,
        "tier_label": "Tier 3 (Unthrottled Dual-Stream)",
        "reason": "Full-resolution dual-camera capture unlocked (4GB+ RAM & high-bandwidth bus).",
    }


