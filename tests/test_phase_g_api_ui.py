"""Phase (g) tests: FastAPI endpoints and static UI serving."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from benchvision.main import create_app


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

        # 3. Lock controls & preflight check
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

        comp = client.get(f"/api/compare/{clip_id}").json()
        assert comp["job_count"] == 1

        sys_info = client.get("/api/system").json()
        assert sys_info["current_version"] == "0.1.0"
        assert sys_info["disk"]["free_bytes"] > 0
