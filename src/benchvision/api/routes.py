"""FastAPI API routes for cameras, recording, library, processing jobs, analysis, and system."""

from __future__ import annotations

import contextlib
import json
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

from benchvision import SCHEMA_VERSION, __version__
from benchvision.analysis.spectrum import compute_roi_spectrum_analysis
from benchvision.core.config import FrequencyPreset, save_settings
from benchvision.core.logging import read_recent_logs
from benchvision.processing import JobParams, estimate_processing_time_s, list_processing_methods
from benchvision.recording.recorder import load_clip_frames_and_timestamps
from benchvision.recording.validators import (
    estimate_recording_storage,
    validate_nyquist_and_duration,
)

router = APIRouter(prefix="/api")


class SelectCameraRequest(BaseModel):
    camera_id: str
    complete_setup: bool = True


class LockControlsRequest(BaseModel):
    width: int = 640
    height: int = 480
    fps: int = 30
    exposure_us: int = 10000
    gain: float = 1.0
    wb_temp_k: int = 4600
    focus_position: float = 0.0
    roi: list[int] | None = None


class PreflightRequest(BaseModel):
    width: int = 640
    height: int = 480
    fps: float = 30.0
    duration_s: float = 8.0
    min_freq_hz: float = 0.8
    max_freq_hz: float = 3.0
    storage_format: str = "mjpeg_q100"
    roi: list[int] | None = None


class StartRecordingRequest(BaseModel):
    title: str = "Vibration Capture"
    duration_s: float = 5.0
    width: int = 640
    height: int = 480
    fps: int = 30
    storage_format: str = "mjpeg_q100"
    roi: list[int] | None = None
    controls: dict[str, Any] | None = None


class SubmitJobRequest(BaseModel):
    clip_id: str
    params: JobParams
    run_async: bool = True


@router.get("/health")
def health_check(request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    return {
        "status": "ok",
        "version": __version__,
        "schema_version": SCHEMA_VERSION,
        "active_camera": state.settings.active_camera_id,
        "setup_completed": state.settings.setup_completed,
    }


@router.get("/cameras")
def get_cameras(request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    discovered = state.camera_manager.discover_all()
    active_cam = state.camera_manager.get_active()
    caps = state.camera_manager.get_capabilities(active_cam)
    return {
        "setup_completed": state.settings.setup_completed,
        "active_camera_id": active_cam.camera_id,
        "discovered": discovered,
        "capabilities": caps.model_dump(),
        "current_controls": active_cam.current_controls.model_dump(),
    }


@router.post("/cameras/select")
def select_camera(req: SelectCameraRequest, request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    caps = state.camera_manager.select_camera(req.camera_id)
    if req.complete_setup:
        state.settings.setup_completed = True
        save_settings(state.settings)
    return {
        "active_camera_id": state.settings.active_camera_id,
        "setup_completed": state.settings.setup_completed,
        "capabilities": caps.model_dump(),
    }


@router.post("/cameras/{camera_id:path}/lens")
def update_camera_lens(
    camera_id: str, lens_payload: dict[str, Any], request: Request
) -> dict[str, Any]:
    state = request.app.state.bv
    lens = state.camera_manager.update_lens_config(camera_id, lens_payload)
    return {"camera_id": camera_id, "lens": lens.model_dump()}


@router.post("/cameras/lock")
def lock_camera_settings(req: LockControlsRequest, request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    if req.roi and len(req.roi) == 4:
        state.preview_service.set_roi(
            (int(req.roi[0]), int(req.roi[1]), int(req.roi[2]), int(req.roi[3]))
        )
    else:
        state.preview_service.set_roi(None)

    locked = state.camera_manager.lock_active_controls(req.model_dump(exclude={"roi"}))
    return {"status": "locked", "controls": locked.model_dump()}


@router.get("/cameras/preview.jpg")
def preview_snapshot(request: Request) -> Response:
    state = request.app.state.bv
    jpg = state.preview_service.grab_preview_jpeg(draw_roi=True)
    return Response(
        content=jpg,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@router.get("/cameras/preview.mjpg")
def preview_stream(request: Request) -> StreamingResponse:
    state = request.app.state.bv
    return StreamingResponse(
        state.preview_service.mjpeg_generator(max_fps=15.0),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@router.get("/cameras/live-signal")
def get_live_roi_signal(request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    with contextlib.suppress(Exception):
        if not state.preview_service.get_live_signal():
            state.preview_service.grab_preview_jpeg(draw_roi=False)
    return {"points": state.preview_service.get_live_signal()}


@router.post("/record/preflight")
def record_preflight(req: PreflightRequest, request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    roi_tuple = (
        (req.roi[0], req.roi[1], req.roi[2], req.roi[3])
        if req.roi and len(req.roi) == 4
        else None
    )
    nyq = validate_nyquist_and_duration(
        fps=req.fps,
        max_freq_hz=req.max_freq_hz,
        min_freq_hz=req.min_freq_hz,
        duration_s=req.duration_s,
    )
    storage = estimate_recording_storage(
        width=req.width,
        height=req.height,
        fps=req.fps,
        duration_s=req.duration_s,
        storage_format=req.storage_format,
        data_dir=state.layout.root,
        roi=roi_tuple,
    )
    return {"nyquist": nyq, "storage": storage}


@router.post("/record/start")
def start_recording(req: StartRecordingRequest, request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    roi_tuple = (
        (req.roi[0], req.roi[1], req.roi[2], req.roi[3])
        if req.roi and len(req.roi) == 4
        else None
    )
    try:
        meta = state.recording_service.record_clip(
            title=req.title,
            duration_s=req.duration_s,
            width=req.width,
            height=req.height,
            fps=req.fps,
            storage_format=req.storage_format,
            roi=roi_tuple,
            controls=req.controls,
        )
        return meta
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/record/status")
def recording_status(request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    return {
        "is_recording": state.recording_service.is_recording,
        "current_clip_id": state.recording_service.current_clip_id,
        "progress": state.recording_service.progress,
    }


@router.get("/presets")
def get_presets_and_methods(request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    return {
        "presets": [p.model_dump() for p in state.settings.presets],
        "methods": list_processing_methods(),
    }


@router.post("/presets")
def save_preset(preset: FrequencyPreset, request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    updated = [p for p in state.settings.presets if p.id != preset.id]
    updated.append(preset)
    state.settings.presets = updated
    save_settings(state.settings)
    return {"presets": [p.model_dump() for p in state.settings.presets]}


@router.get("/library")
def list_library_clips(request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    return {"clips": state.db.list_recordings()}


@router.get("/library/{clip_id}")
def get_clip_details(clip_id: str, request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    clip = state.db.get_recording(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail=f"Clip '{clip_id}' not found")
    return clip


@router.delete("/library/{clip_id}")
def delete_clip(clip_id: str, request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    clip_dir = state.layout.clip_dir(clip_id)
    deleted = state.db.delete_recording(clip_id)
    if clip_dir.exists():
        shutil.rmtree(clip_dir, ignore_errors=True)
    return {"deleted": deleted, "clip_id": clip_id}


@router.get("/library/{clip_id}/auto-band")
def auto_find_clip_band(
    clip_id: str,
    request: Request,
    low_hz: float = Query(default=0.2),
    high_hz: float = Query(default=12.0),
) -> dict[str, Any]:
    state = request.app.state.bv
    clip = state.db.get_recording(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail=f"Clip '{clip_id}' not found")
    clip_dir = state.layout.clip_dir(clip_id)
    frames, _ts, fps = load_clip_frames_and_timestamps(clip_dir, max_width=320)
    return compute_roi_spectrum_analysis(
        frames_bgr=frames, fps=fps, low_hz=low_hz, high_hz=high_hz
    )


@router.get("/library/{clip_id}/files/{filename}")
def get_clip_file(clip_id: str, filename: str, request: Request) -> FileResponse:
    state = request.app.state.bv
    clip_dir = state.layout.clip_dir(clip_id)
    safe_file = state.layout.resolve_safe_path(
        clip_dir.relative_to(state.layout.root) / Path(filename).name
    )
    if not safe_file.exists():
        raise HTTPException(status_code=404, detail=f"File '{filename}' not found")
    return FileResponse(safe_file)


@router.get("/jobs")
def list_all_jobs(request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    return {"jobs": state.db.list_jobs()}


@router.post("/jobs")
def submit_processing_job(req: SubmitJobRequest, request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    try:
        return state.job_manager.submit_job(
            clip_id=req.clip_id, params=req.params, run_async=req.run_async
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/jobs/estimate")
def estimate_job_duration(
    frame_count: int = 150,
    width: int = 640,
    height: int = 480,
    method: str = "eulerian",
) -> dict[str, Any]:
    est = estimate_processing_time_s(frame_count, width, height, method)
    return {"estimated_seconds": est}


@router.get("/jobs/{job_id}")
def get_job_status(job_id: str, request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    job = state.db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return job


@router.post("/jobs/{job_id}/cancel")
def cancel_processing_job(job_id: str, request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    job = state.job_manager.cancel_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return job


@router.get("/jobs/{job_id}/spectrum")
def get_job_spectrum(job_id: str, request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    job = state.db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    res_dir = state.layout.job_result_dir(job["clip_id"], job_id)
    spec_path = res_dir / "spectrum.json"
    if not spec_path.exists():
        raise HTTPException(status_code=404, detail="Spectrum analysis not yet available")
    return json.loads(spec_path.read_text(encoding="utf-8"))


@router.get("/jobs/{job_id}/files/{filename}")
def get_job_output_file(job_id: str, filename: str, request: Request) -> FileResponse:
    state = request.app.state.bv
    job = state.db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    res_dir = state.layout.job_result_dir(job["clip_id"], job_id)
    safe_file = state.layout.resolve_safe_path(
        res_dir.relative_to(state.layout.root) / Path(filename).name
    )
    if not safe_file.exists():
        raise HTTPException(status_code=404, detail=f"Output file '{filename}' not found")
    return FileResponse(safe_file, filename=Path(filename).name)


@router.get("/compare/{clip_id}")
def compare_clip_methods(
    clip_id: str,
    request: Request,
    job_ids: str | None = Query(default=None),
) -> dict[str, Any]:
    state = request.app.state.bv
    ids = [x.strip() for x in job_ids.split(",") if x.strip()] if job_ids else None
    return state.job_manager.compare_jobs(clip_id, job_ids=ids)


@router.get("/system")
def get_system_status(request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    disk = state.layout.disk_usage()
    logs = read_recent_logs(state.layout.root, max_lines=100)
    return {
        "current_version": __version__,
        "latest_version": __version__,
        "update_available": False,
        "schema_version": SCHEMA_VERSION,
        "changelog": [
            {
                "version": "0.1.0",
                "date": "2026-11-05",
                "notes": [
                    "Unified CameraBackend plugin architecture (USB V4L2, Picamera2 CSI, Synthetic Oscillator)",
                    "Locked exposure/gain/WB/focus capture with nanosecond timestamps and Nyquist check",
                    "Offline magnification plugins: Linear Eulerian, Riesz Pyramid, Complex Steerable Phase, Optical Flow, rPPG",
                    "Welch PSD, FFT, STFT Spectrogram, Auto-Find dominant band, and side-by-side comparison",
                ],
            }
        ],
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python_version": platform.python_version(),
        },
        "data_dir": str(state.layout.root),
        "disk": disk,
        "logs": logs,
    }


@router.post("/system/update")
def trigger_manual_update() -> dict[str, Any]:
    update_script = Path(__file__).resolve().parents[3] / "scripts" / "update.sh"
    if not update_script.exists():
        return {"status": "skipped", "message": "scripts/update.sh not found"}
    try:
        out = subprocess.check_output(
            ["bash", str(update_script), "--check-only"],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=10.0,
        )
        return {"status": "ok", "output": out.strip()}
    except subprocess.SubprocessError as exc:
        return {"status": "error", "message": str(exc)}
