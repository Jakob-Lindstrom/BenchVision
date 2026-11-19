"""FastAPI API routes for cameras, recording, library, processing jobs, analysis, and system."""

from __future__ import annotations

import contextlib
import gc
import json
import platform
import re
import shutil
import tempfile
import threading
import time
from collections.abc import Generator
from pathlib import Path
from typing import Annotated, Any

import cv2
import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from src import SCHEMA_VERSION, __version__
from src.analysis.spectrum import (
    compute_2d_motion_orbit,
    compute_dual_roi_phase,
    compute_kymograph,
    compute_lighting_aliases,
    compute_roi_spectrum_analysis,
    compute_stft_spectrogram,
)
from src.core.config import FrequencyPreset, save_settings
from src.core.hardware import (
    compute_mm_per_pixel,
    detect_hardware_profile,
    evaluate_dual_camera_tier,
    read_pi_thermal_telemetry,
)
from src.core.logging import read_recent_logs
from src.core.updater import (
    apply_github_release_update,
    check_github_release,
    get_cached_release_check,
    get_github_repo,
)
from src.processing import JobParams, estimate_processing_time_s, list_processing_methods
from src.processing.frontier import (
    compute_dmd_modal_xray,
    compute_fluid_vorticity_lic,
    compute_optical_strain_gauge,
    compute_rolling_shutter_analysis,
    synthesize_rolling_shutter_wav,
)
from src.processing.stills import render_motion_still
from src.recording.recorder import (
    fix_v4l2m2m_mp4_bytes,
    get_clip_frame_count,
    load_clip_frames_and_timestamps,
)
from src.recording.validators import (
    estimate_recording_storage,
    validate_nyquist_and_duration,
)

router = APIRouter(prefix="/api")
_HEAVY_ANALYSIS_LOCK = threading.Lock()


class SelectCameraRequest(BaseModel):
    camera_id: str
    complete_setup: bool = True
    dual_camera_enabled: bool | None = None
    secondary_camera_id: str | None = None
    dual_sync_mode: str | None = None


class LockControlsRequest(BaseModel):
    width: int = 640
    height: int = 480
    fps: int = 30
    exposure_us: int = 10000
    gain: float = 1.0
    wb_temp_k: int = 4600
    focus_position: float = 0.0
    roi: list[int] | None = None
    roi_b: list[int] | None = None


class PreflightRequest(BaseModel):
    width: int = 640
    height: int = 480
    fps: float = 30.0
    duration_s: float = 8.0
    min_freq_hz: float = 0.8
    max_freq_hz: float = 3.0
    storage_format: str = "mjpeg_q100"
    roi: list[int] | None = None
    roi_b: list[int] | None = None
    crop_preview: bool = False


class StartRecordingRequest(BaseModel):
    title: str = "Vibration Capture"
    duration_s: float = 8.0
    width: int = 640
    height: int = 480
    fps: int = 30
    storage_format: str = "mjpeg_q100"
    roi: list[int] | None = None
    roi_b: list[int] | None = None
    controls: dict[str, Any] | None = None
    include_preroll: bool = False
    preroll_seconds: float = 2.0
    dual_camera: bool | None = None


class RulerCalibrateRequest(BaseModel):
    pixel_distance: float = 0.0
    known_length_mm: float = 0.0
    frame_width_px: int = 640
    frame_width: int | None = None
    clear: bool = False


class SubmitJobRequest(BaseModel):
    clip_id: str
    params: JobParams
    run_async: bool = True


class UpdateClipMetadataRequest(BaseModel):
    title: str | None = Field(default=None, max_length=160)
    notes: str | None = Field(default=None, max_length=1000)


class TareFrameRequest(BaseModel):
    enable: bool = True


class DualRoiRequest(BaseModel):
    roi_a: list[int] | None = None
    roi_b: list[int] | None = None


class TrimClipRequest(BaseModel):
    start_s: float = Field(default=0.0, ge=0.0, le=600.0)
    end_s: float = Field(default=0.0, ge=0.0, le=600.0)


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
    hw = detect_hardware_profile()
    dual_tier = evaluate_dual_camera_tier(
        discovered,
        hw_profile=hw,
        cam_a_id=active_cam.camera_id,
        cam_b_id=state.settings.secondary_camera_id,
    )
    if not dual_tier["allowed"] and state.settings.dual_camera_enabled:
        state.camera_manager.configure_dual_camera(False)
    sec_caps = (
        state.camera_manager.get_capabilities(state.camera_manager.get_secondary()).model_dump()
        if state.settings.dual_camera_enabled
        else None
    )
    return {
        "setup_completed": state.settings.setup_completed,
        "active_camera_id": active_cam.camera_id,
        "dual_camera_enabled": state.settings.dual_camera_enabled,
        "secondary_camera_id": state.settings.secondary_camera_id,
        "dual_sync_mode": state.settings.dual_sync_mode,
        "secondary_capabilities": sec_caps,
        "dual_tier": dual_tier,
        "discovered": discovered,
        "capabilities": caps.model_dump(),
        "current_controls": active_cam.current_controls.model_dump(),
        "hardware": hw,
    }


@router.post("/cameras/select")
def select_camera(req: SelectCameraRequest, request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    caps = state.camera_manager.select_camera(req.camera_id)
    if req.dual_camera_enabled is not None:
        discovered = state.camera_manager.discover_all()
        dual_tier = evaluate_dual_camera_tier(
            discovered,
            cam_a_id=req.camera_id,
            cam_b_id=req.secondary_camera_id or state.settings.secondary_camera_id,
        )
        eff_enable = bool(req.dual_camera_enabled) and bool(dual_tier["allowed"])
        state.camera_manager.configure_dual_camera(
            enabled=eff_enable,
            secondary_camera_id=req.secondary_camera_id,
            sync_mode=req.dual_sync_mode or state.settings.dual_sync_mode,
        )
    if req.complete_setup:
        state.settings.setup_completed = True
        save_settings(state.settings)
    return {
        "active_camera_id": state.settings.active_camera_id,
        "dual_camera_enabled": state.settings.dual_camera_enabled,
        "secondary_camera_id": state.settings.secondary_camera_id,
        "dual_sync_mode": state.settings.dual_sync_mode,
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
            (int(req.roi[0]), int(req.roi[1]), int(req.roi[2]), int(req.roi[3])),
            target_size=(req.width, req.height),
        )
    else:
        state.preview_service.set_roi(None, target_size=(req.width, req.height))

    locked = state.camera_manager.lock_active_controls(req.model_dump(exclude={"roi"}))
    return {"status": "locked", "controls": locked.model_dump()}


@router.post("/cameras/unlock")
def unlock_camera_settings(request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    unlocked = state.camera_manager.unlock_active_controls()
    return {"status": "unlocked", "controls": unlocked.model_dump()}


@router.post("/cameras/tare")
def set_camera_tare_frame(req: TareFrameRequest, request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    return state.preview_service.set_tare_frame(enable=req.enable)


@router.post("/cameras/calibrate-ruler")
def calibrate_camera_ruler(
    req: RulerCalibrateRequest, request: Request
) -> dict[str, Any]:
    state = request.app.state.bv
    active_cam = state.camera_manager.get_active()
    cam_id = active_cam.camera_id
    if req.clear:
        lens = state.camera_manager.update_lens_config(
            cam_id, {"ruler_mm_per_pixel": 0.0}
        )
        lens.ruler_mm_per_pixel = None
        state.settings.camera_lenses[cam_id] = lens
        save_settings(state.settings)
        mm_px = compute_mm_per_pixel(
            lens.target_distance_cm, lens.fov_degrees, req.frame_width_px
        )
        return {
            "camera_id": cam_id,
            "calibration_mode": "pinhole_fov",
            "mm_per_pixel": round(mm_px, 5),
            "lens": lens.model_dump(),
        }

    if req.pixel_distance <= 1.0 or req.known_length_mm <= 0.0:
        raise HTTPException(
            status_code=400,
            detail="pixel_distance must be > 1 px and known_length_mm must be > 0 mm",
        )
    mm_per_px = float(req.known_length_mm) / float(req.pixel_distance)
    ref_w = max(16, int(req.frame_width or req.frame_width_px or 640))
    lens = state.camera_manager.update_lens_config(
        cam_id,
        {
            "ruler_mm_per_pixel": round(mm_per_px, 6),
            "ruler_ref_width_px": ref_w,
        },
    )
    return {
        "camera_id": cam_id,
        "calibration_mode": "ruler_2pt",
        "mm_per_pixel": round(mm_per_px, 5),
        "pixel_distance": round(req.pixel_distance, 2),
        "known_length_mm": round(req.known_length_mm, 3),
        "lens": lens.model_dump(),
    }


@router.get("/cameras/preview.jpg")
def preview_snapshot(
    request: Request,
    mode: str = "normal",
    shift: int = 2,
    gain: float = 1.0,
    strobe_hz: float = 0.0,
    assist: str = "off",
    cam_view: str = "auto",
) -> Response:
    state = request.app.state.bv
    jpg = state.preview_service.grab_preview_jpeg(
        draw_roi=True,
        mode=mode,
        shift_frames=shift,
        gain=gain,
        strobe_hz=strobe_hz,
        assist=assist,
        cam_view=cam_view,
    )
    return Response(
        content=jpg,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@router.get("/cameras/preview.mjpg")
def preview_stream(
    request: Request,
    mode: str = "normal",
    shift: int = 2,
    gain: float = 1.0,
    strobe_hz: float = 0.0,
    assist: str = "off",
    cam_view: str = "auto",
) -> StreamingResponse:
    state = request.app.state.bv
    return StreamingResponse(
        state.preview_service.mjpeg_generator(
            max_fps=15.0,
            mode=mode,
            shift_frames=shift,
            gain=gain,
            strobe_hz=strobe_hz,
            assist=assist,
            cam_view=cam_view,
        ),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@router.get("/cameras/live-signal")
def get_live_roi_signal(
    request: Request,
    threshold_sigma: float = Query(default=3.5),
    threshold_pct: float = Query(default=1.5),
) -> dict[str, Any]:
    state = request.app.state.bv
    with contextlib.suppress(Exception):
        if not state.preview_service.get_live_signal():
            state.preview_service.grab_preview_jpeg(draw_roi=False)
    return {
        "points": state.preview_service.get_live_signal(),
        "trigger": state.preview_service.evaluate_trigger_spike(
            threshold_sigma=threshold_sigma, threshold_pct=threshold_pct
        ),
    }


@router.post("/record/preflight")
def record_preflight(req: PreflightRequest, request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    dual_on = bool(state.settings.dual_camera_enabled)
    dual_tier = evaluate_dual_camera_tier(
        state.camera_manager.discover_all(),
        cam_a_id=state.settings.active_camera_id,
        cam_b_id=state.settings.secondary_camera_id,
    )
    eff_w, eff_h, eff_fps = req.width, req.height, req.fps
    capped_applied = False
    if dual_on and dual_tier["capped"] and (
        eff_w > dual_tier["max_width"] or eff_h > dual_tier["max_height"] or eff_fps > dual_tier["max_fps"]
    ):
        capped_applied = True
        eff_w = min(eff_w, int(dual_tier["max_width"]))
        eff_h = min(eff_h, int(dual_tier["max_height"]))
        eff_fps = min(eff_fps, float(dual_tier["max_fps"]))

    roi_tuple = (
        (int(req.roi[0]), int(req.roi[1]), int(req.roi[2]), int(req.roi[3]))
        if req.roi and len(req.roi) == 4
        else None
    )
    roi_b_tuple = (
        (int(req.roi_b[0]), int(req.roi_b[1]), int(req.roi_b[2]), int(req.roi_b[3]))
        if req.roi_b and len(req.roi_b) == 4
        else None
    )
    state.preview_service.set_roi(
        roi_tuple,
        crop_preview=req.crop_preview,
        target_size=(req.width, req.height),
        roi_b=roi_b_tuple,
    )
    if not state.recording_service.is_recording:
        cam = state.camera_manager.get_active()
        if getattr(cam, "backend_key", "") in ("v4l2", "picamera2") and not cam.is_streaming_at(
            eff_w, eff_h, int(eff_fps)
        ):
            with contextlib.suppress(RuntimeError):
                cam.start(width=eff_w, height=eff_h, fps=int(eff_fps))
        if dual_on:
            cam_b = state.camera_manager.get_secondary()
            if getattr(cam_b, "backend_key", "") in ("v4l2", "picamera2") and not cam_b.is_streaming_at(
                eff_w, eff_h, int(eff_fps)
            ):
                with contextlib.suppress(RuntimeError):
                    cam_b.start(width=eff_w, height=eff_h, fps=int(eff_fps))
    nyq = validate_nyquist_and_duration(
        fps=eff_fps,
        max_freq_hz=req.max_freq_hz,
        min_freq_hz=req.min_freq_hz,
        duration_s=req.duration_s,
    )
    if capped_applied:
        nyq["warnings"].append(
            f"Dual-Camera Tier 2 cap applied: {eff_w}x{eff_h} @ {int(eff_fps)} FPS to protect bus bandwidth."
        )
    storage = estimate_recording_storage(
        width=eff_w,
        height=eff_h,
        fps=eff_fps,
        duration_s=req.duration_s,
        storage_format=req.storage_format,
        data_dir=state.layout.root,
        roi=roi_tuple,
    )
    if dual_on:
        storage["estimated_mb"] = round(float(storage["estimated_mb"]) * 2.0, 2)
    dual_sync = state.camera_manager.measure_sync_delta(fps=eff_fps) if dual_on else {"enabled": False}
    return {
        "nyquist": nyq,
        "storage": storage,
        "active_roi": list(roi_tuple) if roi_tuple else None,
        "active_roi_b": list(roi_b_tuple) if roi_b_tuple else None,
        "dual_sync": dual_sync,
        "dual_tier": dual_tier,
        "effective_resolution": [eff_w, eff_h, eff_fps],
    }


@router.post("/record/start")
def start_recording(req: StartRecordingRequest, request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    roi_tuple = (
        (int(req.roi[0]), int(req.roi[1]), int(req.roi[2]), int(req.roi[3]))
        if req.roi and len(req.roi) == 4
        else None
    )
    roi_b_tuple = (
        (int(req.roi_b[0]), int(req.roi_b[1]), int(req.roi_b[2]), int(req.roi_b[3]))
        if req.roi_b and len(req.roi_b) == 4
        else None
    )
    state.preview_service.set_roi(
        roi_tuple,
        target_size=(req.width, req.height),
        roi_b=roi_b_tuple,
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
            roi_b=roi_b_tuple,
            controls=req.controls,
            include_preroll=req.include_preroll,
            preroll_seconds=req.preroll_seconds,
            dual_camera=req.dual_camera,
        )
        return meta
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/record/stop")
def stop_recording(request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    stopped = state.recording_service.request_stop()
    return {
        "stopping": stopped,
        "is_recording": state.recording_service.is_recording,
    }


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


@router.post("/library/demo-clips")
def generate_demo_clips(request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    clips = state.recording_service.generate_demo_showcase_clips()
    return {"status": "generated", "created_count": len(clips), "clips": clips}


@router.post("/library/upload")
async def upload_external_clip(
    request: Request,
    file: Annotated[UploadFile, File(...)],
    title: Annotated[str, Form()] = "",
    true_fps: Annotated[str, Form()] = "",
) -> dict[str, Any]:
    state = request.app.state.bv
    parsed_fps: float | None = None
    if true_fps and true_fps.strip():
        try:
            val = float(true_fps.strip())
            if val > 0.5:
                parsed_fps = val
        except ValueError:
            parsed_fps = None

    ext = Path(file.filename or "upload.mp4").suffix.lower()
    if ext not in (".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"):
        ext = ".mp4"
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp_fh:
            tmp_path = Path(tmp_fh.name)
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                tmp_fh.write(chunk)
        return state.recording_service.import_external_clip(
            filename=file.filename or "upload.mp4",
            title=title,
            true_fps_override=parsed_fps,
            source_path=tmp_path,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


@router.get("/library/{clip_id}")
def get_clip_details(clip_id: str, request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    clip = state.db.get_recording(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail=f"Clip '{clip_id}' not found")
    return clip


@router.patch("/library/{clip_id}")
def update_clip_metadata(
    clip_id: str, req: UpdateClipMetadataRequest, request: Request
) -> dict[str, Any]:
    state = request.app.state.bv
    clean_title: str | None = None
    if req.title is not None:
        clean_title = req.title.strip()
        if not clean_title:
            raise HTTPException(status_code=400, detail="Clip title cannot be empty")
    clean_notes = req.notes.strip() if req.notes is not None else None
    updated = state.db.update_recording_metadata(
        clip_id=clip_id, title=clean_title, notes=clean_notes
    )
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Clip '{clip_id}' not found")
    clip_dir = state.layout.clip_dir(clip_id)
    if clip_dir.exists():
        meta_disk = {k: v for k, v in updated.items() if k != "jobs"}
        with contextlib.suppress(OSError):
            (clip_dir / "meta.json").write_text(
                json.dumps(meta_disk, indent=2), encoding="utf-8"
            )
    return updated


@router.delete("/library/{clip_id}")
def delete_clip(clip_id: str, request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    clip_dir = state.layout.clip_dir(clip_id)
    deleted = state.db.delete_recording(clip_id)
    if clip_dir.exists():
        shutil.rmtree(clip_dir, ignore_errors=True)
    return {"deleted": deleted, "clip_id": clip_id}


@router.post("/library/{clip_id}/trim")
def trim_library_clip(
    clip_id: str, req: TrimClipRequest, request: Request
) -> dict[str, Any]:
    state = request.app.state.bv
    try:
        return state.recording_service.trim_clip(
            clip_id=clip_id, start_s=req.start_s, end_s=req.end_s
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/library/{clip_id}/still")
def render_clip_motion_still(
    clip_id: str,
    request: Request,
    style: str = Query(default="chrono_paint"),
    sensitivity: float = Query(default=1.0, ge=0.2, le=5.0),
    strobe_count: int = Query(default=8, ge=3, le=20),
    download: bool = Query(default=False),
) -> Response:
    state = request.app.state.bv
    clip = state.db.get_recording(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail=f"Clip '{clip_id}' not found")
    clip_dir = state.layout.clip_dir(clip_id)
    hw = detect_hardware_profile()
    budgets = hw.get("inspector_budgets") or {}
    max_w = int(budgets.get("still_max_width", 540))
    still_cap = int(budgets.get("still_max_frames", 48))
    n_total = get_clip_frame_count(clip_dir)
    keep_count = (
        still_cap
        if style in ("median_bg", "chrono_paint", "motion_trails")
        else max(24, min(still_cap, strobe_count + 16))
    )
    sel_idxs = (
        [int(i) for i in np.unique(np.linspace(0, n_total - 1, min(n_total, keep_count), dtype=int))]
        if n_total >= 2
        else None
    )
    with _HEAVY_ANALYSIS_LOCK:
        frames = None
        try:
            frames, _ts, _fps = load_clip_frames_and_timestamps(
                clip_dir, max_width=max_w, selected_indices=sel_idxs
            )
            still_bgr = render_motion_still(
                frames, style=style, sensitivity=sensitivity, strobe_count=strobe_count
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            del frames
            gc.collect()
    ok, enc = cv2.imencode(".png", still_bgr)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to encode PNG motion still")
    headers = {"Cache-Control": "no-store"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{clip_id}_{style}.png"'
    return Response(content=bytes(enc), media_type="image/png", headers=headers)


@router.get("/library/{clip_id}/frontier/dmd")
def frontier_dmd_modal_xray(
    clip_id: str,
    request: Request,
    max_modes: int = Query(default=6, ge=1, le=10),
) -> dict[str, Any]:
    state = request.app.state.bv
    clip = state.db.get_recording(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail=f"Clip '{clip_id}' not found")
    clip_dir = state.layout.clip_dir(clip_id)
    hw = detect_hardware_profile()
    tier = str(hw.get("performance_tier", "constrained"))
    budgets = hw.get("inspector_budgets") or {}
    max_dim = int(budgets.get("dmd_max_dim", 96))
    max_frames = int(budgets.get("dmd_max_frames", 90))
    max_rank = int(budgets.get("dmd_max_rank", 14))
    with _HEAVY_ANALYSIS_LOCK:
        frames = None
        try:
            frames, _ts, fps = load_clip_frames_and_timestamps(
                clip_dir, max_width=max_dim, max_frames=max_frames
            )
            res = compute_dmd_modal_xray(
                frames,
                fps=fps,
                max_modes=max_modes,
                max_dim=max_dim,
                max_frames=max_frames,
                max_rank=max_rank,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            del frames
            gc.collect()
    return {"clip_id": clip_id, "tier": tier, **res}


@router.get("/library/{clip_id}/frontier/fluid")
def frontier_fluid_vorticity(clip_id: str, request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    clip = state.db.get_recording(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail=f"Clip '{clip_id}' not found")
    clip_dir = state.layout.clip_dir(clip_id)
    hw = detect_hardware_profile()
    tier = str(hw.get("performance_tier", "constrained"))
    budgets = hw.get("inspector_budgets") or {}
    max_w = int(budgets.get("fluid_max_width", 240))
    pair_count = int(budgets.get("fluid_pairs", 10))
    n_total = get_clip_frame_count(clip_dir)
    if n_total >= 2:
        starts = np.unique(np.linspace(0, n_total - 2, min(n_total - 1, pair_count), dtype=int))
        pair_idxs: list[int] | None = sorted(
            {int(k) for s in starts for k in (s, min(int(s) + 1, n_total - 1))}
        )
    else:
        pair_idxs = None
    with _HEAVY_ANALYSIS_LOCK:
        frames = None
        try:
            frames, ts, fps = load_clip_frames_and_timestamps(
                clip_dir, max_width=max_w, selected_indices=pair_idxs
            )
            res = compute_fluid_vorticity_lic(
                frames, fps=fps, max_width=max_w, pair_count=pair_count, timestamps_s=ts
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            del frames
            gc.collect()
    return {"clip_id": clip_id, "tier": tier, **res}


@router.get("/library/{clip_id}/frontier/strain")
def frontier_optical_strain(clip_id: str, request: Request) -> dict[str, Any]:
    state = request.app.state.bv
    clip = state.db.get_recording(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail=f"Clip '{clip_id}' not found")
    clip_dir = state.layout.clip_dir(clip_id)
    hw = detect_hardware_profile()
    tier = str(hw.get("performance_tier", "constrained"))
    budgets = hw.get("inspector_budgets") or {}
    max_w = int(budgets.get("strain_max_width", 240))
    scan_frames = int(budgets.get("strain_scan_frames", 21))
    n_total = get_clip_frame_count(clip_dir)
    if n_total >= 2:
        ref_idxs = list(range(min(3, n_total)))
        scan_idxs = [
            int(i) for i in np.linspace(1, n_total - 1, min(n_total - 1, scan_frames), dtype=int)
        ]
        sel_idxs: list[int] | None = sorted(set(ref_idxs + scan_idxs))
    else:
        sel_idxs = None
    with _HEAVY_ANALYSIS_LOCK:
        frames = None
        try:
            frames, ts, fps = load_clip_frames_and_timestamps(
                clip_dir, max_width=max_w, selected_indices=sel_idxs
            )
            res = compute_optical_strain_gauge(frames, fps=fps, max_width=max_w, timestamps_s=ts)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            del frames
            gc.collect()
    return {"clip_id": clip_id, "tier": tier, **res}


@router.get("/library/{clip_id}/frontier/rolling-shutter")
def frontier_rolling_shutter(
    clip_id: str,
    request: Request,
    readout_ms: float = Query(default=16.0, ge=2.0, le=50.0),
) -> dict[str, Any]:
    state = request.app.state.bv
    clip = state.db.get_recording(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail=f"Clip '{clip_id}' not found")
    clip_dir = state.layout.clip_dir(clip_id)
    hw = detect_hardware_profile()
    tier = str(hw.get("performance_tier", "constrained"))
    budgets = hw.get("inspector_budgets") or {}
    max_w = int(budgets.get("rolling_max_width", 360))
    frame_cap = int(budgets.get("rolling_frames", 24))
    n_total = get_clip_frame_count(clip_dir)
    sel_idxs = (
        [
            int(i)
            for i in np.unique(np.linspace(0, n_total - 1, min(n_total, frame_cap), dtype=int))
        ]
        if n_total >= 2
        else None
    )
    with _HEAVY_ANALYSIS_LOCK:
        frames = None
        try:
            frames, _ts, fps = load_clip_frames_and_timestamps(
                clip_dir, max_width=max_w, selected_indices=sel_idxs
            )
            res = compute_rolling_shutter_analysis(
                frames, fps=fps, readout_ms=readout_ms, max_frames=frame_cap
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            del frames
            gc.collect()
    return {"clip_id": clip_id, "tier": tier, "readout_ms": readout_ms, **res}


@router.get("/library/{clip_id}/frontier/rolling-shutter.wav")
def frontier_rolling_shutter_wav(
    clip_id: str,
    request: Request,
    readout_ms: float = Query(default=16.0, ge=2.0, le=50.0),
) -> Response:
    state = request.app.state.bv
    clip = state.db.get_recording(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail=f"Clip '{clip_id}' not found")
    clip_dir = state.layout.clip_dir(clip_id)
    hw = detect_hardware_profile()
    budgets = hw.get("inspector_budgets") or {}
    max_w = int(budgets.get("rolling_max_width", 360))
    frame_cap = int(budgets.get("rolling_wav_frames", 32))
    n_total = get_clip_frame_count(clip_dir)
    sel_idxs = (
        [
            int(i)
            for i in np.unique(np.linspace(0, n_total - 1, min(n_total, frame_cap), dtype=int))
        ]
        if n_total >= 2
        else None
    )
    with _HEAVY_ANALYSIS_LOCK:
        frames = None
        try:
            frames, _ts, fps = load_clip_frames_and_timestamps(
                clip_dir, max_width=max_w, selected_indices=sel_idxs
            )
            wav_bytes = synthesize_rolling_shutter_wav(
                frames, fps=fps, readout_ms=readout_ms, max_frames=frame_cap
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            del frames
            gc.collect()
    headers = {
        "Content-Disposition": f'attachment; filename="{clip_id}_visual_mic.wav"',
        "Cache-Control": "no-store",
    }
    return Response(content=wav_bytes, media_type="audio/wav", headers=headers)


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
    hw = detect_hardware_profile()
    budgets = hw.get("inspector_budgets") or {}
    max_w = int(budgets.get("autoband_max_width", 240))
    max_frames = int(budgets.get("autoband_max_frames", 180))
    with _HEAVY_ANALYSIS_LOCK:
        frames = None
        try:
            frames, _ts, fps = load_clip_frames_and_timestamps(
                clip_dir, max_width=max_w, max_frames=max_frames
            )
            return compute_roi_spectrum_analysis(
                frames_bgr=frames, fps=fps, low_hz=low_hz, high_hz=high_hz
            )
        finally:
            del frames
            gc.collect()


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
    return {
        "jobs": state.db.list_jobs(),
        "hardware_thermal": read_pi_thermal_telemetry(),
    }


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
    import numpy as np

    state = request.app.state.bv
    job = state.db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    res_dir = state.layout.job_result_dir(job["clip_id"], job_id)
    spec_path = res_dir / "spectrum.json"
    if not spec_path.exists():
        raise HTTPException(status_code=404, detail="Spectrum analysis not yet available")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    ts = spec.get("time_series")
    if isinstance(ts, dict):
        for key in ("raw", "filtered"):
            arr = ts.get(key)
            if isinstance(arr, list) and len(arr) > 6:
                v = np.asarray(arr, dtype=np.float64).copy()
                v[:4] = v[4]
                if key == "raw":
                    v = v - np.mean(v)
                skip = max(4, int(np.ceil(len(v) * 0.05)))
                if skip < len(v):
                    p95 = float(np.percentile(np.abs(v[skip:]), 95))
                    if p95 > 1e-9:
                        v[:skip] = np.clip(v[:skip], -p95, p95)
                ts[key] = [round(float(x), 4) for x in v]
        if "filtered" in ts:
            spec["filtered_signal"] = ts["filtered"]
        raw_arr = ts.get("raw")
        t_arr = ts.get("t_s")
        if isinstance(raw_arr, list) and len(raw_arr) >= 8:
            dur_s = (
                float(t_arr[-1] - t_arr[0])
                if (isinstance(t_arr, list) and len(t_arr) >= 2 and float(t_arr[-1] - t_arr[0]) > 0)
                else float(len(raw_arr) / 30.0)
            )
            fps_est = float(max(1.0, (len(raw_arr) - 1) / max(dur_s, 1e-3)))
            spec_dirty = False
            if not spec.get("spectrogram"):
                spec["spectrogram"] = compute_stft_spectrogram(
                    np.asarray(raw_arr, dtype=np.float64),
                    fps=fps_est,
                    duration_s=dur_s,
                )
                spec_dirty = True
            dom_hz = float(spec.get("dominant_hz") or 0.0)
            if "lighting_aliases" not in spec:
                spec["lighting_aliases"] = compute_lighting_aliases(
                    fps=fps_est, dominant_hz=dom_hz
                )
                spec_dirty = True
            cal = spec.get("calibration")
            if isinstance(cal, dict):
                disp_mm = float(cal.get("peak_displacement_mm") or 0.0)
                if "velocity_rms_mm_s" not in cal:
                    cal["velocity_rms_mm_s"] = round(
                        float((2.0 * np.pi * dom_hz * disp_mm) / np.sqrt(2.0)), 4
                    )
                    spec_dirty = True
                if "peak_accel_g" not in cal:
                    cal["peak_accel_g"] = round(
                        float((((2.0 * np.pi * dom_hz) ** 2) * disp_mm) / 9806.65), 5
                    )
                    spec_dirty = True
            if "orbit_2d" not in spec:
                clip_dir = state.layout.clip_dir(job["clip_id"])
                params = job.get("params") or {}
                with contextlib.suppress(Exception):
                    frames_o, _ts_o, fps_o = load_clip_frames_and_timestamps(
                        clip_dir,
                        roi=params.get("roi"),
                        max_width=params.get("max_width", 640),
                        trim_start_s=float(params.get("trim_start_s", 0.0)),
                        trim_end_s=float(params.get("trim_end_s", 0.0)),
                    )
                    mm_px = float((cal or {}).get("mm_per_pixel") or 0.25)
                    spec["orbit_2d"] = compute_2d_motion_orbit(
                        frames_o,
                        fps=fps_o,
                        low_hz=float(params.get("low_hz", 0.8)),
                        high_hz=float(params.get("high_hz", 3.0)),
                        epicenter=spec.get("epicenter"),
                        mm_per_pixel=mm_px,
                    )
                    spec_dirty = True
            if spec_dirty:
                with contextlib.suppress(OSError):
                    spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return spec


@router.get("/jobs/{job_id}/kymograph")
def get_job_kymograph(
    job_id: str,
    request: Request,
    axis: str = Query(default="horizontal"),
    pos: float | None = Query(default=None),
) -> dict[str, Any]:
    state = request.app.state.bv
    job = state.db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    clip_dir = state.layout.clip_dir(job["clip_id"])
    params = job.get("params") or {}
    frames, _ts, _fps = load_clip_frames_and_timestamps(
        clip_dir, roi=params.get("roi"), max_width=params.get("max_width", 640)
    )
    return compute_kymograph(frames, axis=axis, line_pos=pos)


@router.post("/jobs/{job_id}/dual-roi")
def analyze_job_dual_roi(
    job_id: str, req: DualRoiRequest, request: Request
) -> dict[str, Any]:
    from src.jobs.manager import overlay_dual_roi_boxes_on_bgr

    state = request.app.state.bv
    job = state.db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    clip_dir = state.layout.clip_dir(job["clip_id"])
    res_dir = state.layout.job_result_dir(job["clip_id"], job_id)
    params = job.get("params") or {}
    frames, _ts, fps = load_clip_frames_and_timestamps(
        clip_dir,
        roi=None,
        max_width=params.get("max_width", 640),
        trim_start_s=float(params.get("trim_start_s", 0.0)),
        trim_end_s=float(params.get("trim_end_s", 0.0)),
    )
    proc_h, proc_w = frames.shape[1:3]
    meta_path = clip_dir / "meta.json"
    clip_meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    src_w = int(clip_meta.get("width") or proc_w)
    src_h = int(clip_meta.get("height") or proc_h)

    def _to_proc_box(box: list[int] | None) -> tuple[int, int, int, int] | None:
        if not box or len(box) != 4:
            return None
        sx = float(proc_w) / float(max(1, src_w))
        sy = float(proc_h) / float(max(1, src_h))
        return (
            round(box[0] * sx),
            round(box[1] * sy),
            max(8, round(box[2] * sx)),
            max(8, round(box[3] * sy)),
        )

    def _to_src_box(box: list[int] | None) -> list[int]:
        if not box or len(box) != 4:
            return [0, 0, 16, 16]
        sx = float(src_w) / float(max(1, proc_w))
        sy = float(src_h) / float(max(1, proc_h))
        return [
            round(box[0] * sx),
            round(box[1] * sy),
            max(8, round(box[2] * sx)),
            max(8, round(box[3] * sy)),
        ]

    dual = compute_dual_roi_phase(
        frames,
        fps=fps,
        low_hz=float(params.get("low_hz", 0.8)),
        high_hz=float(params.get("high_hz", 3.0)),
        roi_a=_to_proc_box(req.roi_a),
        roi_b=_to_proc_box(req.roi_b),
    )
    dual["roi_a"] = req.roi_a if (req.roi_a and len(req.roi_a) == 4) else _to_src_box(dual.get("roi_a"))
    dual["roi_b"] = req.roi_b if (req.roi_b and len(req.roi_b) == 4) else _to_src_box(dual.get("roi_b"))

    spec_file = res_dir / "spectrum.json"
    if spec_file.exists():
        with contextlib.suppress(Exception):
            spec = json.loads(spec_file.read_text(encoding="utf-8"))
            spec["dual_roi"] = dual
            spec_file.write_text(json.dumps(spec, indent=2), encoding="utf-8")

    if job.get("result") and isinstance(job["result"], dict):
        extra = job["result"].setdefault("extra_metrics", {})
        extra["dual_roi"] = dual
        state.db.upsert_job(job)

    base_path = res_dir / "report_base.jpg"
    base_img = cv2.imread(str(base_path)) if base_path.exists() else None
    if base_img is None and (res_dir / "heatmap.mp4").exists():
        cap = cv2.VideoCapture(str(res_dir / "heatmap.mp4"))
        total_f = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 1)
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, total_f // 2))
        ok, frm = cap.read()
        cap.release()
        if ok and frm is not None:
            base_img = frm
    if base_img is not None:
        updated_report = overlay_dual_roi_boxes_on_bgr(
            base_img, dual, src_shape=(src_h, src_w)
        )
        cv2.imwrite(str(res_dir / "report_frame.jpg"), updated_report)

    return dual


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
        if safe_file.name == "original_share.mp4":
            clip_preview = state.layout.clip_dir(job["clip_id"]) / "preview.mp4"
            if clip_preview.exists():
                safe_file = clip_preview
        if safe_file.name == "report_frame.jpg" and (res_dir / "heatmap.mp4").exists():
            cap = cv2.VideoCapture(str(res_dir / "heatmap.mp4"))
            total_f = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 1)
            cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, total_f // 2))
            ok, frm = cap.read()
            cap.release()
            if ok and frm is not None:
                cv2.imwrite(str(safe_file), frm)
        if not safe_file.exists():
            # Graceful fallback for older jobs completed before phase_ripple / quiver_vectors were added
            fallback = res_dir / "heatmap.mp4"
            if fallback.exists():
                safe_file = fallback
            else:
                raise HTTPException(status_code=404, detail=f"Output file '{filename}' not found")
    if safe_file.suffix.lower() == ".mp4":
        raw_b = safe_file.read_bytes()
        fixed_b = fix_v4l2m2m_mp4_bytes(raw_b)
        if fixed_b != raw_b:
            safe_file.write_bytes(fixed_b)
    media_type = "video/mp4" if safe_file.suffix.lower() == ".mp4" else None
    return FileResponse(safe_file, media_type=media_type)


@router.get("/jobs/{job_id}/gif")
def export_job_looping_gif(
    job_id: str,
    request: Request,
    mode: str = Query(default="comparison_sbs_mp4"),
) -> FileResponse:
    """Render and download a palette-optimized looping GIF89a of the selected job output video."""
    from src.jobs.manager import encode_looping_gif

    state = request.app.state.bv
    job = state.db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    res_dir = state.layout.job_result_dir(job["clip_id"], job_id)
    file_map = {
        "comparison_sbs_mp4": "comparison_sbs.mp4",
        "dual_sync_mp4": "dual_sync.mp4",
        "split_wipe": "magnified_share.mp4",
        "magnified_mp4": "magnified_share.mp4",
        "heatmap_mp4": "heatmap.mp4",
        "phase_ripple_mp4": "phase_ripple.mp4",
        "quiver_vectors_mp4": "quiver_vectors.mp4",
    }
    src_name = file_map.get(mode, "comparison_sbs.mp4")
    src_path = res_dir / src_name
    if src_name == "magnified_share.mp4" and (res_dir / "magnified_lossless.avi").exists():
        src_path = res_dir / "magnified_lossless.avi"
    elif not src_path.exists():
        src_path = res_dir / "heatmap.mp4"
    if not src_path.exists():
        raise HTTPException(status_code=404, detail="Processed video not found for GIF export")

    gif_path = res_dir / f"{Path(src_name).stem}.gif"
    if not gif_path.exists():
        cap = cv2.VideoCapture(str(src_path))
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
        frames: list[Any] = []
        while len(frames) < 90:
            ok, frm = cap.read()
            if not ok or frm is None:
                break
            frames.append(frm)
        cap.release()
        if not frames:
            raise HTTPException(status_code=500, detail="Could not decode frames for GIF export")
        encode_looping_gif(frames, fps=fps, out_path=gif_path, max_width=360, max_frames=54)

    return FileResponse(
        gif_path,
        media_type="image/gif",
        filename=f"{job_id}_{Path(src_name).stem}.gif",
    )


@router.get("/jobs/{job_id}/stream/{filename}")
def stream_job_output_mjpeg(
    job_id: str,
    filename: str,
    request: Request,
    speed: float = Query(default=1.0, ge=0.05, le=4.0),
    strobe_hz: float = Query(default=0.0, ge=0.0, le=120.0),
) -> StreamingResponse:
    """Stream any processed job video file as a looping MJPEG stream with slow-mo and stroboscope support."""
    state = request.app.state.bv
    job = state.db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    res_dir = state.layout.job_result_dir(job["clip_id"], job_id)
    safe_file = state.layout.resolve_safe_path(
        res_dir.relative_to(state.layout.root) / Path(filename).name
    )
    if not safe_file.exists():
        if safe_file.name == "original_share.mp4":
            clip_preview = state.layout.clip_dir(job["clip_id"]) / "preview.mp4"
            if clip_preview.exists():
                safe_file = clip_preview
        if not safe_file.exists():
            fallback = res_dir / "heatmap.mp4"
            if fallback.exists():
                safe_file = fallback
            else:
                raise HTTPException(status_code=404, detail=f"Output file '{filename}' not found")
    if safe_file.name == "magnified_share.mp4" and (res_dir / "magnified_lossless.avi").exists():
        safe_file = res_dir / "magnified_lossless.avi"
    elif safe_file.suffix.lower() == ".mp4":
        raw_b = safe_file.read_bytes()
        fixed_b = fix_v4l2m2m_mp4_bytes(raw_b)
        if fixed_b != raw_b:
            safe_file.write_bytes(fixed_b)

    def _mjpeg_loop() -> Generator[bytes, Any, None]:
        while True:
            cap = cv2.VideoCapture(str(safe_file))
            fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
            base_fps = max(min(fps, 30.0), 5.0)
            interval = 1.0 / max(base_fps * float(speed), 1.0)
            strobe_step = max(1, round(fps / max(strobe_hz, 0.5))) if strobe_hz > 0 else 1
            frames_sent = 0
            frame_idx = 0
            held_bgr = None
            try:
                while True:
                    t0 = time.monotonic()
                    ok, bgr = cap.read()
                    if not ok or bgr is None:
                        break
                    if strobe_step > 1:
                        if frame_idx % strobe_step == 0 or held_bgr is None:
                            held_bgr = bgr.copy()
                        bgr = held_bgr
                    frame_idx += 1
                    enc_ok, jpg = cv2.imencode(
                        ".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85]
                    )
                    if enc_ok:
                        data = bytes(jpg)
                        yield (
                            b"--frame\r\n"
                            b"Content-Type: image/jpeg\r\n"
                            + f"Content-Length: {len(data)}\r\n\r\n".encode("ascii")
                            + data
                            + b"\r\n"
                        )
                        frames_sent += 1
                    elapsed = time.monotonic() - t0
                    if elapsed < interval:
                        time.sleep(interval - elapsed)
            finally:
                cap.release()
            if frames_sent == 0:
                break

    return StreamingResponse(
        _mjpeg_loop(), media_type="multipart/x-mixed-replace; boundary=frame"
    )


@router.get("/compare/{clip_id}")
def compare_clip_methods(
    clip_id: str,
    request: Request,
    job_ids: str | None = Query(default=None),
) -> dict[str, Any]:
    state = request.app.state.bv
    ids = [x.strip() for x in job_ids.split(",") if x.strip()] if job_ids else None
    return state.job_manager.compare_jobs(clip_id, job_ids=ids)


def _load_release_changelog() -> list[dict[str, Any]]:
    """Parse release entries from CHANGELOG.md so /api/system stays synced automatically."""
    changelog_path = Path(__file__).resolve().parent.parent.parent / "CHANGELOG.md"
    entries: list[dict[str, Any]] = []
    if changelog_path.exists():
        current: dict[str, Any] | None = None
        for raw_line in changelog_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            hdr = re.match(r"^##\s+\[?([^\]\s]+)\]?\s*(?:-\s*(.+))?$", line)
            if hdr:
                if current is not None:
                    entries.append(current)
                current = {
                    "version": hdr.group(1),
                    "date": (hdr.group(2) or "").strip(),
                    "notes": [],
                }
            elif line.startswith("- ") and current is not None:
                clean_note = line[2:].replace("**", "").strip()
                if clean_note:
                    current["notes"].append(clean_note)
        if current is not None:
            entries.append(current)

    if entries:
        return entries

    return [
        {
            "version": __version__,
            "date": "2026-11-19",
            "notes": [
                "Clip Deep Inspector (DMD vibrations, Airflow LIC, Flex Stress, Rolling-Shutter Mic)",
                "Full 4-tier Raspberry Pi 5 (8 GB ULTRA) & Pi 4 hardware scaling",
            ],
        },
        {
            "version": "0.1.0",
            "date": "2026-11-05",
            "notes": [
                "Initial CameraBackend plugin architecture and offline magnification suite",
            ],
        },
    ]


@router.get("/system")
def get_system_status(
    request: Request,
    check_remote: bool = Query(default=False),
    force_refresh: bool = Query(default=False),
) -> dict[str, Any]:
    state = request.app.state.bv
    disk = state.layout.disk_usage()
    logs = read_recent_logs(state.layout.root, max_lines=100)
    hw = detect_hardware_profile()
    repo_slug = get_github_repo()
    if check_remote:
        rel = check_github_release(__version__, repo=repo_slug, force_refresh=force_refresh)
    else:
        rel = get_cached_release_check(__version__, repo=repo_slug) or {
            "github_repo": repo_slug,
            "current_version": __version__,
            "latest_version": __version__,
            "update_available": False,
            "release_url": f"https://github.com/{repo_slug}/releases",
            "download_url": None,
            "published_at": None,
            "release_notes": "",
            "check_status": "local_only",
            "error": None,
        }
    return {
        "current_version": __version__,
        "latest_version": rel.get("latest_version", __version__),
        "update_available": bool(rel.get("update_available", False)),
        "github_repo": rel.get("github_repo", repo_slug),
        "release_url": rel.get("release_url", f"https://github.com/{repo_slug}/releases"),
        "release_notes": rel.get("release_notes", ""),
        "published_at": rel.get("published_at"),
        "update_check_status": rel.get("check_status", "local_only"),
        "update_check_error": rel.get("error"),
        "schema_version": SCHEMA_VERSION,
        "hardware": hw,
        "changelog": _load_release_changelog(),
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
def trigger_manual_update(
    apply: bool = Query(default=False),
    force: bool = Query(default=False),
) -> dict[str, Any]:
    app_dir = Path(__file__).resolve().parent.parent.parent
    if apply:
        return apply_github_release_update(app_dir=app_dir, current_version=__version__, force=force)
    rel = check_github_release(__version__, force_refresh=True)
    return {"status": "ok", **rel}
