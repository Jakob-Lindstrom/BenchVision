"""Frontier computational-vision extractors: DMD modal X-ray, fluid PIV/LIC, strain gauge & rolling shutter."""

from __future__ import annotations

import base64
import io
import wave
from typing import Any

import cv2
import numpy as np


def _encode_png_b64(bgr: np.ndarray, target_width: int = 360) -> str:
    """Resize BGR image preserving aspect ratio and encode as a base64 data URI PNG."""
    h, w = bgr.shape[:2]
    if w > 0 and abs(w - target_width) > 4:
        scale = float(target_width) / float(w)
        new_h = max(8, round(h * scale))
        interp = cv2.INTER_CUBIC if w < target_width else cv2.INTER_AREA
        bgr = cv2.resize(bgr, (target_width, new_h), interpolation=interp)
    ok, buf = cv2.imencode(".png", bgr)
    if not ok:
        raise RuntimeError("Failed to encode diagnostic image to PNG.")
    b64 = base64.b64encode(bytes(buf)).decode("ascii")
    return f"data:image/png;base64,{b64}"


def compute_dmd_modal_xray(
    frames_bgr: np.ndarray,
    fps: float,
    max_modes: int = 6,
    max_dim: int = 112,
    max_frames: int = 100,
    max_rank: int = 14,
) -> dict[str, Any]:
    """Decompose video into coherent frequency/damping eigenmodes via Snapshot Gram DMD."""
    if frames_bgr.ndim != 4 or len(frames_bgr) < 6:
        raise ValueError("Clip must contain at least 6 frames for Auto-Find Vibrations.")

    n_total, h0, w0, _ = frames_bgr.shape
    stride = max(1, n_total // max(12, int(max_frames)))
    sub = frames_bgr[::stride]
    fps_eff = float(fps) / float(stride)
    dt = 1.0 / max(fps_eff, 1e-3)

    scale = min(1.0, float(max_dim) / float(max(h0, w0)))
    h_s = max(4, round(h0 * scale))
    w_s = max(4, round(w0 * scale))
    t_len = len(sub)

    snapshots = np.empty((h_s * w_s, t_len), dtype=np.float32)
    for t_i in range(t_len):
        gray = cv2.cvtColor(sub[t_i], cv2.COLOR_BGR2GRAY)
        if (h_s, w_s) != (h0, w0):
            gray = cv2.resize(gray, (w_s, h_s), interpolation=cv2.INTER_AREA)
        snapshots[:, t_i] = gray.reshape(-1).astype(np.float32)

    mean_vec = np.mean(snapshots, axis=1, keepdims=True)
    mean_img = mean_vec.reshape(h_s, w_s)
    X = snapshots - mean_vec
    del snapshots
    if float(np.sum(X * X)) < 1e-5:
        return {
            "clip_fps": round(float(fps), 3),
            "effective_fps": round(fps_eff, 3),
            "frames_analyzed": t_len,
            "rank_used": 0,
            "mode_count": 0,
            "summary": "Scene looks completely static (zero pixel change across frames).",
            "modes": [],
        }

    n_pix = h_s * w_s
    # 2-step Hankel time-delay augmentation ensures real standing waves (rank-1 spatially)
    # span a 2D complex-conjugate oscillatory subspace, solved via Method of Snapshots.
    X_h = np.vstack([X[:, :-1], X[:, 1:]])
    del X
    X1 = X_h[:, :-1]
    X2 = X_h[:, 1:]
    gram = (X1.T @ X1).astype(np.float64)
    eig_g, v_g = np.linalg.eigh(gram)
    eig_g = eig_g[::-1]
    v_g = v_g[:, ::-1]
    s = np.sqrt(np.maximum(eig_g, 0.0))
    if len(s) < 2 or float(s[0]) <= 1e-8:
        return {
            "clip_fps": round(float(fps), 3),
            "effective_fps": round(fps_eff, 3),
            "frames_analyzed": t_len,
            "rank_used": 0,
            "mode_count": 0,
            "summary": "Not enough motion contrast to isolate distinct vibration frequencies.",
            "modes": [],
        }

    valid_sv = int(np.sum(s > (float(s[0]) * 1e-4)))
    r = min(len(s), max(2, min(int(max_rank), valid_sv)))
    sr = np.maximum(s[:r], 1e-9)
    inv_s_v = (v_g[:, :r] / sr[None, :]).astype(np.float32)
    u_r = X1 @ inv_s_v
    a_tilde = ((u_r.T @ X2) @ inv_s_v).astype(np.float64)
    evals, w_eig = np.linalg.eig(a_tilde)
    pod_x0 = (u_r.T @ X1[:, 0]).astype(np.float64)
    b, *_ = np.linalg.lstsq(w_eig, pod_x0, rcond=None)
    # Only project physical top half of X2 (saves 50% spatial mode memory)
    phi_spatial = (X2[:n_pix, :] @ inv_s_v) @ w_eig
    del X_h, X1, X2, u_r

    total_time = max(t_len * dt, 1e-3)
    min_freq_hz = max(0.12, 0.7 / total_time)
    max_freq_hz = 0.48 * fps_eff

    candidates: list[dict[str, Any]] = []
    for idx in range(len(evals)):
        mu = complex(evals[idx])
        mag_mu = abs(mu)
        if mag_mu < 1e-5 or mag_mu > 1.5:
            continue
        omega = np.log(complex(mu)) / dt
        freq_hz = float(np.imag(omega) / (2.0 * np.pi))
        if freq_hz < min_freq_hz or freq_hz > max_freq_hz:
            continue

        sigma = float(np.real(omega))
        damping_ratio = max(-1.0, min(1.0, float(-sigma / max(abs(omega), 1e-6))))
        damping_pct = round(damping_ratio * 100.0, 2)
        if damping_pct > 2.0:
            stability = "Decaying Ring-Down"
        elif damping_pct < -2.0:
            stability = "Growing / Unstable"
        else:
            stability = "Steady Forced Mode"

        phi_vec = phi_spatial[:, idx]
        mode_energy = float(
            ((abs(complex(b[idx])) / max(mag_mu, 1e-3)) * np.linalg.norm(phi_vec)) ** 2
        )
        if not np.isfinite(mode_energy) or mode_energy <= 0.0:
            continue

        candidates.append(
            {
                "idx": idx,
                "freq_hz": freq_hz,
                "damping_pct": damping_pct,
                "stability_label": stability,
                "mode_energy": mode_energy,
                "phi_2d": phi_vec.reshape(h_s, w_s),
            }
        )

    candidates.sort(key=lambda m: m["mode_energy"], reverse=True)
    selected: list[dict[str, Any]] = []
    for cand in candidates:
        if any(abs(cand["freq_hz"] - k["freq_hz"]) < 0.08 for k in selected):
            continue
        selected.append(cand)
        if len(selected) >= max_modes:
            break

    total_energy = sum(m["mode_energy"] for m in selected) or 1.0
    base_gray_u8 = np.clip(mean_img, 0.0, 255.0).astype(np.float32)
    base_bgr = np.repeat(base_gray_u8[..., None], 3, axis=2)
    cyan = np.array([248.0, 189.0, 56.0], dtype=np.float32)
    coral = np.array([95.0, 90.0, 250.0], dtype=np.float32)

    modes_out: list[dict[str, Any]] = []
    for rank_i, mode in enumerate(selected, start=1):
        phi_2d = mode["phi_2d"]
        amp = np.abs(phi_2d).astype(np.float32)
        p99 = max(float(np.percentile(amp, 99.0)), 1e-6)
        amp_norm = np.clip(amp / p99, 0.0, 1.0)

        heat = cv2.applyColorMap(
            (amp_norm * 255.0).astype(np.uint8), cv2.COLORMAP_INFERNO
        ).astype(np.float32)
        amp_bgr = np.clip(base_bgr * 0.32 + heat * 0.68, 0.0, 255.0).astype(np.uint8)

        py, px = np.unravel_index(int(np.argmax(amp)), amp.shape)
        ref_ang = float(np.angle(phi_2d[py, px]))
        signed_cos = np.cos(np.angle(phi_2d) - ref_ang).astype(np.float32)
        pos = np.clip(signed_cos, 0.0, 1.0) * amp_norm
        neg = np.clip(-signed_cos, 0.0, 1.0) * amp_norm
        alpha_m = np.maximum(pos, neg)[..., None] * 0.78
        dim_base = base_bgr * 0.42
        phase_bgr = (
            dim_base * (1.0 - alpha_m)
            + pos[..., None] * cyan[None, None, :]
            + neg[..., None] * coral[None, None, :]
        )
        phase_u8 = np.clip(phase_bgr, 0.0, 255.0).astype(np.uint8)

        f_hz = round(mode["freq_hz"], 2)
        bw = max(0.25, round(mode["freq_hz"] * 0.22, 2))
        modes_out.append(
            {
                "mode_index": rank_i,
                "freq_hz": f_hz,
                "bpm": round(mode["freq_hz"] * 60.0, 1),
                "damping_pct": mode["damping_pct"],
                "stability_label": mode["stability_label"],
                "energy_pct": round((mode["mode_energy"] / total_energy) * 100.0, 1),
                "suggested_low_hz": round(max(0.1, mode["freq_hz"] - bw), 2),
                "suggested_high_hz": round(min(float(fps) * 0.48, mode["freq_hz"] + bw), 2),
                "amp_png_b64": _encode_png_b64(amp_bgr, target_width=320),
                "phase_png_b64": _encode_png_b64(phase_u8, target_width=320),
            }
        )

    if modes_out:
        top = modes_out[0]
        summary = (
            f"Found {len(modes_out)} vibration frequency/ies: strongest at "
            f"{top['freq_hz']} Hz ({top['bpm']} BPM, {top['energy_pct']}% of motion energy)."
        )
    else:
        summary = "No periodic oscillation peaks stood out in this clip."

    return {
        "clip_fps": round(float(fps), 3),
        "effective_fps": round(fps_eff, 3),
        "frames_analyzed": t_len,
        "rank_used": int(r),
        "mode_count": len(modes_out),
        "summary": summary,
        "modes": modes_out,
    }


def _compute_fast_lic(vx: np.ndarray, vy: np.ndarray, steps: int = 9) -> np.ndarray:
    """Generate a fast 2D Line Integral Convolution texture highlighting streamlines."""
    h, w = vx.shape[:2]
    rng = np.random.default_rng(42)
    noise = rng.random((h, w), dtype=np.float32)

    speed = np.hypot(vx, vy)
    p95 = float(np.percentile(speed, 95.0)) if float(np.max(speed)) > 0 else 0.0
    active = speed >= (0.05 * p95)
    denom = np.maximum(speed, 1e-4)
    nx = np.where(active, vx / denom, 0.0).astype(np.float32)
    ny = np.where(active, vy / denom, 0.0).astype(np.float32)

    gx, gy = np.meshgrid(
        np.arange(w, dtype=np.float32),
        np.arange(h, dtype=np.float32),
    )

    step_weights = np.hanning(2 * steps + 1).astype(np.float32)
    acc = noise * step_weights[steps]
    total_w = float(step_weights[steps])

    fx, fy = gx.copy(), gy.copy()
    bx, by = gx.copy(), gy.copy()
    step_px = 1.25
    for k in range(1, steps + 1):
        w_f = float(step_weights[steps + k])
        w_b = float(step_weights[steps - k])
        ix_f = np.clip(np.round(fx).astype(np.int32), 0, w - 1)
        iy_f = np.clip(np.round(fy).astype(np.int32), 0, h - 1)
        fx = fx + nx[iy_f, ix_f] * step_px
        fy = fy + ny[iy_f, ix_f] * step_px

        ix_b = np.clip(np.round(bx).astype(np.int32), 0, w - 1)
        iy_b = np.clip(np.round(by).astype(np.int32), 0, h - 1)
        bx = bx - nx[iy_b, ix_b] * step_px
        by = by - ny[iy_b, ix_b] * step_px

        s_f = cv2.remap(noise, fx, fy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        s_b = cv2.remap(noise, bx, by, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        acc = acc + s_f * w_f + s_b * w_b
        total_w += w_f + w_b

    lic = acc / max(total_w, 1e-6)
    lo = float(np.percentile(lic, 5.0))
    hi = float(np.percentile(lic, 95.0))
    if hi - lo < 1e-5:
        return np.full((h, w), 0.5, dtype=np.float32)
    return np.clip((lic - lo) / (hi - lo), 0.0, 1.0).astype(np.float32)


def compute_fluid_vorticity_lic(
    frames_bgr: np.ndarray,
    fps: float,
    max_width: int = 400,
    pair_count: int = 16,
    timestamps_s: np.ndarray | None = None,
) -> dict[str, Any]:
    """Extract fluid PIV streamlines (LIC), vorticity curl, and divergence from dense optical flow."""
    if frames_bgr.ndim != 4 or len(frames_bgr) < 2:
        raise ValueError("Clip must contain at least 2 frames for Airflow & Swirl Map.")

    n, h0, w0, _ = frames_bgr.shape
    scale = min(1.0, float(max_width) / float(max(w0, 1)))
    w = max(16, round(w0 * scale))
    h = max(16, round(h0 * scale))

    grays: list[np.ndarray] = []
    for i in range(n):
        g = cv2.cvtColor(frames_bgr[i], cv2.COLOR_BGR2GRAY)
        if (w, h) != (w0, h0):
            g = cv2.resize(g, (w, h), interpolation=cv2.INTER_AREA)
        grays.append(g)

    pair_list: list[tuple[int, float]] = []
    if timestamps_s is not None and len(timestamps_s) == n:
        dts = np.diff(timestamps_s)
        pos_dts = dts[dts > 1e-4]
        min_dt = float(np.min(pos_dts)) if len(pos_dts) > 0 else (1.0 / max(float(fps), 1e-3))
        for i in range(n - 1):
            dt_i = float(dts[i])
            if 1e-4 < dt_i <= (min_dt * 1.8):
                pair_list.append((i, dt_i))
    if not pair_list:
        default_dt = 1.0 / max(float(fps), 1e-3)
        pair_idxs = np.unique(np.linspace(0, n - 2, min(n - 1, pair_count), dtype=int))
        pair_list = [(int(idx), default_dt) for idx in pair_idxs]

    vx_sum = np.zeros((h, w), dtype=np.float32)
    vy_sum = np.zeros((h, w), dtype=np.float32)
    rms_sum = 0.0
    best_ke = -1.0
    vx_peak = np.zeros((h, w), dtype=np.float32)
    vy_peak = np.zeros((h, w), dtype=np.float32)

    for idx, dt_i in pair_list:
        flow = cv2.calcOpticalFlowFarneback(
            grays[idx], grays[idx + 1], None, 0.5, 3, 15, 3, 5, 1.2, 0
        )
        inv_dt = 1.0 / max(dt_i, 1e-4)
        vx_i = flow[..., 0] * inv_dt
        vy_i = flow[..., 1] * inv_dt
        vx_sum += vx_i
        vy_sum += vy_i
        ke_i = float(np.mean(vx_i * vx_i + vy_i * vy_i))
        rms_sum += float(np.sqrt(ke_i))
        if ke_i > best_ke:
            best_ke = ke_i
            vx_peak = vx_i
            vy_peak = vy_i

    m_pairs = max(1, len(pair_list))
    vx_mean = vx_sum / float(m_pairs)
    vy_mean = vy_sum / float(m_pairs)
    mean_speed_avg = float(np.mean(np.hypot(vx_mean, vy_mean)))
    rms_speed_avg = rms_sum / float(m_pairs)

    if mean_speed_avg < 0.35 * rms_speed_avg:
        vx, vy = vx_peak, vy_peak
        field_mode = "peak_instantaneous"
    else:
        vx, vy = vx_mean, vy_mean
        field_mode = "steady_mean"

    vx = cv2.GaussianBlur(vx, (5, 5), 0)
    vy = cv2.GaussianBlur(vy, (5, 5), 0)
    dvx_dy, dvx_dx = np.gradient(vx)
    dvy_dy, dvy_dx = np.gradient(vy)
    curl = (dvy_dx - dvx_dy).astype(np.float32)
    div = (dvx_dx + dvy_dy).astype(np.float32)
    speed = np.hypot(vx, vy).astype(np.float32)

    lic = _compute_fast_lic(vx, vy, steps=9)
    lic_mod = 0.45 + 0.75 * lic

    c_scale = max(float(np.percentile(np.abs(curl), 98.0)), 1e-4)
    c_norm = np.clip(curl / c_scale, -1.0, 1.0)
    ccw = np.clip(c_norm, 0.0, 1.0)[..., None]
    cw = np.clip(-c_norm, 0.0, 1.0)[..., None]
    neutral = (1.0 - np.abs(c_norm))[..., None]

    slate = np.array([42.0, 32.0, 24.0], dtype=np.float32)
    ccw_col = np.array([68.0, 125.0, 250.0], dtype=np.float32)  # Fiery red/orange
    cw_col = np.array([250.0, 165.0, 50.0], dtype=np.float32)   # Crisp electric blue

    vort_rgb = (
        neutral * slate[None, None, :]
        + ccw * ccw_col[None, None, :]
        + cw * cw_col[None, None, :]
    ) * lic_mod[..., None]
    scene_bgr = np.repeat(grays[n // 2][..., None].astype(np.float32), 3, axis=2)
    vort_vis = np.clip(vort_rgb * 0.78 + scene_bgr * 0.22, 0.0, 255.0).astype(np.uint8)

    p95_spd = max(float(np.percentile(speed, 95.0)), 1e-4)
    step = max(18, min(h, w) // 11)
    for yy in range(step // 2, h, step):
        for xx in range(step // 2, w, step):
            spd_pt = float(speed[yy, xx])
            if spd_pt <= 0.2 * p95_spd:
                continue
            dx_n = float(vx[yy, xx] / spd_pt)
            dy_n = float(vy[yy, xx] / spd_pt)
            arr_len = max(6, min(14, round(6.0 + 8.0 * min(1.0, spd_pt / p95_spd))))
            x2 = round(xx + dx_n * arr_len)
            y2 = round(yy + dy_n * arr_len)
            cv2.arrowedLine(
                vort_vis,
                (xx, yy),
                (x2, y2),
                (245, 250, 255),
                1,
                cv2.LINE_AA,
                tipLength=0.35,
            )

    spd_norm = np.clip(speed / p95_spd, 0.0, 1.0)
    spd_heat = cv2.applyColorMap((spd_norm * 255.0).astype(np.uint8), cv2.COLORMAP_TURBO).astype(
        np.float32
    )
    spd_vis = np.clip((spd_heat * lic_mod[..., None]) * 0.78 + scene_bgr * 0.22, 0.0, 255.0).astype(
        np.uint8
    )

    curl_max = float(np.max(curl))
    curl_min = float(np.min(curl))
    if abs(curl_max) < 0.05 and abs(curl_min) < 0.05:
        dominant_spin = "Irrotational / Minimal Swirl"
    elif curl_max >= abs(curl_min):
        dominant_spin = "Counter-Clockwise (CCW) Vortex"
    else:
        dominant_spin = "Clockwise (CW) Vortex"

    mean_spd = float(np.mean(speed))
    max_spd = float(np.max(speed))
    peak_vort = float(np.max(np.abs(curl)))
    turb_idx = float(np.std(speed) / max(mean_spd, 1e-4))
    return {
        "field_mode": field_mode,
        "mean_speed_px_s": round(mean_spd, 2),
        "max_speed_px_s": round(max_spd, 2),
        "peak_vorticity_rad_s": round(peak_vort, 3),
        "mean_abs_vorticity_rad_s": round(float(np.mean(np.abs(curl))), 3),
        "peak_divergence_s": round(float(np.max(np.abs(div))), 3),
        "turbulence_index": round(turb_idx, 3),
        "dominant_spin": dominant_spin,
        "summary": (
            f"Peak flow reached {round(max_spd, 1)} px/s with {dominant_spin.lower()} "
            f"(swirl {round(peak_vort, 2)} rad/s)."
        ),
        "vorticity_lic_png_b64": _encode_png_b64(vort_vis, target_width=400),
        "speed_png_b64": _encode_png_b64(spd_vis, target_width=400),
    }


def compute_optical_strain_gauge(
    frames_bgr: np.ndarray,
    fps: float,
    max_width: int = 400,
    timestamps_s: np.ndarray | None = None,
) -> dict[str, Any]:
    """Compute 2D sub-pixel displacement strain tensor and von Mises stress map."""
    if frames_bgr.ndim != 4 or len(frames_bgr) < 2:
        raise ValueError("Clip must contain at least 2 frames for Optical Strain Gauge.")

    n, h0, w0, _ = frames_bgr.shape
    scale = min(1.0, float(max_width) / float(max(w0, 1)))
    w = max(16, round(w0 * scale))
    h = max(16, round(h0 * scale))

    grays: list[np.ndarray] = []
    for i in range(n):
        g = cv2.cvtColor(frames_bgr[i], cv2.COLOR_BGR2GRAY)
        if (w, h) != (w0, h0):
            g = cv2.resize(g, (w, h), interpolation=cv2.INTER_AREA)
        grays.append(g)

    ref_n = min(3, n)
    ref_gray = np.median(np.stack(grays[:ref_n], axis=0), axis=0).astype(np.uint8)
    ref_f = ref_gray.astype(np.float32)

    scan_idxs = np.unique(np.linspace(1, n - 1, min(n - 1, 24), dtype=int))
    peak_idx = int(scan_idxs[-1])
    best_diff = -1.0
    for idx in scan_idxs:
        d = float(np.mean(np.abs(grays[int(idx)].astype(np.float32) - ref_f)))
        if d > best_diff:
            best_diff = d
            peak_idx = int(idx)

    flow = cv2.calcOpticalFlowFarneback(
        ref_gray, grays[peak_idx], None, 0.5, 4, 15, 4, 7, 1.5, 0
    )
    disp_u = cv2.GaussianBlur(flow[..., 0], (7, 7), 0)
    disp_v = cv2.GaussianBlur(flow[..., 1], (7, 7), 0)

    du_dy, du_dx = np.gradient(disp_u)
    dv_dy, dv_dx = np.gradient(disp_v)
    exx = du_dx
    eyy = dv_dy
    exy = 0.5 * (du_dy + dv_dx)

    eps_vm = np.sqrt(np.maximum(0.0, exx * exx - exx * eyy + eyy * eyy + 3.0 * exy * exy))
    dilation = exx + eyy

    m = min(4, h // 8, w // 8)
    if h > 2 * m and w > 2 * m:
        vm_inner = eps_vm[m : h - m, m : w - m]
        dil_inner = dilation[m : h - m, m : w - m]
        iy, ix = np.unravel_index(int(np.argmax(vm_inner)), vm_inner.shape)
        peak_y, peak_x = int(iy + m), int(ix + m)
    else:
        vm_inner = eps_vm
        dil_inner = dilation
        peak_y, peak_x = np.unravel_index(int(np.argmax(eps_vm)), eps_vm.shape)
        peak_y, peak_x = int(peak_y), int(peak_x)

    peak_vm = float(np.max(vm_inner))
    mean_vm = float(np.mean(vm_inner))
    peak_tens = float(max(0.0, float(np.max(dil_inner))))
    peak_comp = float(max(0.0, float(-np.min(dil_inner))))
    peak_disp = float(np.max(np.hypot(disp_u, disp_v)))

    vm_scale = max(float(np.percentile(eps_vm, 98.5)), 1e-4)
    vm_norm = np.clip(eps_vm / vm_scale, 0.0, 1.0)
    vm_heat = cv2.applyColorMap((vm_norm * 255.0).astype(np.uint8), cv2.COLORMAP_TURBO).astype(
        np.float32
    )
    scene_bgr = np.repeat(grays[peak_idx][..., None].astype(np.float32), 3, axis=2)
    vm_vis = np.clip(scene_bgr * 0.40 + vm_heat * 0.60, 0.0, 255.0).astype(np.uint8)
    cv2.drawMarker(
        vm_vis,
        (peak_x, peak_y),
        (255, 255, 255),
        markerType=cv2.MARKER_CROSS,
        markerSize=14,
        thickness=2,
    )
    cv2.circle(vm_vis, (peak_x, peak_y), 7, (248, 189, 56), 2)

    d_scale = max(float(np.percentile(np.abs(dilation), 98.5)), 1e-4)
    d_norm = np.clip(dilation / d_scale, -1.0, 1.0)
    tens_m = np.clip(d_norm, 0.0, 1.0)[..., None]
    comp_m = np.clip(-d_norm, 0.0, 1.0)[..., None]
    alpha_tc = np.maximum(tens_m, comp_m) * 0.72
    red_t = np.array([75.0, 80.0, 245.0], dtype=np.float32)
    blue_c = np.array([250.0, 165.0, 55.0], dtype=np.float32)
    tc_vis = (
        scene_bgr * 0.55 * (1.0 - alpha_tc)
        + tens_m * red_t[None, None, :]
        + comp_m * blue_c[None, None, :]
    )
    tc_u8 = np.clip(tc_vis, 0.0, 255.0).astype(np.uint8)

    peak_t = (
        round(float(timestamps_s[peak_idx] - timestamps_s[0]), 3)
        if (timestamps_s is not None and len(timestamps_s) == n)
        else round(float(peak_idx) / max(float(fps), 1e-3), 3)
    )
    return {
        "peak_frame_index": peak_idx,
        "peak_time_s": peak_t,
        "peak_displacement_px": round(peak_disp, 3),
        "peak_von_mises_pct": round(peak_vm * 100.0, 3),
        "peak_microstrain": round(peak_vm * 1e6, 1),
        "mean_von_mises_pct": round(mean_vm * 100.0, 3),
        "peak_tension_pct": round(peak_tens * 100.0, 3),
        "peak_compression_pct": round(peak_comp * 100.0, 3),
        "peak_xy": [peak_x, peak_y],
        "summary": (
            f"Peak flex of {round(peak_disp, 2)} px and {round(peak_vm * 100.0, 2)}% strain "
            f"near pixel ({peak_x}, {peak_y}) at t={peak_t}s."
        ),
        "strain_vm_png_b64": _encode_png_b64(vm_vis, target_width=400),
        "tension_comp_png_b64": _encode_png_b64(tc_u8, target_width=400),
    }


def _extract_rolling_shutter_residuals(
    frames_bgr: np.ndarray, max_frames: int
) -> tuple[np.ndarray, np.ndarray]:
    """Return (row_mean_matrix, zero_mean_residual) both shaped (T_sub, H)."""
    n = len(frames_bgr)
    idxs = np.unique(np.linspace(0, n - 1, min(n, max_frames), dtype=int))
    h = frames_bgr.shape[1]
    row_mat = np.empty((len(idxs), h), dtype=np.float32)
    for i, f_idx in enumerate(idxs):
        gray = cv2.cvtColor(frames_bgr[int(f_idx)], cv2.COLOR_BGR2GRAY).astype(np.float32)
        row_mat[i, :] = np.mean(gray, axis=1)
    bg_prof = np.median(row_mat, axis=0, keepdims=True)
    res = row_mat - bg_prof
    res = res - np.mean(res, axis=1, keepdims=True)
    return row_mat, res


def compute_rolling_shutter_analysis(
    frames_bgr: np.ndarray,
    fps: float,
    readout_ms: float = 16.0,
    max_frames: int = 32,
) -> dict[str, Any]:
    """Extract super-Nyquist flicker & acoustic waves from CMOS rolling-shutter row banding."""
    if frames_bgr.ndim != 4 or len(frames_bgr) < 2:
        raise ValueError("Clip must contain at least 2 frames for Rolling-Shutter analysis.")

    row_mat, res = _extract_rolling_shutter_residuals(frames_bgr, max_frames=max_frames)
    _t_len, h = res.shape
    eff_readout_s = max(0.002, min(0.050, float(readout_ms) / 1000.0))
    fs_row = float(h) / eff_readout_s

    win = np.hanning(h).astype(np.float32)
    windowed = res * win[None, :]
    nfft = max(h, 512)
    spec = np.fft.rfft(windowed, n=nfft, axis=1)
    avg_psd = np.mean(np.abs(spec) ** 2, axis=0)
    freqs = np.fft.rfftfreq(nfft, d=1.0 / max(fs_row, 1.0))

    f_min = max(35.0, float(fps) * 1.5)
    f_max = min(12000.0, fs_row * 0.45)
    mask = (freqs >= f_min) & (freqs <= f_max)
    if not np.any(mask):
        mask = freqs > 0.0

    sub_freqs = freqs[mask]
    sub_psd = avg_psd[mask]
    peak_i = int(np.argmax(sub_psd))
    peak_hz = float(sub_freqs[peak_i])
    peak_p = float(sub_psd[peak_i])
    med_p = max(float(np.median(sub_psd)), 1e-12)
    snr_db = float(10.0 * np.log10(max(peak_p, 1e-12) / med_p))

    harmonics: list[dict[str, float]] = []
    order = np.argsort(sub_psd)[::-1]
    for oi in order:
        cand_hz = float(sub_freqs[int(oi)])
        if any(abs(cand_hz - h_item["hz"]) < max(30.0, peak_hz * 0.08) for h_item in harmonics):
            continue
        harmonics.append(
            {
                "hz": round(cand_hz, 1),
                "rel_db": round(
                    float(10.0 * np.log10(max(float(sub_psd[int(oi)]), 1e-12) / max(peak_p, 1e-12))),
                    1,
                ),
            }
        )
        if len(harmonics) >= 3:
            break

    mean_luma = max(float(np.mean(row_mat)), 1.0)
    flicker_depth_pct = round(float((2.0 * np.std(res) / mean_luma) * 100.0), 2)

    if snr_db < 6.0 or flicker_depth_pct < 0.35:
        source_hint = "Clean DC / Global Shutter (No Banding)"
    elif 85.0 <= peak_hz <= 135.0:
        source_hint = "Mains AC Lighting Flicker (50/60 Hz Rectified)"
    elif 135.0 < peak_hz <= 2500.0:
        source_hint = "LED / Display PWM or Acoustic Vibration"
    else:
        source_hint = "High-Frequency PWM Driver"

    # Build 2-panel BGR visualization (top: space-time rolling banding strip; bottom: spectrum curve)
    panel_w, top_h, bot_h = 460, 140, 160
    p99 = max(float(np.percentile(np.abs(res), 99.0)), 1e-4)
    norm_res = np.clip((res.T / (2.0 * p99)) + 0.5, 0.0, 1.0)
    band_u8 = (norm_res * 255.0).astype(np.uint8)
    band_bgr = cv2.applyColorMap(
        cv2.resize(band_u8, (panel_w, top_h), interpolation=cv2.INTER_NEAREST),
        cv2.COLORMAP_TWILIGHT_SHIFTED,
    )
    cv2.putText(
        band_bgr,
        "ROW-TIME BANDING STRIPS (Y vs FRAME)",
        (10, 20),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.48,
        (240, 248, 255),
        1,
    )

    spec_bgr = np.full((bot_h, panel_w, 3), (24, 15, 10), dtype=np.uint8)
    log_psd = np.log10(np.maximum(sub_psd, 1e-12))
    lo_l, hi_l = float(np.min(log_psd)), float(np.max(log_psd))
    span_l = max(hi_l - lo_l, 1e-3)
    margin_x, margin_y = 18, 24
    usable_w = panel_w - 2 * margin_x
    usable_h = bot_h - 2 * margin_y
    pts: list[tuple[int, int]] = []
    f_span = max(float(sub_freqs[-1] - sub_freqs[0]), 1.0)
    for k in range(len(sub_freqs)):
        xp = margin_x + round(((float(sub_freqs[k]) - float(sub_freqs[0])) / f_span) * usable_w)
        yp = bot_h - margin_y - round(((float(log_psd[k]) - lo_l) / span_l) * usable_h)
        pts.append((xp, yp))
    if len(pts) >= 2:
        cv2.polylines(
            spec_bgr, [np.array(pts, dtype=np.int32)], False, (248, 189, 56), 2, cv2.LINE_AA
        )
    px_x = margin_x + round(((peak_hz - float(sub_freqs[0])) / f_span) * usable_w)
    cv2.line(spec_bgr, (px_x, 12), (px_x, bot_h - margin_y), (11, 158, 245), 1)
    cv2.putText(
        spec_bgr,
        f"PEAK: {peak_hz:.1f} Hz ({snr_db:.1f} dB SNR)",
        (10, 18),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.48,
        (11, 158, 245),
        1,
    )
    combo = np.vstack([band_bgr, spec_bgr])

    return {
        "row_sample_rate_hz": round(fs_row, 1),
        "nyquist_limit_hz": round(fs_row * 0.45, 1),
        "peak_hz": round(peak_hz, 1),
        "snr_db": round(snr_db, 2),
        "flicker_depth_pct": flicker_depth_pct,
        "harmonics": harmonics,
        "source_hint": source_hint,
        "summary": (
            f"{source_hint}: strongest high-frequency pattern at {round(peak_hz, 1)} Hz "
            f"({round(snr_db, 1)} dB above noise)."
        ),
        "rolling_png_b64": _encode_png_b64(combo, target_width=460),
    }


def synthesize_rolling_shutter_wav(
    frames_bgr: np.ndarray,
    fps: float,
    readout_ms: float = 16.0,
    max_frames: int = 48,
) -> bytes:
    """Recover an audible mono 16 kHz PCM WAV stream from rolling-shutter row residuals."""
    if frames_bgr.ndim != 4 or len(frames_bgr) < 2:
        raise ValueError("Clip must contain at least 2 frames for Visual Mic WAV export.")

    _row_mat, res = _extract_rolling_shutter_residuals(frames_bgr, max_frames=max_frames)
    t_len, h = res.shape
    eff_readout_s = max(0.002, min(0.050, float(readout_ms) / 1000.0))

    # Taper each frame's top/bottom rows slightly to reduce inter-frame seam clicks
    seam_win = np.hanning(h).astype(np.float32)
    tapered = (res * seam_win[None, :]).reshape(-1)
    total_dur_s = max(0.1, float(t_len) * eff_readout_s)
    out_rate = 16000
    n_out = max(1600, round(total_dur_s * out_rate))
    x_src = np.linspace(0.0, 1.0, len(tapered), dtype=np.float64)
    x_dst = np.linspace(0.0, 1.0, n_out, dtype=np.float64)
    audio = np.interp(x_dst, x_src, tapered.astype(np.float64))

    peak = float(np.max(np.abs(audio)))
    if peak > 1e-8:
        audio = audio / peak * 0.85
    fade = max(16, int(0.01 * n_out))
    audio[:fade] *= np.linspace(0.0, 1.0, fade)
    audio[-fade:] *= np.linspace(1.0, 0.0, fade)
    pcm = np.clip(np.round(audio * 32767.0), -32768, 32767).astype("<i2")

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(out_rate)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()
