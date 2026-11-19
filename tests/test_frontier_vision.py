"""Tests for Clip Deep Inspector extractors, low-RAM streaming loader, DVS preview & endpoints."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from fastapi.testclient import TestClient

from src.core.hardware import build_hardware_profile
from src.main import create_app
from src.processing.frontier import (
    compute_dmd_modal_xray,
    compute_fluid_vorticity_lic,
    compute_optical_strain_gauge,
    compute_rolling_shutter_analysis,
    synthesize_rolling_shutter_wav,
)
from src.recording.recorder import load_clip_frames_and_timestamps


def test_dmd_modal_xray_recovers_synthetic_frequency() -> None:
    fps = 30.0
    n_frames = 90
    h, w = 96, 128
    frames = np.full((n_frames, h, w, 3), 40, dtype=np.uint8)
    for t_i in range(n_frames):
        val = round(128.0 + 70.0 * np.sin(2.0 * np.pi * 2.0 * (t_i / fps)))
        frames[t_i, 24:72, 32:96] = (val, val, val)

    res = compute_dmd_modal_xray(frames, fps=fps, max_modes=4)
    assert res["mode_count"] >= 1
    assert len(res["modes"]) >= 1
    top_mode = res["modes"][0]
    assert 1.6 <= top_mode["freq_hz"] <= 2.4
    assert top_mode["amp_png_b64"].startswith("data:image/png;base64,")
    assert top_mode["phase_png_b64"].startswith("data:image/png;base64,")


def test_fluid_vorticity_lic_detects_swirl() -> None:
    h, w = 128, 128
    rng = np.random.default_rng(123)
    base = cv2.GaussianBlur(rng.integers(40, 220, (h, w), dtype=np.uint8), (5, 5), 1.2)
    frames: list[np.ndarray] = []
    center = (w / 2.0, h / 2.0)
    for i in range(8):
        mat = cv2.getRotationMatrix2D(center, angle=float(i) * 4.5, scale=1.0)
        rot = cv2.warpAffine(base, mat, (w, h), borderMode=cv2.BORDER_REFLECT)
        frames.append(cv2.cvtColor(rot, cv2.COLOR_GRAY2BGR))

    res = compute_fluid_vorticity_lic(np.stack(frames, axis=0), fps=30.0, max_width=128)
    assert res["peak_vorticity_rad_s"] > 0.0
    assert res["mean_speed_px_s"] > 0.0
    assert res["vorticity_lic_png_b64"].startswith("data:image/png;base64,")
    assert res["speed_png_b64"].startswith("data:image/png;base64,")


def test_optical_strain_gauge_detects_stretch() -> None:
    h, w = 120, 160
    rng = np.random.default_rng(77)
    base = cv2.GaussianBlur(rng.integers(30, 230, (h, w), dtype=np.uint8), (5, 5), 1.0)
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    frames: list[np.ndarray] = []
    for i in range(6):
        alpha = float(i) / 5.0
        # Non-uniform horizontal stretch -> non-zero strain tensor exx
        dx = alpha * 3.5 * np.sin((gx / float(w)) * np.pi)
        mx = (gx - dx).astype(np.float32)
        my = gy.astype(np.float32)
        warped = cv2.remap(base, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        frames.append(cv2.cvtColor(warped, cv2.COLOR_GRAY2BGR))

    res = compute_optical_strain_gauge(np.stack(frames, axis=0), fps=30.0, max_width=160)
    assert res["peak_frame_index"] >= 1
    assert res["peak_von_mises_pct"] > 0.0
    assert res["peak_tension_pct"] > 0.0
    assert res["strain_vm_png_b64"].startswith("data:image/png;base64,")
    assert res["tension_comp_png_b64"].startswith("data:image/png;base64,")


def test_rolling_shutter_detects_super_nyquist_banding_and_wav() -> None:
    h, w = 240, 160
    n_frames = 12
    readout_ms = 16.0
    readout_s = readout_ms / 1000.0
    y_norm = np.arange(h, dtype=np.float32) / float(h)
    frames = np.empty((n_frames, h, w, 3), dtype=np.uint8)
    for t_i in range(n_frames):
        phase = float(t_i) * 0.73
        row_sig = 128.0 + 42.0 * np.sin(2.0 * np.pi * 1000.0 * (y_norm * readout_s) + phase)
        band = np.repeat(np.clip(row_sig, 0.0, 255.0).astype(np.uint8)[:, None], w, axis=1)
        frames[t_i] = cv2.cvtColor(band, cv2.COLOR_GRAY2BGR)

    arr_stack = frames
    res = compute_rolling_shutter_analysis(arr_stack, fps=30.0, readout_ms=readout_ms)
    assert 880.0 <= res["peak_hz"] <= 1120.0
    assert res["snr_db"] > 10.0
    assert res["rolling_png_b64"].startswith("data:image/png;base64,")

    wav_bytes = synthesize_rolling_shutter_wav(arr_stack, fps=30.0, readout_ms=readout_ms)
    assert wav_bytes[:4] == b"RIFF"
    assert wav_bytes[8:12] == b"WAVE"
    assert len(wav_bytes) > 512


def test_frontier_api_dvs_preview_and_ui(temp_data_dir: Path) -> None:
    app = create_app(data_dir=temp_data_dir, start_worker=False, realtime_synthetic=False)
    with TestClient(app) as client:
        sel = client.post(
            "/api/cameras/select",
            json={"camera_id": "synthetic:0", "complete_setup": True},
        )
        assert sel.status_code == 200

        for step_idx in range(2):
            dvs_res = client.get(f"/api/cameras/preview.jpg?mode=dvs_events&gain=2.0&step={step_idx}")
            assert dvs_res.status_code == 200
            assert dvs_res.headers["content-type"] == "image/jpeg"
            assert dvs_res.content.startswith(b"\xff\xd8")

        demo_res = client.post("/api/library/demo-clips")
        assert demo_res.status_code == 200
        clip_id = demo_res.json()["clips"][0]["clip_id"]

        dmd_res = client.get(f"/api/library/{clip_id}/frontier/dmd")
        assert dmd_res.status_code == 200
        assert "modes" in dmd_res.json()

        fluid_res = client.get(f"/api/library/{clip_id}/frontier/fluid")
        assert fluid_res.status_code == 200
        assert fluid_res.json()["vorticity_lic_png_b64"].startswith("data:image/png;base64,")

        strain_res = client.get(f"/api/library/{clip_id}/frontier/strain")
        assert strain_res.status_code == 200
        assert strain_res.json()["strain_vm_png_b64"].startswith("data:image/png;base64,")

        roll_res = client.get(f"/api/library/{clip_id}/frontier/rolling-shutter?readout_ms=16")
        assert roll_res.status_code == 200
        assert roll_res.json()["rolling_png_b64"].startswith("data:image/png;base64,")

        wav_res = client.get(f"/api/library/{clip_id}/frontier/rolling-shutter.wav")
        assert wav_res.status_code == 200
        assert "audio/wav" in wav_res.headers["content-type"]
        assert wav_res.content[:4] == b"RIFF"

        missing = client.get("/api/library/does-not-exist/frontier/dmd")
        assert missing.status_code == 404

        html = client.get("/").text
        for token in (
            "Clip Deep Inspector",
            "Auto-Find Vibrations",
            "Airflow &amp; Swirl Map",
            "Flex &amp; Stress Map",
            "Light Flicker &amp; Sound",
            "dvs_events",
            "frontier-modal",
            "btn-open-frontier-analysis",
        ):
            assert token in html
        assert "Frontier Vision Lab" not in html


def test_streaming_npz_loader_subset_and_resize(tmp_path: Path) -> None:
    clip_dir = tmp_path / "clip_stream"
    clip_dir.mkdir(parents=True)
    n_total, h, w = 24, 120, 160
    frames = np.zeros((n_total, h, w, 3), dtype=np.uint8)
    for i in range(n_total):
        frames[i, :, :, :] = (i * 10) % 250
    np.savez(clip_dir / "raw_frames.npz", frames=frames)
    (clip_dir / "meta.json").write_text('{"target_fps": 30.0}', encoding="utf-8")

    sub_f, sub_t, sub_fps = load_clip_frames_and_timestamps(
        clip_dir, max_width=80, selected_indices=[0, 5, 10, 23]
    )
    assert sub_f.shape == (4, 60, 80, 3)
    assert len(sub_t) == 4
    assert abs(sub_fps - 30.0) < 0.01
    assert int(sub_f[1, 10, 10, 0]) == 50
    assert int(sub_f[3, 10, 10, 0]) == 230

    strided_f, strided_t, eff_fps = load_clip_frames_and_timestamps(
        clip_dir, max_width=80, max_frames=8
    )
    assert len(strided_f) == len(strided_t)
    assert len(strided_f) <= 8
    assert eff_fps < 30.0


def test_hardware_tier_scaling_unlocks_pi5_ultra_budgets() -> None:
    gib = 1024**3
    dummy_therm = {
        "cpu_temp_c": 45.0,
        "throttled_hex": "0x0",
        "is_throttled": False,
        "under_voltage": False,
        "freq_capped": False,
        "thermal_status": "nominal",
    }
    ultra = build_hardware_profile("Raspberry Pi 5 Model B Rev 1.0", 8 * gib, telemetry=dummy_therm)
    high = build_hardware_profile("Raspberry Pi 5 Model B Rev 1.0", 4 * gib, telemetry=dummy_therm)
    bal = build_hardware_profile("Raspberry Pi 4 Model B Rev 1.4", 4 * gib, telemetry=dummy_therm)
    cons = build_hardware_profile("Raspberry Pi 4 Model B Rev 1.4", 2 * gib, telemetry=dummy_therm)

    assert ultra["performance_tier"] == "ultra"
    assert high["performance_tier"] == "high"
    assert bal["performance_tier"] == "balanced"
    assert cons["performance_tier"] == "constrained"

    for key in (
        "still_max_width",
        "dmd_max_dim",
        "fluid_max_width",
        "strain_max_width",
        "rolling_max_width",
    ):
        assert ultra["inspector_budgets"][key] > high["inspector_budgets"][key]
        assert high["inspector_budgets"][key] > bal["inspector_budgets"][key]
        assert bal["inspector_budgets"][key] > cons["inspector_budgets"][key]
