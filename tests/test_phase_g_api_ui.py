"""Phase (g) tests: FastAPI endpoints and static UI serving."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from fastapi.testclient import TestClient

from src import __version__
from src.cameras.v4l2_usb import V4L2UsbCameraBackend
from src.main import create_app


def test_full_api_and_ui_workflow(temp_data_dir: Path) -> None:
    app = create_app(data_dir=temp_data_dir, start_worker=False, realtime_synthetic=False)
    with TestClient(app) as client:
        # 1. Verify UI index and static assets
        idx_res = client.get("/")
        assert idx_res.status_code == 200
        assert "BenchVision" in idx_res.text

        css_res = client.get("/static/styles.css")
        assert css_res.status_code == 200

        # 2. Verify health and camera setup
        health = client.get("/api/health").json()
        assert health["status"] == "ok"

        cams = client.get("/api/cameras").json()
        assert any(c["camera_id"] == "synthetic:0" for c in cams["discovered"])

        sel = client.post(
            "/api/cameras/select",
            json={"camera_id": "synthetic:0", "complete_setup": True},
        ).json()
        assert sel["setup_completed"] is True
        assert sel["active_camera_id"] == "synthetic:0"

        # 3. Lock & Unlock controls + preflight check
        locked = client.post(
            "/api/cameras/lock",
            json={"width": 80, "height": 60, "fps": 30, "exposure_us": 10000, "gain": 1.5},
        ).json()
        assert locked["controls"]["locked"] is True

        unlocked = client.post("/api/cameras/unlock").json()
        assert unlocked["controls"]["locked"] is False

        locked = client.post(
            "/api/cameras/lock",
            json={"width": 80, "height": 60, "fps": 30, "exposure_us": 10000, "gain": 1.5},
        ).json()
        assert locked["controls"]["locked"] is True

        pre = client.post(
            "/api/record/preflight",
            json={"width": 80, "height": 60, "fps": 30.0, "duration_s": 3.0, "max_freq_hz": 3.0},
        ).json()
        assert pre["nyquist"]["valid"] is True
        assert pre["storage"]["sufficient_space"] is True

        # 4. Record clip
        rec_meta = client.post(
            "/api/record/start",
            json={"title": "API Test Clip", "duration_s": 3.0, "width": 80, "height": 60, "fps": 30},
        ).json()
        clip_id = rec_meta["clip_id"]

        lib = client.get("/api/library").json()
        assert any(c["clip_id"] == clip_id for c in lib["clips"])

        # 5. Submit synchronous processing job & verify spectrum + CSV export + comparison
        job = client.post(
            "/api/jobs",
            json={
                "clip_id": clip_id,
                "params": {
                    "method": "riesz",
                    "low_hz": 1.2,
                    "high_hz": 3.0,
                    "alpha": 20.0,
                    "pyramid_levels": 3,
                },
                "run_async": False,
            },
        ).json()
        assert job["status"] == "completed"
        job_id = job["job_id"]

        spec = client.get(f"/api/jobs/{job_id}/spectrum").json()
        assert 1.7 <= spec["dominant_hz"] <= 2.3

        csv_res = client.get(f"/api/jobs/{job_id}/files/signal.csv")
        assert csv_res.status_code == 200
        assert "frame_index,time_s,raw_signal,filtered_signal" in csv_res.text

        mp4_res = client.get(f"/api/jobs/{job_id}/files/comparison_sbs.mp4")
        assert mp4_res.status_code == 200
        assert "video/mp4" in mp4_res.headers.get("content-type", "")

        comp = client.get(f"/api/compare/{clip_id}").json()
        assert comp["job_count"] == 1

        sys_info = client.get("/api/system").json()
        assert sys_info["current_version"] == __version__ == "1.0.1"
        assert sys_info["changelog"][0]["version"] == "1.0.1"
        assert sys_info["disk"]["free_bytes"] > 0
        assert "hardware" in sys_info
        assert "inspector_budgets" in sys_info["hardware"]
        assert sys_info["hardware"]["total_ram_gb"] > 0
        assert "iconbv.png" in idx_res.text
        assert "iconbv.svg" in idx_res.text
        svg_res = client.get("/static/iconbv.svg")
        assert svg_res.status_code == 200
        assert "<svg" in svg_res.text
        assert "data-tip=" in idx_res.text
        assert "unlockCameraControls()" in idx_res.text


def test_roi_preview_and_v4l2_repeated_lock(temp_data_dir: Path, monkeypatch) -> None:
    app = create_app(data_dir=temp_data_dir, start_worker=False, realtime_synthetic=False)
    with TestClient(app) as client:
        client.post(
            "/api/cameras/select",
            json={"camera_id": "synthetic:0", "complete_setup": True},
        )
        # 1. Verify preflight sets live preview ROI and crop_preview mode
        pre = client.post(
            "/api/record/preflight",
            json={
                "width": 160,
                "height": 120,
                "fps": 30.0,
                "duration_s": 3.0,
                "max_freq_hz": 3.0,
                "roi": [16, 16, 64, 64],
                "crop_preview": True,
            },
        ).json()
        assert pre["active_roi"] == [16, 16, 64, 64]

        jpg_bytes = client.get("/api/cameras/preview.jpg").content
        arr = np.frombuffer(jpg_bytes, dtype=np.uint8)
        decoded = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        assert decoded is not None
        assert decoded.shape[:2] == (256, 256)

        # 2. Verify repeated V4L2 lock_controls never resets gain <= 2.0 on 0..255 webcams
        sample_v4l2 = (
            "gain 0x00980913 (int) : min=0 max=255 step=1 default=0 value=120\n"
            "exposure_time_absolute 0x009a0902 (int) : min=1 max=10000 step=1 default=156 value=300\n"
            "white_balance_temperature 0x0098091a (int) : min=2000 max=6500 step=1 default=4000 value=5200\n"
        )
        cam = V4L2UsbCameraBackend(device_path="/dev/video99", name="Mock C270")
        monkeypatch.setattr(cam, "_run_v4l2_ctl", lambda args: sample_v4l2)

        first_lock = cam.lock_controls({"exposure_us": 10000, "gain": 1.0, "fps": 30})
        assert first_lock.locked is True
        assert first_lock.gain == 120.0

        # Second lock with default UI gain=1.0 while already locked must preserve bright gain
        second_lock = cam.lock_controls({"exposure_us": 10000, "gain": 1.0, "fps": 30})
        assert second_lock.locked is True
        assert second_lock.gain == 120.0


def test_posy_modes_epicenter_kymograph_dual_roi_and_calibration(temp_data_dir: Path) -> None:
    app = create_app(data_dir=temp_data_dir, start_worker=False, realtime_synthetic=False)
    with TestClient(app) as client:
        # 1. Configure lens with target_distance_cm and select camera
        lens_res = client.post(
            "/api/cameras/synthetic:0/lens",
            json={
                "focal_length_mm": 4.0,
                "fov_degrees": 60.0,
                "target_distance_cm": 50.0,
                "focus_mode": "fixed",
            },
        ).json()
        assert lens_res["lens"]["target_distance_cm"] == 50.0

        sel = client.post(
            "/api/cameras/select",
            json={"camera_id": "synthetic:0", "complete_setup": True},
        ).json()
        assert sel["capabilities"]["lens_info"]["target_distance_cm"] == 50.0

        # 2. Test Tare / Zero reference frame lock and Posy / Strobe preview modes
        tare_on = client.post("/api/cameras/tare", json={"enable": True}).json()
        assert tare_on["tare_locked"] is True

        for mode_q in (
            "mode=motion_extraction",
            "mode=posy_rgb_trail&shift_frames=2",
            "mode=posy_peak_hold&shift_frames=2",
            "mode=stroboscope&strobe_hz=3.0",
        ):
            res = client.get(f"/api/cameras/preview.jpg?{mode_q}")
            assert res.status_code == 200
            assert len(res.content) > 100

        tare_off = client.post("/api/cameras/tare", json={"enable": False}).json()
        assert tare_off["tare_locked"] is False

        # 3. Record clip and run job with dual ROI, sub-pixel stabilization, and heatmap noise gate
        rec_meta = client.post(
            "/api/record/start",
            json={"title": "Posy & Physics Test", "duration_s": 3.0, "width": 80, "height": 60, "fps": 30},
        ).json()
        clip_id = rec_meta["clip_id"]

        job = client.post(
            "/api/jobs",
            json={
                "clip_id": clip_id,
                "params": {
                    "method": "posy_rgb_trail",
                    "low_hz": 1.2,
                    "high_hz": 3.2,
                    "alpha": 15.0,
                    "pyramid_levels": 3,
                    "chrom_attenuation": 0.5,
                    "heatmap_threshold": 0.25,
                    "stabilize_subpixel": True,
                    "roi": [10, 10, 30, 30],
                    "roi_b": [40, 20, 30, 30],
                },
                "run_async": False,
            },
        ).json()
        assert job["status"] == "completed"
        job_id = job["job_id"]
        result = job["result"]

        # 4. Verify Epicenter, Calibration, Kymograph, Dual-ROI, and Phase/Quiver outputs
        assert "epicenter" in result
        assert 0 <= result["epicenter"]["x"] < 80
        assert 0 <= result["epicenter"]["y"] < 60
        assert "calibration" in result
        assert result["calibration"]["mm_per_pixel"] > 0
        assert result["calibration"]["peak_displacement_um"] >= 0

        for fname in ("phase_ripple.mp4", "quiver_vectors.mp4", "heatmap.mp4"):
            f_res = client.get(f"/api/jobs/{job_id}/files/{fname}")
            assert f_res.status_code == 200

        kymo = client.get(f"/api/jobs/{job_id}/kymograph?axis=horizontal&pos=0.5").json()
        assert kymo["axis"] == "horizontal"
        assert kymo["n_frames"] > 0
        assert kymo["spatial_length"] > 0

        dual = client.post(
            f"/api/jobs/{job_id}/dual-roi",
            json={"roi_a": [8, 12, 24, 24], "roi_b": [42, 18, 24, 24]},
        ).json()
        assert -180.0 <= dual["phase_shift_deg"] <= 180.0
        assert len(dual["signal_a"]) == len(dual["signal_b"])
        assert dual["roi_a"] == [8, 12, 24, 24]
        assert dual["roi_b"] == [42, 18, 24, 24]

        spec_after = client.get(f"/api/jobs/{job_id}/spectrum").json()
        assert spec_after["dual_roi"]["roi_a"] == [8, 12, 24, 24]
        assert spec_after["dual_roi"]["roi_b"] == [42, 18, 24, 24]
        assert client.get(f"/api/jobs/{job_id}/files/report_base.jpg").status_code == 200
        assert client.get(f"/api/jobs/{job_id}/files/report_frame.jpg").status_code == 200

        html_text = client.get("/").text
        assert "btn-draw-probe-a" in html_text
        assert "btn-draw-probe-b" in html_text
        assert "btn-reset-dualroi" in html_text

        # 5. Verify Pi thermal telemetry fields in /api/system
        sys_info = client.get("/api/system").json()
        assert "thermal_status" in sys_info["hardware"]
        assert "is_throttled" in sys_info["hardware"]


def test_upload_fps_override_ruler_calibration_preroll_and_report(temp_data_dir: Path) -> None:
    app = create_app(data_dir=temp_data_dir, start_worker=False, realtime_synthetic=False)
    with TestClient(app) as client:
        client.post(
            "/api/cameras/select",
            json={"camera_id": "synthetic:0", "complete_setup": True},
        )

        # 1. 2-Point Ruler Calibration: 200 px = 50 mm at 640px width -> 0.25 mm/px
        ruler_res = client.post(
            "/api/cameras/calibrate-ruler",
            json={
                "pixel_distance": 200.0,
                "known_length_mm": 50.0,
                "frame_width": 640,
                "clear": False,
            },
        ).json()
        assert ruler_res["calibration_mode"] == "ruler_2pt"
        assert abs(ruler_res["mm_per_pixel"] - 0.25) < 1e-4

        # 2. Populate pre-roll buffer via preview.jpg & evaluate live trigger signal
        for _ in range(12):
            res_jpg = client.get("/api/cameras/preview.jpg")
            assert res_jpg.status_code == 200

        sig_res = client.get("/api/cameras/live-signal?threshold_sigma=3.0&threshold_pct=2.0").json()
        assert "trigger" in sig_res
        assert sig_res["trigger"]["preroll_frames"] >= 1

        # Record a clip with include_preroll=True
        rec_meta = client.post(
            "/api/record/start",
            json={
                "title": "Triggered Clip",
                "duration_s": 2.0,
                "width": 80,
                "height": 60,
                "fps": 30,
                "include_preroll": True,
                "preroll_seconds": 1.5,
            },
        ).json()
        assert rec_meta["frame_count"] > 60  # 2.0s * 30fps + prepended pre-roll frames

        # 3. External video upload with True Capture FPS Override (e.g. 240 FPS slow-mo)
        preview_mp4_path = temp_data_dir / "recordings" / rec_meta["clip_id"] / "preview.mp4"
        video_bytes = preview_mp4_path.read_bytes()
        up_res = client.post(
            "/api/library/upload",
            data={"title": "Smartphone 240FPS Upload", "true_fps": "240"},
            files={"file": ("slowmo_test.mp4", video_bytes, "video/mp4")},
        )
        assert up_res.status_code == 200
        up_meta = up_res.json()
        assert up_meta["title"] == "Smartphone 240FPS Upload"
        assert up_meta["achieved_fps"] == 240.0
        assert up_meta["fps_override_applied"] is True

        # 4. Run Optical Flow job and verify ruler scale + report_frame.jpg + epicenter
        job = client.post(
            "/api/jobs",
            json={
                "clip_id": up_meta["clip_id"],
                "params": {
                    "method": "optical_flow",
                    "low_hz": 5.0,
                    "high_hz": 30.0,
                    "alpha": 15.0,
                    "pyramid_levels": 3,
                },
                "run_async": False,
            },
        ).json()
        assert job["status"] == "completed"
        job_id = job["job_id"]

        expected_mm_px = 0.25 * (640.0 / float(up_meta["width"]))
        assert abs(job["result"]["calibration"]["mm_per_pixel"] - expected_mm_px) < 0.01

        rep_res = client.get(f"/api/jobs/{job_id}/files/report_frame.jpg")
        assert rep_res.status_code == 200
        assert len(rep_res.content) > 200

        # 5. Verify temporal clip trimming (trim_start_s / trim_end_s) and Frame-0 spike suppression
        trim_job = client.post(
            "/api/jobs",
            json={
                "clip_id": rec_meta["clip_id"],
                "params": {
                    "method": "eulerian",
                    "low_hz": 1.0,
                    "high_hz": 4.0,
                    "alpha": 15.0,
                    "pyramid_levels": 3,
                    "trim_start_s": 0.4,
                    "trim_end_s": 0.4,
                },
                "run_async": False,
            },
        ).json()
        assert trim_job["status"] == "completed"
        spec_trim = client.get(f"/api/jobs/{trim_job['job_id']}/spectrum").json()
        # Trimmed time series must have fewer frames than the untrimmed recording
        assert len(spec_trim["time_series"]["t_s"]) < rec_meta["frame_count"]
        # First 4 samples of raw detrended signal must not have a massive Frame-0 spike
        filt_sig = np.asarray(spec_trim["time_series"]["filtered"], dtype=np.float64)
        p95 = float(np.percentile(np.abs(filt_sig[4:]), 95))
        assert np.max(np.abs(filt_sig[:4])) <= p95 * 1.05 + 1e-6


def test_v07_release_features_demos_assist_gif_iso_orbit_and_aliases(temp_data_dir: Path) -> None:
    app = create_app(data_dir=temp_data_dir, start_worker=False, realtime_synthetic=False)
    with TestClient(app) as client:
        client.post(
            "/api/cameras/select",
            json={"camera_id": "synthetic:0", "complete_setup": True},
        )

        # 1. Focus Peaking & Overexposure Zebra Assist on Live Preview
        for assist_mode in ("off", "focus_peaking", "zebras"):
            resp_assist = client.get(f"/api/cameras/preview.jpg?assist={assist_mode}")
            assert resp_assist.status_code == 200
            assert resp_assist.headers["content-type"] == "image/jpeg"
            assert len(resp_assist.content) > 200

        # 2. One-Click "Generate Demo Showcase Clips" in Library
        demo_res = client.post("/api/library/demo-clips")
        assert demo_res.status_code == 200
        demo_data = demo_res.json()
        assert demo_data["created_count"] == 2
        demo_clips = demo_data["clips"]
        assert any("Cantilever" in c["title"] for c in demo_clips)
        assert any("Micro-Pulse" in c["title"] for c in demo_clips)
        clip_id = demo_clips[0]["clip_id"]

        # 3 & 4. Run job with Background Noise Gate (SNR Masking), verify Split-Wipe & Looping GIF export
        job = client.post(
            "/api/jobs",
            json={
                "clip_id": clip_id,
                "params": {
                    "method": "eulerian",
                    "low_hz": 1.4,
                    "high_hz": 2.8,
                    "alpha": 20.0,
                    "pyramid_levels": 3,
                    "max_width": 160,
                    "heatmap_threshold": 0.25,
                    "gate_background_noise": True,
                },
                "run_async": False,
            },
        ).json()
        assert job["status"] == "completed"
        job_id = job["job_id"]

        # Split-Wipe original_share.mp4 and magnified_share.mp4
        assert client.get(f"/api/jobs/{job_id}/files/original_share.mp4").status_code == 200
        assert client.get(f"/api/jobs/{job_id}/files/magnified_share.mp4").status_code == 200

        # Looping GIF Export (GIF89a header)
        gif_res = client.get(f"/api/jobs/{job_id}/gif?mode=magnified_mp4")
        assert gif_res.status_code == 200
        assert gif_res.headers["content-type"] == "image/gif"
        assert gif_res.content[:6] == b"GIF89a"
        assert b"NETSCAPE2.0" in gif_res.content

        # 5, 6, 9. Spectrum endpoint: ISO Vibration Metrics, AC Lighting Aliases, and 2D Motion Orbit
        spec = client.get(f"/api/jobs/{job_id}/spectrum").json()
        cal = spec["calibration"]
        assert "velocity_rms_mm_s" in cal
        assert "peak_accel_g" in cal
        assert cal["velocity_rms_mm_s"] >= 0.0
        assert cal["peak_accel_g"] >= 0.0

        aliases = spec["lighting_aliases"]
        assert "alias_50hz_mains_hz" in aliases
        assert "alias_60hz_mains_hz" in aliases
        assert abs(aliases["alias_50hz_mains_hz"] - 10.0) < 0.01  # 100 Hz aliased at 30 FPS -> 10 Hz
        assert abs(aliases["alias_60hz_mains_hz"] - 0.0) < 0.01   # 120 Hz aliased at 30 FPS -> 0 Hz

        orbit = spec["orbit_2d"]
        assert len(orbit["x_um"]) > 10
        assert len(orbit["y_um"]) == len(orbit["x_um"])
        assert -90.0 <= orbit["principal_angle_deg"] <= 90.0
        assert 0.0 <= orbit["eccentricity"] <= 1.0

        # Verify UI HTML includes all 9 feature controls
        html = client.get("/").text
        for elem_id in (
            "live-preview-assist",
            "btn-generate-demos",
            "split-wipe-container",
            "proc-bg-gate",
            "analysis-baseline-select",
            "kpi-iso-kinematics",
            "btn-download-wav",
            "fft-alias-warning",
            "canvas-orbit",
        ):
            assert elem_id in html


def test_dual_camera_part1_setup_preview_and_recording(temp_data_dir: Path) -> None:
    app = create_app(data_dir=temp_data_dir, start_worker=False, realtime_synthetic=False)
    with TestClient(app) as client:
        # 1. GET /api/cameras exposes >= 2 synthetic cameras and dual_tier hardware evaluation
        cam_data = client.get("/api/cameras").json()
        assert len(cam_data["discovered"]) >= 2
        assert cam_data["dual_camera_enabled"] is False
        assert "dual_tier" in cam_data
        assert cam_data["dual_tier"]["allowed"] is True

        # 2. Enable Dual-Camera mode via POST /api/cameras/select
        sel_res = client.post(
            "/api/cameras/select",
            json={
                "camera_id": "synthetic:0",
                "complete_setup": True,
                "dual_camera_enabled": True,
                "secondary_camera_id": "synthetic:1",
                "dual_sync_mode": "software",
            },
        ).json()
        assert sel_res["dual_camera_enabled"] is True
        assert sel_res["secondary_camera_id"] == "synthetic:1"
        assert sel_res["dual_sync_mode"] == "software"

        # 3. Preview endpoints work for Cam A, Cam B, and Side-by-Side (sbs)
        for cam_view in ("a", "b", "sbs"):
            prev = client.get(f"/api/cameras/preview.jpg?cam_view={cam_view}")
            assert prev.status_code == 200
            assert prev.headers["content-type"] == "image/jpeg"
            assert len(prev.content) > 500

        # 4. Preflight reports live sync delta badge
        pf = client.post(
            "/api/record/preflight",
            json={
                "width": 160,
                "height": 120,
                "fps": 30.0,
                "duration_s": 1.0,
                "min_freq_hz": 0.5,
                "max_freq_hz": 5.0,
                "storage_format": "npz",
            },
        ).json()
        assert pf["dual_sync"]["enabled"] is True
        assert "Sync" in pf["dual_sync"]["badge_text"]

        # 5. Record a synchronized Dual-Camera clip and verify frames + frames_b
        rec = client.post(
            "/api/record/start",
            json={
                "title": "Dual Cam Test",
                "width": 160,
                "height": 120,
                "fps": 30,
                "duration_s": 1.0,
                "storage_format": "npz",
            },
        ).json()
        assert rec["dual_camera"] is True
        assert rec["secondary_camera_id"] == "synthetic:1"
        assert rec["sync_mode"] == "software"
        assert "sync_dt_mean_ms" in rec
        clip_id = rec["clip_id"]

        # 6. Part 2: Submit a Dual-Camera Magnification Job with target_stream="both"
        job = client.post(
            "/api/jobs",
            json={
                "clip_id": clip_id,
                "params": {
                    "method": "eulerian",
                    "low_hz": 1.2,
                    "high_hz": 3.5,
                    "alpha": 20.0,
                    "pyramid_levels": 3,
                    "max_width": 160,
                    "target_stream": "both",
                },
                "run_async": False,
            },
        ).json()
        assert job["status"] == "completed"
        assert job["result"]["dual_camera"] is True
        assert job["result"]["files"]["dual_sync_mp4"] == "dual_sync.mp4"
        job_id = job["job_id"]

        # Verify dual_sync.mp4 file & GIF export
        assert client.get(f"/api/jobs/{job_id}/files/dual_sync.mp4").status_code == 200
        assert client.get(f"/api/jobs/{job_id}/gif?mode=dual_sync_mp4").status_code == 200

        # 7. Verify Page 4 Cross-Camera Spectrum, Phase Shift, Coherence, and 3D Orbit
        spec = client.get(f"/api/jobs/{job_id}/spectrum").json()
        assert spec["dual_camera"] is True
        d_sync = spec["dual_camera_sync"]
        assert d_sync["enabled"] is True
        assert -180.0 <= d_sync["phase_shift_deg"] <= 180.0
        assert 0.0 <= d_sync["coherence"] <= 1.0
        assert len(d_sync["cam_b_fft_magnitude"]) == len(spec["fft_magnitude"])
        orbit_3d = d_sync["orbit_3d"]
        assert len(orbit_3d["x_um"]) > 10
        assert len(orbit_3d["y_um"]) == len(orbit_3d["x_um"])
        assert len(orbit_3d["z_um"]) == len(orbit_3d["x_um"])
        assert orbit_3d["radius_3d_rms_um"] >= 0.0

        # Verify UI progressive disclosure classes and elements exist across Pages 1-4
        html = client.get("/").text
        for elem_id in (
            "dual-cam-only",
            "dual-cam-topbar-badge",
            "dual-preview-switcher",
            "setup-dual-enable",
            "setup-dual-panel",
            "setup-camera-b-select",
            "setup-sync-mode",
            "proc-target-stream-wrap",
            "proc-target-stream",
            "opt-dual-sync-mode",
            "dual-cam-analysis-banner",
            "subtab-btn-orbit3d",
            "canvas-orbit-3d",
        ):
            assert elem_id in html


def test_roi_crop_and_60fps_mode_switch_recording(temp_data_dir: Path) -> None:
    """Verify ROI crop & 60 FPS switching when camera initially streams at 640x480 @ 30 FPS."""
    from src.cameras.v4l2_usb import V4L2UsbCameraBackend, parse_v4l2_formats_ext_output

    sample_brio_ext = """
ioctl: VIDIOC_ENUM_FMT
\tType: Video Capture

\t[0]: 'MJPG' (Motion-JPEG, compressed)
\t\tSize: Discrete 640x480
\t\t\tInterval: Discrete 0.033s (30.000 fps)
\t\t\tInterval: Discrete 0.067s (15.000 fps)
\t\tSize: Discrete 1280x720
\t\t\tInterval: Discrete 0.017s (60.000 fps)
\t\t\tInterval: Discrete 0.033s (30.000 fps)
\t\tSize: Discrete 1920x1080
\t\t\tInterval: Discrete 0.033s (30.000 fps)
"""
    modes = parse_v4l2_formats_ext_output(sample_brio_ext)
    assert modes[(640, 480)] == [30, 15]
    assert modes[(1280, 720)] == [60, 30]
    assert modes[(1920, 1080)] == [30]

    v4l2_cam = V4L2UsbCameraBackend()
    v4l2_cam._mjpg_modes = modes
    # Requesting 60 FPS at 640x480, 1280x720, or 1920x1080 resolves to hardware 1280x720 @ 60 FPS
    assert v4l2_cam._resolve_hw_capture_mode(640, 480, 60) == (1280, 720, 60)
    assert v4l2_cam._resolve_hw_capture_mode(1280, 720, 60) == (1280, 720, 60)
    assert v4l2_cam._resolve_hw_capture_mode(1920, 1080, 60) == (1280, 720, 60)

    app = create_app(data_dir=temp_data_dir, start_worker=False, realtime_synthetic=False)
    with TestClient(app) as client:
        # Start live preview at default 640x480 @ 30 FPS
        prev = client.get("/api/cameras/preview.jpg")
        assert prev.status_code == 200
        cam = app.state.bv.camera_manager.get_active()
        assert cam.is_streaming_at(640, 480, 30)

        # Now record at 1280x720 @ 60 FPS with an ROI crop of [212, 237, 946, 400]
        rec = client.post(
            "/api/record/start",
            json={
                "title": "High-Speed 60 FPS ROI Crop",
                "width": 1280,
                "height": 720,
                "fps": 60,
                "duration_s": 0.5,
                "storage_format": "npz",
                "roi": [212, 237, 946, 400],
            },
        ).json()
        assert rec["width"] == 946
        assert rec["height"] == 400
        assert rec["target_fps"] == 60
        assert abs(rec["achieved_fps"] - 60.0) < 1.5
        assert cam.is_streaming_at(1280, 720, 60)


def test_record_stop_endpoint_and_ui_integrity(temp_data_dir: Path) -> None:
    app = create_app(data_dir=temp_data_dir, start_worker=False, realtime_synthetic=False)
    with TestClient(app) as client:
        stop_res = client.post("/api/record/stop")
        assert stop_res.status_code == 200
        assert stop_res.json() == {"stopping": False, "is_recording": False}

        jobs_res = client.get("/api/jobs")
        assert jobs_res.status_code == 200
        jobs_data = jobs_res.json()
        assert "jobs" in jobs_data
        assert "hardware_thermal" in jobs_data
        assert "cpu_temp_c" in jobs_data["hardware_thermal"]

        html = client.get("/").text
        js = client.get("/static/app.js").text
        assert 'id="upload-video-file"' in html
        assert "upload-video-file" in js
        assert "upload-file-input" not in js
        assert 'id="trig-status-badge"' in html
        assert "trig-status-badge" in js
        assert "trigger-status-badge" not in js
        assert "$X(t)$" not in html
        assert "$X_{\\text" not in html
        assert "'banner warn'" not in js


def test_schlieren_slit_scan_stills_and_full_ui(temp_data_dir: Path) -> None:
    app = create_app(data_dir=temp_data_dir, start_worker=False, realtime_synthetic=False)
    with TestClient(app) as client:
        sel = client.post(
            "/api/cameras/select",
            json={"camera_id": "synthetic:0", "complete_setup": True},
        )
        assert sel.status_code == 200

        for mode in ("schlieren", "slit_scan", "slit_scan_h"):
            frame_res = client.get(f"/api/cameras/preview.jpg?mode={mode}")
            assert frame_res.status_code == 200
            assert frame_res.headers["content-type"] == "image/jpeg"
            assert frame_res.content.startswith(b"\xff\xd8")

        demos_res = client.post("/api/library/demo-clips")
        assert demos_res.status_code == 200
        clips = demos_res.json()["clips"]
        assert len(clips) >= 1
        clip_id = clips[0]["clip_id"]

        for style in ("chrono_paint", "strobe_ghost", "median_bg", "motion_trails"):
            still_res = client.get(
                f"/api/library/{clip_id}/still?style={style}&sensitivity=2.0&strobe_count=5"
            )
            assert still_res.status_code == 200
            assert still_res.headers["content-type"] == "image/png"
            assert still_res.content.startswith(b"\x89PNG\r\n\x1a\n")

        dl_res = client.get(f"/api/library/{clip_id}/still?style=chrono_paint&download=true")
        assert dl_res.status_code == 200
        assert "attachment;" in dl_res.headers.get("content-disposition", "")

        bad_res = client.get(f"/api/library/{clip_id}/still?style=invalid")
        assert bad_res.status_code == 400

        html = client.get("/").text
        for token in (
            "stills-modal",
            "schlieren",
            "slit_scan",
            "slit_scan_h",
        ):
            assert token in html
        for hidden_mode_token in (
            "btn-mode-studio",
            "btn-mode-lab",
            "ui-mode-studio",
            "lab-only",
        ):
            assert hidden_mode_token not in html

        repo_root = Path(__file__).resolve().parents[1]
        assert (repo_root / "LICENSE").exists()


def test_clip_rename_notes_and_quick_capture_presets(temp_data_dir: Path) -> None:
    import json

    app = create_app(data_dir=temp_data_dir, start_worker=False, realtime_synthetic=False)
    with TestClient(app) as client:
        demos_res = client.post("/api/library/demo-clips")
        assert demos_res.status_code == 200
        clip_id = demos_res.json()["clips"][0]["clip_id"]

        patch_res = client.patch(
            f"/api/library/{clip_id}",
            json={"title": "Cooling Fan 1800 RPM", "notes": "Rigid tripod at 45 cm"},
        )
        assert patch_res.status_code == 200
        body = patch_res.json()
        assert body["title"] == "Cooling Fan 1800 RPM"
        assert body["notes"] == "Rigid tripod at 45 cm"

        fetched = client.get(f"/api/library/{clip_id}")
        assert fetched.status_code == 200
        assert fetched.json()["title"] == "Cooling Fan 1800 RPM"
        assert fetched.json()["notes"] == "Rigid tripod at 45 cm"

        disk_meta = json.loads(
            (temp_data_dir / "recordings" / clip_id / "meta.json").read_text(encoding="utf-8")
        )
        assert disk_meta["title"] == "Cooling Fan 1800 RPM"
        assert disk_meta["notes"] == "Rigid tripod at 45 cm"

        empty_res = client.patch(f"/api/library/{clip_id}", json={"title": "   "})
        assert empty_res.status_code == 400

        missing_res = client.patch("/api/library/no-such-clip", json={"title": "Test"})
        assert missing_res.status_code == 404

        html = client.get("/").text
        assert "capture-quick-presets" in html
        assert "applyCaptureQuickPreset" in html
        js = client.get("/static/app.js").text
        assert "saveClipMetadata" in js


