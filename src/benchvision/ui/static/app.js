/* BenchVision Frontend Controller (Zero Node.js Runtime Dependency) */

const state = {
  cameras: [],
  activeCameraId: '',
  capabilities: null,
  presets: [],
  methods: [],
  clips: [],
  jobs: [],
  activeResultJob: null,
  lastRecWidth: 640,
  lastRecHeight: 480,
  tareLocked: false,
  showEpicenter: false,
  currentSpectrum: null,
  audioCtx: null,
  audioSource: null,
  userEditedClipTitle: false,
  userEditedMaxHz: false,
  rulerMode: false,
  rulerPoints: [],
  rulerPixelDist: 0,
  triggerArmed: false,
  triggerRecordingInFlight: false,
  selectedFftPeakHz: null,
  hoverFftHz: null,
  activeDualProbe: 'A',
  baselineSpectrum: null,
  dualCamEnabled: false,
  secondaryCameraId: 'synthetic:1',
  dualSyncMode: 'software',
  dualTier: null,
  dualPreviewView: 'sbs',
};

function formatDefaultClipTitle() {
  const now = new Date();
  const pad2 = (n) => String(n).padStart(2, '0');
  const y = now.getFullYear();
  const m = pad2(now.getMonth() + 1);
  const d = pad2(now.getDate());
  const hh = pad2(now.getHours());
  const mm = pad2(now.getMinutes());
  const ss = pad2(now.getSeconds());
  return `clip-${y}${m}${d}-${hh}${mm}${ss}`;
}

function refreshDefaultClipTitleIfUnedited() {
  const titleEl = document.getElementById('rec-title');
  if (titleEl && !state.userEditedClipTitle) {
    titleEl.value = formatDefaultClipTitle();
  }
}

function onTargetFpsChange() {
  const fpsEl = document.getElementById('rec-fps');
  const maxHzEl = document.getElementById('rec-max-hz');
  if (fpsEl && maxHzEl && !state.userEditedMaxHz) {
    const fps = parseFloat(fpsEl.value) || 30;
    maxHzEl.value = Math.max(1.0, Math.floor(fps / 3));
  }
  runPreflight();
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  if (!res.ok) {
    const errText = await res.text();
    throw new Error(errText || `HTTP ${res.status}`);
  }
  return res.json();
}

function switchPage(pageId) {
  document.querySelectorAll('.page-section').forEach((el) => el.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach((el) => el.classList.remove('active'));
  const sec = document.getElementById(`page-${pageId}`);
  if (sec) sec.classList.add('active');
  const btn = document.querySelector(`.nav-tab[data-page="${pageId}"]`);
  if (btn) btn.classList.add('active');

  if (pageId === 'library') loadLibrary();
  if (pageId === 'process') {
    loadLibrary().then(() => loadJobs());
  }
  if (pageId === 'analysis') {
    loadJobs().then(() => loadJobAnalysis());
  }
  if (pageId === 'system') loadSystemStatus();
}

function openSetupModal() {
  document.getElementById('setup-modal').classList.add('open');
}

function closeSetupModal() {
  document.getElementById('setup-modal').classList.remove('open');
}

function togglePreviewFullscreen() {
  const box = document.getElementById('preview-box');
  if (!document.fullscreenElement) {
    box.requestFullscreen();
  } else {
    document.exitFullscreen();
  }
}

function setDualPreviewView(view) {
  state.dualPreviewView = view || 'sbs';
  ['a', 'b', 'sbs'].forEach((v) => {
    const btn = document.getElementById(`btn-camview-${v}`);
    if (btn) btn.className = v === state.dualPreviewView ? 'primary' : '';
  });
  updateLivePreviewMode();
}

function updateDualCamUiVisibility() {
  const topBadge = document.getElementById('dual-cam-topbar-badge');
  const switcher = document.getElementById('dual-preview-switcher');
  if (topBadge) {
    if (state.dualCamEnabled) {
      topBadge.classList.remove('dual-cam-only');
      const syncTag = (state.dualSyncMode || 'software').toUpperCase();
      topBadge.textContent = `DUAL CAM: A+B (${syncTag})`;
    } else {
      topBadge.classList.add('dual-cam-only');
    }
  }
  if (switcher) {
    if (state.dualCamEnabled) {
      switcher.classList.remove('dual-cam-only');
    } else {
      switcher.classList.add('dual-cam-only');
    }
  }
}

function onToggleSetupDualCamera() {
  const chk = document.getElementById('setup-dual-enable');
  const panel = document.getElementById('setup-dual-panel');
  if (!chk || !panel) return;
  if (chk.checked) {
    panel.classList.remove('dual-cam-only');
  } else {
    panel.classList.add('dual-cam-only');
  }
}

function updateLivePreviewMode() {
  const sel = document.getElementById('live-preview-mode');
  const val = sel ? sel.value : 'normal';
  const assistSel = document.getElementById('live-preview-assist');
  const assistVal = assistSel ? assistSel.value : 'off';
  const strobeEl = document.getElementById('live-strobe-hz');
  const strobeHz = strobeEl ? parseFloat(strobeEl.value) || 0 : 0;
  const img = document.getElementById('live-preview-img');
  if (!img) return;
  const stParam = strobeHz > 0 ? `&strobe_hz=${strobeHz}` : '';
  const assistParam = assistVal && assistVal !== 'off' ? `&assist=${encodeURIComponent(assistVal)}` : '';
  const camViewParam = state.dualCamEnabled ? `&cam_view=${encodeURIComponent(state.dualPreviewView || 'sbs')}` : '';
  if (val === 'posy_2_1') {
    img.src = `/api/cameras/preview.mjpg?mode=motion_extraction&shift=2&gain=1${stParam}${assistParam}${camViewParam}&t=${Date.now()}`;
  } else if (val === 'posy_3_3') {
    img.src = `/api/cameras/preview.mjpg?mode=motion_extraction&shift=3&gain=3${stParam}${assistParam}${camViewParam}&t=${Date.now()}`;
  } else if (val === 'posy_6_6') {
    img.src = `/api/cameras/preview.mjpg?mode=motion_extraction&shift=6&gain=6${stParam}${assistParam}${camViewParam}&t=${Date.now()}`;
  } else if (val === 'posy_rgb_trail') {
    img.src = `/api/cameras/preview.mjpg?mode=posy_rgb_trail&shift=3&gain=3${stParam}${assistParam}${camViewParam}&t=${Date.now()}`;
  } else if (val === 'posy_peak_hold') {
    img.src = `/api/cameras/preview.mjpg?mode=posy_peak_hold&shift=2&gain=3${stParam}${assistParam}${camViewParam}&t=${Date.now()}`;
  } else if (val === 'stroboscope') {
    const hz = strobeHz > 0 ? strobeHz : 2.5;
    img.src = `/api/cameras/preview.mjpg?mode=stroboscope&strobe_hz=${hz}${assistParam}${camViewParam}&t=${Date.now()}`;
  } else {
    img.src = `/api/cameras/preview.mjpg?mode=normal${stParam}${assistParam}${camViewParam}&t=${Date.now()}`;
  }
}

async function toggleTareReferenceFrame() {
  const nextState = !state.tareLocked;
  const res = await api('/api/cameras/tare', {
    method: 'POST',
    body: JSON.stringify({ enable: nextState }),
  });
  state.tareLocked = Boolean(res.tare_locked);
  const btn = document.getElementById('btn-tare-frame');
  if (btn) {
    btn.textContent = state.tareLocked ? 'Unlock Tare (Zero Active)' : 'Tare / Zero Frame';
    btn.className = state.tareLocked ? 'primary' : '';
  }
  updateLivePreviewMode();
}

function updateThermalBadge(hw) {
  if (!hw) return;
  const badge = document.getElementById('thermal-badge');
  if (badge && hw.cpu_temp_c !== undefined) {
    if (hw.is_throttled) {
      badge.textContent = `THROTTLING ${hw.cpu_temp_c}°C (${hw.throttled_hex || ''})`;
      badge.className = 'badge danger';
    } else if (hw.thermal_status === 'warm') {
      badge.textContent = `CPU ${hw.cpu_temp_c}°C (Warm)`;
      badge.className = 'badge rolling';
    } else {
      badge.textContent = `CPU ${hw.cpu_temp_c}°C`;
      badge.className = 'badge global';
    }
  }
}

function getOptionalRoi() {
  const xEl = document.getElementById('roi-x');
  const yEl = document.getElementById('roi-y');
  const wEl = document.getElementById('roi-w');
  const hEl = document.getElementById('roi-h');
  const rawX = xEl ? xEl.value.trim() : '';
  const rawY = yEl ? yEl.value.trim() : '';
  const rawW = wEl ? wEl.value.trim() : '';
  const rawH = hEl ? hEl.value.trim() : '';

  const recW = parseInt(document.getElementById('rec-width').value, 10) || 640;
  const recH = parseInt(document.getElementById('rec-height').value, 10) || 480;
  const roiBadge = document.getElementById('active-roi-badge');

  if (!rawX && !rawY && !rawW && !rawH) {
    if (roiBadge) {
      roiBadge.textContent = `ROI: Full Frame (${recW}x${recH}) · Drag on video or enter ROI`;
      roiBadge.className = 'badge';
    }
    return null;
  }

  const parsedX = parseInt(rawX, 10);
  const parsedY = parseInt(rawY, 10);
  const parsedW = parseInt(rawW, 10);
  const parsedH = parseInt(rawH, 10);

  const rx = Number.isNaN(parsedX) ? 0 : Math.max(0, Math.min(parsedX, recW - 16));
  const ry = Number.isNaN(parsedY) ? 0 : Math.max(0, Math.min(parsedY, recH - 16));
  const rw = (!Number.isNaN(parsedW) && parsedW > 0)
    ? Math.max(16, Math.min(parsedW, recW - rx))
    : Math.max(16, recW - rx);
  const rh = (!Number.isNaN(parsedH) && parsedH > 0)
    ? Math.max(16, Math.min(parsedH, recH - ry))
    : Math.max(16, recH - ry);

  if (rx === 0 && ry === 0 && rw >= recW && rh >= recH) {
    if (roiBadge) {
      roiBadge.textContent = `ROI: Full Frame (${recW}x${recH})`;
      roiBadge.className = 'badge';
    }
    return null;
  }

  if (roiBadge) {
    roiBadge.textContent = `Active ROI Crop: ${rw}x${rh} px at (${rx}, ${ry})`;
    roiBadge.className = 'badge global';
  }
  return [rx, ry, rw, rh];
}

function onSelectResolutionPreset() {
  const sel = document.getElementById('rec-res-preset');
  if (!sel || !sel.value || sel.value === 'custom') return;
  const parts = sel.value.split('x');
  if (parts.length === 2) {
    document.getElementById('rec-width').value = parseInt(parts[0], 10) || 640;
    document.getElementById('rec-height').value = parseInt(parts[1], 10) || 480;
    onResolutionDimensionChange();
  }
}

function onResolutionDimensionChange() {
  const newW = Math.max(64, parseInt(document.getElementById('rec-width').value, 10) || 640);
  const newH = Math.max(64, parseInt(document.getElementById('rec-height').value, 10) || 480);
  const oldW = state.lastRecWidth || 640;
  const oldH = state.lastRecHeight || 480;

  if ((newW !== oldW || newH !== oldH) && oldW > 0 && oldH > 0) {
    const xEl = document.getElementById('roi-x');
    const yEl = document.getElementById('roi-y');
    const wEl = document.getElementById('roi-w');
    const hEl = document.getElementById('roi-h');
    const sx = newW / oldW;
    const sy = newH / oldH;
    if (xEl && xEl.value.trim() !== '') {
      xEl.value = Math.round((parseInt(xEl.value, 10) || 0) * sx);
    }
    if (yEl && yEl.value.trim() !== '') {
      yEl.value = Math.round((parseInt(yEl.value, 10) || 0) * sy);
    }
    if (wEl && wEl.value.trim() !== '') {
      wEl.value = Math.max(16, Math.round((parseInt(wEl.value, 10) || oldW) * sx));
    }
    if (hEl && hEl.value.trim() !== '') {
      hEl.value = Math.max(16, Math.round((parseInt(hEl.value, 10) || oldH) * sy));
    }
    state.lastRecWidth = newW;
    state.lastRecHeight = newH;
  }

  const presetSel = document.getElementById('rec-res-preset');
  if (presetSel) {
    const key = `${newW}x${newH}`;
    if (Array.from(presetSel.options).some((o) => o.value === key)) {
      presetSel.value = key;
    } else {
      presetSel.value = 'custom';
    }
  }
  runPreflight();
}

function clearRoiInputs() {
  ['roi-x', 'roi-y', 'roi-w', 'roi-h'].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  const cropChk = document.getElementById('roi-crop-preview');
  if (cropChk) cropChk.checked = false;
  const dragBox = document.getElementById('roi-drag-box');
  if (dragBox) dragBox.style.display = 'none';
  runPreflight();
}

function initPreviewRoiDrag() {
  const box = document.getElementById('preview-box');
  const img = document.getElementById('live-preview-img');
  const dragBox = document.getElementById('roi-drag-box');
  const dragLabel = document.getElementById('roi-drag-label');
  if (!box || !img) return;
  img.draggable = false;
  let dragStart = null;

  const getVideoMetrics = () => {
    const boxRect = box.getBoundingClientRect();
    const imgRect = img.getBoundingClientRect();
    const recW = parseInt(document.getElementById('rec-width').value, 10) || 640;
    const recH = parseInt(document.getElementById('rec-height').value, 10) || 480;
    const renderW = Math.max(1, imgRect.width);
    const renderH = Math.max(1, imgRect.height);
    const offsetX = imgRect.left - boxRect.left;
    const offsetY = imgRect.top - boxRect.top;
    return { boxRect, imgRect, renderW, renderH, offsetX, offsetY, recW, recH };
  };

  const toCameraCoords = (evt) => {
    const m = getVideoMetrics();
    const isSbs = Boolean(state.dualCamEnabled && state.dualPreviewView === 'sbs');
    const rawRelXFull = (evt.clientX - m.imgRect.left) / m.renderW;
    const rawRelY = (evt.clientY - m.imgRect.top) / m.renderH;
    const panelRight = isSbs && rawRelXFull >= 0.5;
    const rawRelX = isSbs ? (panelRight ? (rawRelXFull - 0.5) * 2.0 : rawRelXFull * 2.0) : rawRelXFull;
    const relX = Math.max(0, Math.min(1, rawRelX));
    const relY = Math.max(0, Math.min(1, rawRelY));

    let camX = Math.round(relX * m.recW);
    let camY = Math.round(relY * m.recH);
    const snapX = Math.max(6, Math.round(m.recW * 0.015));
    const snapY = Math.max(6, Math.round(m.recH * 0.015));
    if (camX <= snapX) camX = 0;
    if (camX >= m.recW - snapX) camX = m.recW;
    if (camY <= snapY) camY = 0;
    if (camY >= m.recH - snapY) camY = m.recH;

    const panelW = isSbs ? m.renderW * 0.5 : m.renderW;
    const panelOffsetX = m.offsetX + (panelRight ? m.renderW * 0.5 : 0);
    const pxX = panelOffsetX + (camX / m.recW) * panelW;
    const pxY = m.offsetY + (camY / m.recH) * m.renderH;
    return { camX, camY, pxX, pxY, recW: m.recW, recH: m.recH };
  };

  const updateDragOverlay = (ptA, ptB) => {
    if (!dragBox) return;
    const leftPx = Math.min(ptA.pxX, ptB.pxX);
    const topPx = Math.min(ptA.pxY, ptB.pxY);
    const widthPx = Math.max(2, Math.abs(ptB.pxX - ptA.pxX));
    const heightPx = Math.max(2, Math.abs(ptB.pxY - ptA.pxY));
    dragBox.style.position = 'absolute';
    dragBox.style.pointerEvents = 'none';
    dragBox.style.boxSizing = 'border-box';
    dragBox.style.display = 'block';
    dragBox.style.left = `${leftPx}px`;
    dragBox.style.top = `${topPx}px`;
    dragBox.style.width = `${widthPx}px`;
    dragBox.style.height = `${heightPx}px`;

    const x0 = Math.min(ptA.camX, ptB.camX);
    const y0 = Math.min(ptA.camY, ptB.camY);
    const rw = Math.abs(ptB.camX - ptA.camX);
    const rh = Math.abs(ptB.camY - ptA.camY);
    if (dragLabel) {
      dragLabel.textContent = `${rw}x${rh} at (${x0},${y0})`;
    }
  };

  const renderRulerOverlay = (pts, m) => {
    const svg = document.getElementById('ruler-overlay-svg');
    if (!svg) return;
    svg.innerHTML = '';
    if (!pts || pts.length === 0) {
      svg.style.display = 'none';
      return;
    }
    svg.style.display = 'block';
    svg.setAttribute('viewBox', `0 0 ${m.recW} ${m.recH}`);
    svg.setAttribute('preserveAspectRatio', 'none');

    if (pts.length === 2) {
      const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      line.setAttribute('x1', pts[0].camX);
      line.setAttribute('y1', pts[0].camY);
      line.setAttribute('x2', pts[1].camX);
      line.setAttribute('y2', pts[1].camY);
      line.setAttribute('stroke', '#facc15');
      line.setAttribute('stroke-width', Math.max(2, Math.round(m.recW / 240)));
      line.setAttribute('stroke-dasharray', '6,4');
      svg.appendChild(line);
    }

    pts.forEach((pt, idx) => {
      const c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      c.setAttribute('cx', pt.camX);
      c.setAttribute('cy', pt.camY);
      c.setAttribute('r', Math.max(4, Math.round(m.recW / 120)));
      c.setAttribute('fill', idx === 0 ? '#38bdf8' : '#facc15');
      c.setAttribute('stroke', '#020617');
      c.setAttribute('stroke-width', '2');
      svg.appendChild(c);
    });
  };

  let rulerDragging = false;
  box.addEventListener('pointerdown', (evt) => {
    if (evt.button !== 0) return;
    if (state.rulerMode) {
      evt.preventDefault();
      const pt = toCameraCoords(evt);
      const m = getVideoMetrics();
      if (state.rulerPoints.length === 1) {
        state.rulerPoints.push(pt);
        rulerDragging = false;
      } else {
        state.rulerPoints = [pt];
        rulerDragging = true;
        box.setPointerCapture(evt.pointerId);
      }
      renderRulerOverlay(state.rulerPoints, m);
      const readout = document.getElementById('ruler-px-readout');
      if (state.rulerPoints.length === 1) {
        state.rulerPixelDist = 0;
        if (readout) readout.textContent = `Point 1 set at (${pt.camX}, ${pt.camY}) — Click or drag to Point 2`;
      } else if (state.rulerPoints.length === 2) {
        const [p1, p2] = state.rulerPoints;
        const dist = Math.hypot(p2.camX - p1.camX, p2.camY - p1.camY);
        state.rulerPixelDist = dist;
        if (readout) readout.textContent = `Line: ${dist.toFixed(1)} px (${m.recW}×${m.recH})`;
      }
      return;
    }
    const cropChk = document.getElementById('roi-crop-preview');
    if (cropChk && cropChk.checked) return;
    evt.preventDefault();
    dragStart = toCameraCoords(evt);
    box.setPointerCapture(evt.pointerId);
    updateDragOverlay(dragStart, dragStart);
  });

  box.addEventListener('pointermove', (evt) => {
    if (state.rulerMode && rulerDragging && state.rulerPoints.length >= 1) {
      const cur = toCameraCoords(evt);
      const p1 = state.rulerPoints[0];
      if (Math.hypot(cur.camX - p1.camX, cur.camY - p1.camY) >= 4) {
        const m = getVideoMetrics();
        state.rulerPoints = [p1, cur];
        renderRulerOverlay(state.rulerPoints, m);
        const dist = Math.hypot(cur.camX - p1.camX, cur.camY - p1.camY);
        state.rulerPixelDist = dist;
        const readout = document.getElementById('ruler-px-readout');
        if (readout) readout.textContent = `Line: ${dist.toFixed(1)} px (${m.recW}×${m.recH})`;
      }
      return;
    }
    if (!dragStart) return;
    const cur = toCameraCoords(evt);
    updateDragOverlay(dragStart, cur);
  });

  const finishDrag = (evt) => {
    if (state.rulerMode && rulerDragging) {
      rulerDragging = false;
      return;
    }
    if (!dragStart) return;
    const end = toCameraCoords(evt);
    const x0 = Math.min(dragStart.camX, end.camX);
    const y0 = Math.min(dragStart.camY, end.camY);
    const x1 = Math.max(dragStart.camX, end.camX);
    const y1 = Math.max(dragStart.camY, end.camY);
    const rw = x1 - x0;
    const rh = y1 - y0;
    dragStart = null;
    if (dragBox) dragBox.style.display = 'none';

    if (rw >= 16 && rh >= 16) {
      document.getElementById('roi-x').value = x0;
      document.getElementById('roi-y').value = y0;
      document.getElementById('roi-w').value = rw;
      document.getElementById('roi-h').value = rh;
      runPreflight();
    }
  };

  box.addEventListener('pointerup', finishDrag);
  box.addEventListener('pointercancel', () => {
    dragStart = null;
    if (dragBox) dragBox.style.display = 'none';
  });
}

async function initCameras() {
  const data = await api('/api/cameras');
  state.cameras = data.discovered || [];
  state.activeCameraId = data.active_camera_id;
  state.capabilities = data.capabilities;

  const badge = document.getElementById('active-camera-badge');
  const shutter = data.capabilities.shutter_type || 'rolling';
  badge.textContent = `${data.capabilities.name} (${shutter.toUpperCase()} SHUTTER)`;
  badge.className = `badge ${shutter}`;

  if (data.hardware) {
    state.hardware = data.hardware;
    updateThermalBadge(data.hardware);
    const hwBadge = document.getElementById('hw-model-badge');
    if (hwBadge) {
      hwBadge.textContent = `${data.hardware.model} (${data.hardware.total_ram_gb} GB RAM)`;
      hwBadge.title = `Detected ${data.hardware.model} with ${data.hardware.cpu_count} cores and ${data.hardware.total_ram_gb} GB total RAM (${data.hardware.available_ram_gb} GB free).`;
    }
    const maxWSelect = document.getElementById('proc-max-width');
    if (maxWSelect && data.hardware.recommended_max_width) {
      maxWSelect.value = String(data.hardware.recommended_max_width);
    }
  }

  if (data.current_controls) {
    if (data.current_controls.locked) {
      document.getElementById('ctrl-exposure').value = data.current_controls.exposure_us;
      document.getElementById('ctrl-gain').value = data.current_controls.gain;
      document.getElementById('ctrl-wb').value = data.current_controls.wb_temp_k;
    }
    const lockBadge = document.getElementById('lock-status-badge');
    if (lockBadge) {
      if (data.current_controls.locked) {
        lockBadge.textContent = `Locked (${data.current_controls.exposure_us} us, Gain ${data.current_controls.gain})`;
        lockBadge.className = 'badge global';
      } else {
        lockBadge.textContent = 'Auto-Adjust (Unlocked)';
        lockBadge.className = 'badge';
      }
    }
  }

  if (data.capabilities && Array.isArray(data.capabilities.resolutions)) {
    const resPreset = document.getElementById('rec-res-preset');
    if (resPreset) {
      const curW = parseInt(document.getElementById('rec-width').value, 10) || 640;
      const curH = parseInt(document.getElementById('rec-height').value, 10) || 480;
      const seen = new Set();
      resPreset.innerHTML = '';
      const allRes = [
        ...data.capabilities.resolutions,
        [640, 480],
        [1280, 720],
        [1456, 1088],
        [1920, 1080],
      ];
      allRes.forEach(([rw, rh]) => {
        const key = `${rw}x${rh}`;
        if (seen.has(key)) return;
        seen.add(key);
        const opt = document.createElement('option');
        opt.value = key;
        opt.textContent = `${rw} × ${rh} px`;
        if (rw === curW && rh === curH) opt.selected = true;
        resPreset.appendChild(opt);
      });
      const customOpt = document.createElement('option');
      customOpt.value = 'custom';
      customOpt.textContent = 'Custom Width × Height';
      resPreset.appendChild(customOpt);
    }
  }

  const warnBanner = document.getElementById('shutter-warning-banner');
  if (data.capabilities.rolling_shutter_warning) {
    warnBanner.textContent = data.capabilities.rolling_shutter_warning;
    warnBanner.style.display = 'block';
  } else {
    warnBanner.style.display = 'none';
  }

  const sel = document.getElementById('setup-camera-select');
  sel.innerHTML = '';
  const selB = document.getElementById('setup-camera-b-select');
  if (selB) selB.innerHTML = '';

  state.dualCamEnabled = Boolean(data.dual_camera_enabled);
  state.secondaryCameraId = data.secondary_camera_id || 'synthetic:1';
  if (state.secondaryCameraId === state.activeCameraId || state.secondaryCameraId === 'synthetic:1') {
    const hwSecond = state.cameras.find(
      (c) => (c.backend_type === 'picamera2' || c.backend_type === 'v4l2') && c.camera_id !== state.activeCameraId
    );
    if (hwSecond) {
      state.secondaryCameraId = hwSecond.camera_id;
    } else {
      const anyOther = state.cameras.find((c) => c.camera_id !== state.activeCameraId);
      if (anyOther) state.secondaryCameraId = anyOther.camera_id;
    }
  }
  state.dualSyncMode = data.dual_sync_mode || 'software';
  state.dualTier = data.dual_tier || null;

  state.cameras.forEach((c) => {
    const opt = document.createElement('option');
    opt.value = c.camera_id;
    opt.textContent = `${c.name} [${c.camera_id}] (${c.shutter_type} shutter)`;
    if (c.camera_id === state.activeCameraId) opt.selected = true;
    sel.appendChild(opt);

    if (selB) {
      const optB = document.createElement('option');
      optB.value = c.camera_id;
      optB.textContent = `${c.name} [${c.camera_id}] (${c.shutter_type} shutter)`;
      if (c.camera_id === state.secondaryCameraId) optB.selected = true;
      selB.appendChild(optB);
    }
  });

  const dualChk = document.getElementById('setup-dual-enable');
  const dualTierBadge = document.getElementById('setup-dual-tier-badge');
  const dualTierNote = document.getElementById('setup-dual-tier-note');
  const syncModeSel = document.getElementById('setup-sync-mode');
  if (syncModeSel) syncModeSel.value = state.dualSyncMode;

  if (dualChk) {
    const allowed = state.dualTier ? Boolean(state.dualTier.allowed) : state.cameras.length >= 2;
    dualChk.disabled = !allowed;
    dualChk.checked = allowed && state.dualCamEnabled;
    onToggleSetupDualCamera();
  }
  if (dualTierBadge && state.dualTier) {
    dualTierBadge.textContent = `Tier ${state.dualTier.tier}: ${state.dualTier.tier_label}`;
    dualTierBadge.className = state.dualTier.tier === 3
      ? 'badge global'
      : state.dualTier.tier === 2
        ? 'badge rolling'
        : 'badge';
  }
  if (dualTierNote && state.dualTier) {
    dualTierNote.textContent = state.dualTier.reason || '';
  }

  if (data.secondary_capabilities && data.secondary_capabilities.lens_info) {
    const lb = data.secondary_capabilities.lens_info;
    const fb = document.getElementById('setup-focal-b');
    const fovb = document.getElementById('setup-fov-b');
    const db = document.getElementById('setup-distance-b');
    if (fb) fb.value = lb.focal_length_mm || 4.0;
    if (fovb) fovb.value = lb.fov_degrees || 60.0;
    if (db) db.value = lb.target_distance_cm || 50.0;
  }

  updateDualCamUiVisibility();
  updateLivePreviewMode();

  if (data.capabilities.lens_info) {
    document.getElementById('setup-focal').value = data.capabilities.lens_info.focal_length_mm || 4.0;
    document.getElementById('setup-fov').value = data.capabilities.lens_info.fov_degrees || 60.0;
    const distEl = document.getElementById('setup-distance');
    if (distEl) {
      distEl.value = data.capabilities.lens_info.target_distance_cm || 50.0;
    }
    document.getElementById('setup-focus-mode').value = data.capabilities.lens_info.focus_mode || 'fixed';
    updateLiveScaleBadge(data.capabilities.lens_info);
  }

  if (!data.setup_completed) {
    openSetupModal();
  }
}

function updateLiveScaleBadge(lensInfo) {
  const badge = document.getElementById('live-scale-badge');
  const modalStatus = document.getElementById('setup-ruler-status');
  if (!lensInfo) return;
  if (lensInfo.ruler_mm_per_pixel && lensInfo.ruler_mm_per_pixel > 0) {
    const val = Number(lensInfo.ruler_mm_per_pixel).toFixed(4);
    const refW = lensInfo.ruler_ref_width_px || 640;
    if (badge) {
      badge.textContent = `Scale: ${val} mm/px (2-Pt Ruler @ ${refW}px)`;
      badge.className = 'badge global';
    }
    if (modalStatus) {
      modalStatus.textContent = `Active 2-Point Ruler: ${val} mm/px (at ${refW}px reference width).`;
    }
  } else {
    const fov = lensInfo.fov_degrees || 60.0;
    const distCm = lensInfo.target_distance_cm || 50.0;
    const recW = parseInt(document.getElementById('rec-width').value, 10) || 640;
    const widthMm = 2.0 * (distCm * 10.0) * Math.tan((fov * Math.PI) / 360.0);
    const mmPerPx = (widthMm / recW).toFixed(4);
    if (badge) {
      badge.textContent = `Scale: ~${mmPerPx} mm/px (Lens FOV @ ${distCm} cm)`;
      badge.className = 'badge';
    }
    if (modalStatus) {
      modalStatus.textContent = `Using Optical Lens FOV (${fov}° @ ${distCm} cm ≈ ${mmPerPx} mm/px).`;
    }
  }
}

function toggleRulerCalibrationMode(forceState = null) {
  state.rulerMode = forceState !== null ? Boolean(forceState) : !state.rulerMode;
  state.rulerPoints = [];
  state.rulerPixelDist = 0;
  const bar = document.getElementById('ruler-calib-bar');
  const btn = document.getElementById('btn-ruler-mode');
  const svg = document.getElementById('ruler-overlay-svg');
  const readout = document.getElementById('ruler-px-readout');
  if (bar) bar.style.display = state.rulerMode ? 'flex' : 'none';
  if (btn) {
    btn.className = state.rulerMode ? 'primary' : '';
    btn.textContent = state.rulerMode ? '2-Point Ruler (Active)' : '2-Point Ruler';
  }
  if (!state.rulerMode && svg) {
    svg.innerHTML = '';
    svg.style.display = 'none';
  }
  if (readout) {
    readout.textContent = 'Click 2 points on the video preview across an object of known size';
  }
}

function startRulerFromModal() {
  closeSetupModal();
  switchPage('record');
  toggleRulerCalibrationMode(true);
}

async function applyRulerCalibration() {
  if (!state.rulerPixelDist || state.rulerPixelDist < 2.0) {
    alert('Please click two points across an object of known length on the Live Preview image first.');
    return;
  }
  const knownMm = parseFloat(document.getElementById('ruler-known-mm').value) || 0;
  if (knownMm <= 0) {
    alert('Please enter a valid positive length in millimeters (mm).');
    return;
  }
  const recW = parseInt(document.getElementById('rec-width').value, 10) || 640;
  await api('/api/cameras/calibrate-ruler', {
    method: 'POST',
    body: JSON.stringify({
      pixel_distance: state.rulerPixelDist,
      known_length_mm: knownMm,
      frame_width: recW,
      clear: false,
    }),
  });
  toggleRulerCalibrationMode(false);
  await initCameras();
}

async function clearRulerCalibration() {
  await api('/api/cameras/calibrate-ruler', {
    method: 'POST',
    body: JSON.stringify({
      pixel_distance: 100.0,
      known_length_mm: 10.0,
      frame_width: 640,
      clear: true,
    }),
  });
  toggleRulerCalibrationMode(false);
  await initCameras();
}

function toggleMotionTrigger() {
  state.triggerArmed = !state.triggerArmed;
  const btn = document.getElementById('btn-arm-trigger');
  const badge = document.getElementById('trigger-status-badge');
  if (btn) {
    btn.textContent = state.triggerArmed ? 'Disarm Trigger (Armed & Listening...)' : 'Arm Motion Trigger';
    btn.className = state.triggerArmed ? 'primary' : '';
  }
  if (badge) {
    badge.textContent = state.triggerArmed
      ? 'Armed: Monitoring Live ROI for Spike (2.0s Pre-Roll Ready)'
      : 'Disarmed (Pre-Roll Buffer Active)';
    badge.className = state.triggerArmed ? 'badge global' : 'badge';
  }
}

async function saveCameraSetup() {
  const camId = document.getElementById('setup-camera-select').value;
  const distEl = document.getElementById('setup-distance');
  const dualChk = document.getElementById('setup-dual-enable');
  const dualEnabled = Boolean(dualChk && dualChk.checked && !dualChk.disabled);
  const camBSelect = document.getElementById('setup-camera-b-select');
  const camBId = camBSelect ? camBSelect.value : 'synthetic:1';
  const syncModeSel = document.getElementById('setup-sync-mode');
  const syncMode = syncModeSel ? syncModeSel.value : 'software';

  await api(`/api/cameras/${encodeURIComponent(camId)}/lens`, {
    method: 'POST',
    body: JSON.stringify({
      focal_length_mm: parseFloat(document.getElementById('setup-focal').value) || 4.0,
      fov_degrees: parseFloat(document.getElementById('setup-fov').value) || 60.0,
      target_distance_cm: distEl ? parseFloat(distEl.value) || 50.0 : 50.0,
      focus_mode: document.getElementById('setup-focus-mode').value,
    }),
  });

  if (dualEnabled && camBId) {
    const fb = document.getElementById('setup-focal-b');
    const fovb = document.getElementById('setup-fov-b');
    const db = document.getElementById('setup-distance-b');
    await api(`/api/cameras/${encodeURIComponent(camBId)}/lens`, {
      method: 'POST',
      body: JSON.stringify({
        focal_length_mm: fb ? parseFloat(fb.value) || 4.0 : 4.0,
        fov_degrees: fovb ? parseFloat(fovb.value) || 60.0 : 60.0,
        target_distance_cm: db ? parseFloat(db.value) || 50.0 : 50.0,
        focus_mode: 'fixed',
      }),
    });
  }

  await api('/api/cameras/select', {
    method: 'POST',
    body: JSON.stringify({
      camera_id: camId,
      complete_setup: true,
      dual_camera_enabled: dualEnabled,
      secondary_camera_id: camBId,
      dual_sync_mode: syncMode,
    }),
  });
  closeSetupModal();
  await initCameras();
  updateLivePreviewMode();
  await runPreflight();
}

async function lockCameraControls() {
  const payload = {
    width: parseInt(document.getElementById('rec-width').value, 10) || 640,
    height: parseInt(document.getElementById('rec-height').value, 10) || 480,
    fps: parseInt(document.getElementById('rec-fps').value, 10) || 30,
    exposure_us: parseInt(document.getElementById('ctrl-exposure').value, 10) || 10000,
    gain: parseFloat(document.getElementById('ctrl-gain').value) || 1.0,
    wb_temp_k: parseInt(document.getElementById('ctrl-wb').value, 10) || 4600,
    focus_position: parseFloat(document.getElementById('ctrl-focus').value) || 0.0,
    roi: getOptionalRoi(),
  };
  const res = await api('/api/cameras/lock', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
  document.getElementById('ctrl-exposure').value = res.controls.exposure_us;
  document.getElementById('ctrl-gain').value = res.controls.gain;
  document.getElementById('ctrl-wb').value = res.controls.wb_temp_k;
  const lockBadge = document.getElementById('lock-status-badge');
  lockBadge.textContent = `Locked (${res.controls.exposure_us} us, Gain ${res.controls.gain})`;
  lockBadge.className = 'badge global';
  await runPreflight();
}

async function unlockCameraControls() {
  await api('/api/cameras/unlock', {
    method: 'POST',
  });
  const lockBadge = document.getElementById('lock-status-badge');
  lockBadge.textContent = 'Auto-Adjust (Unlocked)';
  lockBadge.className = 'badge';
}

async function runPreflight() {
  const cropChk = document.getElementById('roi-crop-preview');
  const payload = {
    width: parseInt(document.getElementById('rec-width').value, 10) || 640,
    height: parseInt(document.getElementById('rec-height').value, 10) || 480,
    fps: parseFloat(document.getElementById('rec-fps').value) || 30.0,
    duration_s: parseFloat(document.getElementById('rec-duration').value) || 6.0,
    min_freq_hz: 0.5,
    max_freq_hz: parseFloat(document.getElementById('rec-max-hz').value) || 3.5,
    storage_format: document.getElementById('rec-format').value,
    roi: getOptionalRoi(),
    crop_preview: Boolean(cropChk && cropChk.checked),
  };
  const res = await api('/api/record/preflight', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
  const banner = document.getElementById('preflight-banner');
  const nyq = res.nyquist;
  const st = res.storage;

  const roiLabel = res.active_roi
    ? ` &bull; <strong>ROI Crop:</strong> ${res.active_roi[2]}&times;${res.active_roi[3]} at (${res.active_roi[0]},${res.active_roi[1]})`
    : ` &bull; <strong>Frame:</strong> ${payload.width}&times;${payload.height} (Full)`;

  const dualSyncLabel = res.dual_sync && res.dual_sync.enabled
    ? ` &bull; <strong style="color: var(--accent);">${res.dual_sync.badge_text}</strong>`
    : '';

  let html = `<strong>Nyquist Limit:</strong> ${nyq.nyquist_limit_hz} Hz (Freq Resolution &Delta;f = ${nyq.freq_resolution_hz} Hz)${roiLabel}${dualSyncLabel} &bull; `;
  html += `<strong>Est. Size:</strong> ~${st.estimated_mb} MB &bull; <strong>Free Disk:</strong> ${st.free_gb} GB`;

  if (!nyq.valid || !st.sufficient_space) {
    banner.className = 'banner danger';
    html += `<br/><span>${nyq.warnings.join(' ')}</span>`;
  } else if (nyq.warnings.length > 0) {
    banner.className = 'banner warning';
    html += `<br/><span>${nyq.warnings.join(' ')}</span>`;
  } else {
    banner.className = 'banner success';
  }
  banner.innerHTML = html;
}

async function startRecording(options = {}) {
  const btn = document.getElementById('btn-record');
  const resBanner = document.getElementById('record-result-banner');
  if (state.triggerRecordingInFlight) return;
  state.triggerRecordingInFlight = true;
  btn.disabled = true;
  const includePreroll = Boolean(options.includePreroll);
  btn.textContent = includePreroll
    ? 'Auto-Triggered! Prepending 2.0s pre-roll & recording...'
    : 'Recording in progress (controls locked)...';
  resBanner.style.display = 'none';

  try {
    const payload = {
      title: document.getElementById('rec-title').value || formatDefaultClipTitle(),
      duration_s: parseFloat(document.getElementById('rec-duration').value) || 8.0,
      width: parseInt(document.getElementById('rec-width').value, 10) || 640,
      height: parseInt(document.getElementById('rec-height').value, 10) || 480,
      fps: parseInt(document.getElementById('rec-fps').value, 10) || 30,
      storage_format: document.getElementById('rec-format').value,
      roi: getOptionalRoi(),
      include_preroll: includePreroll,
      preroll_seconds: options.prerollSeconds || 2.0,
      controls: {
        exposure_us: parseInt(document.getElementById('ctrl-exposure').value, 10) || 10000,
        gain: parseFloat(document.getElementById('ctrl-gain').value) || 1.0,
        wb_temp_k: parseInt(document.getElementById('ctrl-wb').value, 10) || 4600,
        focus_position: parseFloat(document.getElementById('ctrl-focus').value) || 0.0,
      },
    };
    const meta = await api('/api/record/start', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    if (meta.controls) {
      document.getElementById('ctrl-exposure').value = meta.controls.exposure_us;
      document.getElementById('ctrl-gain').value = meta.controls.gain;
      document.getElementById('ctrl-wb').value = meta.controls.wb_temp_k;
      const lockBadge = document.getElementById('lock-status-badge');
      if (lockBadge) {
        lockBadge.textContent = `Locked (${meta.controls.exposure_us} us, Gain ${meta.controls.gain})`;
        lockBadge.className = 'badge global';
      }
    }
    resBanner.style.display = 'block';
    resBanner.className = 'banner success';
    resBanner.innerHTML = `
      <strong>Clip Recorded:</strong> ${meta.title} (${meta.clip_id}) &bull; <strong>${meta.width}&times;${meta.height} px</strong><br/>
      Achieved Sample Rate: <strong>${meta.achieved_fps} FPS</strong> (Target ${meta.target_fps} FPS, Jitter &sigma; = ${meta.jitter_std_ms} ms) &bull;
      Frames: ${meta.frame_count} &bull;
      <a href="#" style="color: var(--accent);" onclick="openClipInProcess('${meta.clip_id}'); return false;">Process This Clip &rarr;</a>
    `;
    refreshDefaultClipTitleIfUnedited();
    await loadLibrary();
  } catch (err) {
    resBanner.style.display = 'block';
    resBanner.className = 'banner danger';
    resBanner.textContent = `Recording failed: ${err.message}`;
  } finally {
    state.triggerRecordingInFlight = false;
    btn.disabled = false;
    btn.textContent = '2. Record Lossless Clip';
  }
}

function openClipInProcess(clipId) {
  switchPage('process');
  loadLibrary().then(() => {
    document.getElementById('proc-clip').value = clipId;
    onSelectProcessClip();
  });
}

async function initPresetsAndMethods() {
  const data = await api('/api/presets');
  state.presets = data.presets || [];
  state.methods = data.methods || [];

  const methodSel = document.getElementById('proc-method');
  methodSel.innerHTML = '';
  state.methods.forEach((m) => {
    const opt = document.createElement('option');
    opt.value = m.key;
    opt.textContent = `${m.name} [${m.speed_rating}]`;
    methodSel.appendChild(opt);
  });

  const presetSel = document.getElementById('proc-preset');
  presetSel.innerHTML = '';
  state.presets.forEach((p) => {
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = `${p.name} (${p.low_hz}–${p.high_hz} Hz)`;
    presetSel.appendChild(opt);
  });
  const customOpt = document.createElement('option');
  customOpt.value = 'custom';
  customOpt.textContent = 'Custom Frequency Band';
  presetSel.appendChild(customOpt);

  applyPresetSelection();
}

function applyPresetSelection() {
  const presetId = document.getElementById('proc-preset').value;
  const preset = state.presets.find((p) => p.id === presetId);
  if (!preset) return;
  document.getElementById('proc-low-hz').value = preset.low_hz;
  document.getElementById('proc-high-hz').value = preset.high_hz;
  document.getElementById('proc-alpha').value = preset.default_alpha;
  if (preset.recommended_method) {
    document.getElementById('proc-method').value = preset.recommended_method;
  }
  updateJobTimeEstimate();
}

async function loadLibrary() {
  const data = await api('/api/library');
  state.clips = data.clips || [];

  const grid = document.getElementById('library-grid');
  const procSelect = document.getElementById('proc-clip');
  const currentSelected = procSelect.value;

  grid.innerHTML = '';
  procSelect.innerHTML = '';

  if (state.clips.length === 0) {
    grid.innerHTML = '<p style="color: var(--text-muted);">No recordings yet. Go to 1. Record to capture a lossless clip.</p>';
    return;
  }

  state.clips.forEach((c) => {
    const opt = document.createElement('option');
    opt.value = c.clip_id;
    opt.textContent = `${c.title} (${c.width}x${c.height} @ ${c.achieved_fps} FPS, ${c.duration_s}s)`;
    procSelect.appendChild(opt);

    const card = document.createElement('div');
    card.className = 'card';
    card.style.marginBottom = '0';
    const mb = (c.file_size_bytes / (1024 * 1024)).toFixed(2);
    const jobCount = (c.jobs || []).length;
    const dualBadge = c.dual_camera
      ? `<span class="badge global" title="Synchronized Dual-Camera Clip (${c.sync_mode || 'software'} sync)">DUAL CAM (A+B)</span>`
      : '';
    const dualMetaLine = c.dual_camera
      ? `<br/><span style="color: var(--accent);">Sync: <strong>${(c.sync_mode || 'software').toUpperCase()}</strong> (&Delta;t ${c.sync_dt_mean_ms || 0} ms) &bull; Cam B: ${c.secondary_camera_id || 'Cam B'}</span>`
      : '';
    card.innerHTML = `
      <div class="video-box" style="margin-bottom: 0.75rem;">
        <img src="/api/library/${c.clip_id}/files/thumb.jpg" alt="${c.title}" />
      </div>
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.35rem; gap: 0.35rem; flex-wrap: wrap;">
        <strong>${c.title}</strong>
        <div style="display: flex; gap: 0.3rem; align-items: center;">
          ${dualBadge}
          <span class="badge ${c.shutter_type}">${c.shutter_type}</span>
        </div>
      </div>
      <p style="font-size: 0.8rem; color: var(--text-muted); margin-bottom: 0.75rem;">
        ${c.width}&times;${c.height} &bull; Achieved <strong>${c.achieved_fps} FPS</strong> (Jitter ${c.jitter_std_ms || 0} ms)<br/>
        ${c.frame_count} frames (${c.duration_s}s) &bull; ${mb} MB &bull; ${jobCount} processed run(s)${dualMetaLine}
      </p>
      <div style="display: flex; gap: 0.45rem; flex-wrap: wrap;">
        <button class="primary" onclick="openClipInProcess('${c.clip_id}')">Process / Compare</button>
        <a class="btn" href="/api/library/${c.clip_id}/files/preview.mp4" download="${c.clip_id}.mp4">MP4</a>
        <button class="danger" onclick="deleteClip('${c.clip_id}')">Delete</button>
      </div>
    `;
    grid.appendChild(card);
  });

  if (currentSelected && state.clips.some((c) => c.clip_id === currentSelected)) {
    procSelect.value = currentSelected;
  }
  onSelectProcessClip();
}

async function deleteClip(clipId) {
  await api(`/api/library/${clipId}`, { method: 'DELETE' });
  await loadLibrary();
}

function toggleUploadDrawer() {
  const drawer = document.getElementById('upload-clip-drawer');
  if (!drawer) return;
  const isHidden = drawer.style.display === 'none' || !drawer.style.display;
  drawer.style.display = isHidden ? 'block' : 'none';
}

async function generateDemoShowcaseClips() {
  const btn = document.getElementById('btn-generate-demos');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Synthesizing 2 Demo Clips...';
  }
  try {
    await api('/api/library/demo-clips', { method: 'POST' });
    await loadLibrary();
    if (btn) {
      btn.textContent = 'Demo Clips Ready!';
      setTimeout(() => {
        btn.textContent = 'Generate Demo Showcase Clips';
      }, 1800);
    }
  } catch (err) {
    alert(`Failed to generate demo clips: ${err.message}`);
    if (btn) btn.textContent = 'Generate Demo Showcase Clips';
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function uploadExternalClip() {
  const fileEl = document.getElementById('upload-file-input');
  const titleEl = document.getElementById('upload-clip-title');
  const fpsEl = document.getElementById('upload-true-fps');
  const statusEl = document.getElementById('upload-status-text');
  const btn = document.getElementById('btn-do-upload');

  if (!fileEl || !fileEl.files || fileEl.files.length === 0) {
    alert('Please select a video file (.mp4, .mov, .avi, .mkv) to upload.');
    return;
  }

  const file = fileEl.files[0];
  const formData = new FormData();
  formData.append('file', file);
  formData.append('title', titleEl && titleEl.value.trim() ? titleEl.value.trim() : '');
  const trueFpsRaw = fpsEl && fpsEl.value.trim() ? parseFloat(fpsEl.value.trim()) : 0;
  if (trueFpsRaw > 0) {
    formData.append('true_fps', String(trueFpsRaw));
  }

  if (btn) btn.disabled = true;
  if (statusEl) statusEl.textContent = 'Decoding video frames & generating lossless archive...';

  try {
    const res = await fetch('/api/library/upload', {
      method: 'POST',
      body: formData,
    });
    if (!res.ok) {
      const errText = await res.text();
      throw new Error(errText || `HTTP ${res.status}`);
    }
    const meta = await res.json();
    if (statusEl) {
      statusEl.textContent = `Imported "${meta.title}" (${meta.frame_count} frames @ ${meta.achieved_fps} FPS).`;
    }
    fileEl.value = '';
    if (titleEl) titleEl.value = '';
    if (fpsEl) fpsEl.value = '';
    await loadLibrary();
    setTimeout(() => {
      toggleUploadDrawer();
      if (statusEl) statusEl.textContent = '';
    }, 1100);
  } catch (err) {
    if (statusEl) statusEl.textContent = `Upload failed: ${err.message}`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

function onSelectProcessClip() {
  const clipId = document.getElementById('proc-clip').value;
  const clip = state.clips.find((c) => c.clip_id === clipId);
  const streamWrap = document.getElementById('proc-target-stream-wrap');
  if (streamWrap) {
    if (clip && clip.dual_camera) {
      streamWrap.classList.remove('dual-cam-only');
    } else {
      streamWrap.classList.add('dual-cam-only');
    }
  }
  updateJobTimeEstimate();
  const clipJob = state.jobs.find((j) => j.clip_id === clipId && j.status === 'completed');
  if (clipJob) {
    selectJobForResultView(clipJob.job_id);
  }
  loadComparisonForSelectedClip();
}

async function updateJobTimeEstimate() {
  const clipId = document.getElementById('proc-clip').value;
  const clip = state.clips.find((c) => c.clip_id === clipId);
  const method = document.getElementById('proc-method').value || 'eulerian';
  const maxW = parseInt(document.getElementById('proc-max-width').value, 10) || 640;
  const frames = clip ? clip.frame_count : 150;
  const w = clip ? Math.min(clip.width, maxW) : maxW;
  const h = clip ? clip.height : 480;

  const res = await api(`/api/jobs/estimate?frame_count=${frames}&width=${w}&height=${h}&method=${method}`);
  const hwName = state.hardware ? state.hardware.model : 'Raspberry Pi';
  document.getElementById('job-estimate-banner').textContent =
    `Estimated offline processing time on ${hwName}: ~${res.estimated_seconds}s (${frames} frames @ ${w}x${h})`;
}

async function autoDetectBandForClip() {
  const clipId = document.getElementById('proc-clip').value;
  if (!clipId) return;
  const banner = document.getElementById('job-estimate-banner');
  banner.textContent = 'Analyzing clip ROI spectrum for dominant oscillation peak...';
  const spec = await api(`/api/library/${clipId}/auto-band`);
  const sug = spec.suggested_band;
  document.getElementById('proc-low-hz').value = sug.low_hz;
  document.getElementById('proc-high-hz').value = sug.high_hz;
  banner.className = 'banner success';
  banner.textContent = `Auto-Detected Dominant Peak: ${sug.peak_hz} Hz (${sug.peak_bpm} BPM) -> Band set to [${sug.low_hz}, ${sug.high_hz}] Hz`;
}

async function submitProcessingJob() {
  const clipId = document.getElementById('proc-clip').value;
  if (!clipId) return;
  const chromaEl = document.getElementById('proc-chroma');
  const heatEl = document.getElementById('proc-heat-thresh');
  const stabEl = document.getElementById('proc-stabilize');
  const bgGateEl = document.getElementById('proc-bg-gate');
  const trimStartEl = document.getElementById('proc-trim-start');
  const trimEndEl = document.getElementById('proc-trim-end');
  const clip = state.clips.find((c) => c.clip_id === clipId);
  const targetStreamEl = document.getElementById('proc-target-stream');
  const targetStream = clip && clip.dual_camera && targetStreamEl ? targetStreamEl.value : 'cam_a';
  const params = {
    method: document.getElementById('proc-method').value,
    preset_id: document.getElementById('proc-preset').value,
    low_hz: parseFloat(document.getElementById('proc-low-hz').value) || 0.8,
    high_hz: parseFloat(document.getElementById('proc-high-hz').value) || 3.0,
    alpha: parseFloat(document.getElementById('proc-alpha').value) || 25.0,
    pyramid_levels: parseInt(document.getElementById('proc-levels').value, 10) || 4,
    spatial_blur_sigma: parseFloat(document.getElementById('proc-blur').value) || 1.5,
    max_width: parseInt(document.getElementById('proc-max-width').value, 10) || 640,
    filter_type: document.getElementById('proc-filter').value,
    target_stream: targetStream,
    chrom_attenuation: chromaEl ? parseFloat(chromaEl.value) || 0.2 : 0.2,
    heatmap_threshold: heatEl ? parseFloat(heatEl.value) || 0.2 : 0.2,
    stabilize_subpixel: Boolean(stabEl && stabEl.checked),
    gate_background_noise: Boolean(bgGateEl && bgGateEl.checked),
    trim_start_s: trimStartEl ? Math.max(0.0, parseFloat(trimStartEl.value) || 0.0) : 0.0,
    trim_end_s: trimEndEl ? Math.max(0.0, parseFloat(trimEndEl.value) || 0.0) : 0.0,
  };
  await api('/api/jobs', {
    method: 'POST',
    body: JSON.stringify({ clip_id: clipId, params, run_async: true }),
  });
  await loadJobs();
}

async function cancelJob(jobId) {
  await api(`/api/jobs/${jobId}/cancel`, { method: 'POST' });
  await loadJobs();
}

async function loadJobs() {
  const data = await api('/api/jobs');
  state.jobs = data.jobs || [];

  const listEl = document.getElementById('jobs-list');
  const analysisSelect = document.getElementById('analysis-job-select');
  const baselineSelect = document.getElementById('analysis-baseline-select');
  const prevAnalysisJob = analysisSelect.value;
  const prevBaselineJob = baselineSelect ? baselineSelect.value : '';

  listEl.innerHTML = '';
  analysisSelect.innerHTML = '';
  if (baselineSelect) {
    baselineSelect.innerHTML = '<option value="">None (Single Job)</option>';
  }

  const completedJobs = state.jobs.filter((j) => j.status === 'completed');
  completedJobs.forEach((j) => {
    const opt = document.createElement('option');
    opt.value = j.job_id;
    const hz = j.result ? j.result.dominant_hz : 0;
    const bpm = j.result ? j.result.dominant_bpm : 0;
    const labelText = `${j.method.toUpperCase()} — ${hz} Hz (${bpm} BPM) [${j.job_id}]`;
    opt.textContent = labelText;
    analysisSelect.appendChild(opt);
    if (baselineSelect) {
      const bOpt = document.createElement('option');
      bOpt.value = j.job_id;
      bOpt.textContent = labelText;
      baselineSelect.appendChild(bOpt);
    }
  });

  if (prevAnalysisJob && completedJobs.some((j) => j.job_id === prevAnalysisJob)) {
    analysisSelect.value = prevAnalysisJob;
  }
  if (baselineSelect && prevBaselineJob && completedJobs.some((j) => j.job_id === prevBaselineJob)) {
    baselineSelect.value = prevBaselineJob;
  }

  if (state.jobs.length === 0) {
    listEl.innerHTML = '<p style="color: var(--text-muted); font-size: 0.85rem;">No processing jobs submitted yet.</p>';
    return;
  }

  state.jobs.slice(0, 10).forEach((j) => {
    const pct = Math.round((j.progress || 0) * 100);
    const row = document.createElement('div');
    row.style.borderBottom = '1px solid var(--border)';
    row.style.padding = '0.65rem 0';

    let actionBtn = '';
    if (j.status === 'queued' || j.status === 'running') {
      actionBtn = `<button class="danger" onclick="cancelJob('${j.job_id}')">Cancel</button>`;
    } else if (j.status === 'completed') {
      actionBtn = `
        <button class="primary" onclick="selectJobForResultView('${j.job_id}')">View</button>
        <button onclick="openJobInAnalysis('${j.job_id}')">Spectrum</button>
      `;
    }

    const resSummary = j.result && j.result.dominant_hz !== undefined
      ? ` &bull; Peak: <strong>${j.result.dominant_hz} Hz (${j.result.dominant_bpm} BPM)</strong>`
      : j.error_message
      ? ` &bull; <span style="color: var(--danger);">${j.error_message}</span>`
      : '';

    row.innerHTML = `
      <div style="display: flex; justify-content: space-between; align-items: center; gap: 0.5rem;">
        <div>
          <strong>${j.method.toUpperCase()}</strong>
          <span style="font-size: 0.8rem; color: var(--text-muted);">(${j.params.low_hz}–${j.params.high_hz} Hz, &alpha;=${j.params.alpha})</span>
          <span class="badge">${j.status} (${pct}%)</span>
          ${resSummary}
        </div>
        <div style="display: flex; gap: 0.4rem;">${actionBtn}</div>
      </div>
      <div class="progress-track"><div class="progress-fill" style="width: ${pct}%;"></div></div>
    `;
    listEl.appendChild(row);
  });

  if (completedJobs.length > 0) {
    const newestId = completedJobs[0].job_id;
    if (!state.activeResultJob || state.lastLatestCompletedJobId !== newestId) {
      state.lastLatestCompletedJobId = newestId;
      selectJobForResultView(newestId);
    }
  }
  await loadComparisonForSelectedClip();
}

function selectJobForResultView(jobId) {
  const job = state.jobs.find((j) => j.job_id === jobId);
  if (!job || job.status !== 'completed') return;
  state.activeResultJob = job;
  const optDual = document.getElementById('opt-dual-sync-mode');
  const modeSel = document.getElementById('view-output-mode');
  const isDualJob = Boolean(job.result && (job.result.dual_camera || (job.result.files && job.result.files.dual_sync_mp4)));
  if (optDual) {
    if (isDualJob) {
      optDual.classList.remove('dual-cam-only');
    } else {
      optDual.classList.add('dual-cam-only');
      if (modeSel && modeSel.value === 'dual_sync_mp4') {
        modeSel.value = 'comparison_sbs_mp4';
      }
    }
  }
  updateComparisonVideoMode();
}

function toggleEpicenterOverlay() {
  state.showEpicenter = !state.showEpicenter;
  const btn = document.getElementById('btn-toggle-epicenter');
  if (btn) {
    btn.textContent = state.showEpicenter ? 'Epicenter Crosshair: ON' : 'Epicenter Crosshair: OFF';
    btn.className = state.showEpicenter ? 'primary' : '';
  }
  updateEpicenterMarkerPosition();
}

function updateEpicenterMarkerPosition() {
  const marker = document.getElementById('epicenter-marker');
  const label = document.getElementById('epicenter-label');
  const readout = document.getElementById('epicenter-readout');
  const job = state.activeResultJob;
  const epi = job && job.result && (job.result.epicenter || (job.result.extra_metrics && job.result.extra_metrics.epicenter));

  if (readout) {
    readout.textContent = epi
      ? `Epicenter: (${epi.x}, ${epi.y})`
      : 'Epicenter: —';
  }
  if (!marker) return;
  if (!state.showEpicenter || !epi) {
    marker.style.display = 'none';
    return;
  }

  const modeKey = document.getElementById('view-output-mode').value;
  let relX = epi.rel_x !== undefined ? epi.rel_x : 0.5;
  const relY = epi.rel_y !== undefined ? epi.rel_y : 0.5;
  if (modeKey === 'comparison_sbs_mp4') {
    // In Side-by-Side mode, place the marker on the right (Magnified) panel
    relX = 0.5 + 0.5 * relX;
  }
  marker.style.display = 'block';
  marker.style.left = `${(relX * 100).toFixed(2)}%`;
  marker.style.top = `${(relY * 100).toFixed(2)}%`;
  if (label) {
    label.textContent = `EPICENTER (${epi.x},${epi.y})`;
  }
}

function updateComparisonVideoMode() {
  const job = state.activeResultJob;
  if (!job || !job.result || !job.result.files) return;
  const modeKey = document.getElementById('view-output-mode').value;
  const engineSel = document.getElementById('view-player-engine');
  const engine = engineSel ? engineSel.value : 'mjpeg';
  const speedSel = document.getElementById('view-playback-speed');
  const speed = speedSel ? parseFloat(speedSel.value) || 1.0 : 1.0;
  const strobeSel = document.getElementById('view-strobe-mode');
  const strobeVal = strobeSel ? strobeSel.value : 'off';

  let strobeHz = 0.0;
  const domHz = job.result.dominant_hz || 2.0;
  if (strobeVal === 'beat_0.5') {
    strobeHz = Math.max(0.4, Math.abs(domHz - 0.5));
  } else if (strobeVal === 'beat_0.2') {
    strobeHz = Math.max(0.4, Math.abs(domHz - 0.2));
  }

  const defaultFileMap = {
    comparison_sbs_mp4: 'comparison_sbs.mp4',
    dual_sync_mp4: 'dual_sync.mp4',
    magnified_mp4: 'magnified_share.mp4',
    heatmap_mp4: 'heatmap.mp4',
    phase_ripple_mp4: 'phase_ripple.mp4',
    quiver_vectors_mp4: 'quiver_vectors.mp4',
  };
  const filename = job.result.files[modeKey] || defaultFileMap[modeKey] || 'comparison_sbs.mp4';

  const emptyHint = document.getElementById('result-empty-hint');
  if (emptyHint) emptyHint.style.display = 'none';

  const mjpegImg = document.getElementById('result-mjpeg-player');
  const videoEl = document.getElementById('result-video-player');
  const wipeBox = document.getElementById('split-wipe-container');
  const wipeOrig = document.getElementById('wipe-orig-mjpeg');
  const wipeMag = document.getElementById('wipe-mag-mjpeg');

  if (modeKey === 'split_wipe') {
    if (videoEl) {
      videoEl.pause();
      videoEl.removeAttribute('src');
      videoEl.style.display = 'none';
    }
    if (mjpegImg) {
      mjpegImg.removeAttribute('src');
      mjpegImg.style.display = 'none';
    }
    if (wipeBox) wipeBox.style.display = 'block';
    const ts = Date.now();
    if (wipeOrig) {
      wipeOrig.src = `/api/jobs/${job.job_id}/stream/original_share.mp4?speed=${speed}&strobe_hz=${strobeHz.toFixed(2)}&t=${ts}`;
    }
    if (wipeMag) {
      wipeMag.src = `/api/jobs/${job.job_id}/stream/magnified_share.mp4?speed=${speed}&strobe_hz=${strobeHz.toFixed(2)}&t=${ts}`;
    }
  } else {
    if (wipeBox) wipeBox.style.display = 'none';
    if (wipeOrig) wipeOrig.removeAttribute('src');
    if (wipeMag) wipeMag.removeAttribute('src');

    if (engine === 'mjpeg' || strobeHz > 0) {
      if (videoEl) {
        videoEl.pause();
        videoEl.removeAttribute('src');
        videoEl.style.display = 'none';
      }
      if (mjpegImg) {
        mjpegImg.style.display = 'block';
        mjpegImg.src = `/api/jobs/${job.job_id}/stream/${filename}?speed=${speed}&strobe_hz=${strobeHz.toFixed(2)}&t=${Date.now()}`;
      }
    } else {
      if (mjpegImg) {
        mjpegImg.removeAttribute('src');
        mjpegImg.style.display = 'none';
      }
      if (videoEl) {
        videoEl.style.display = 'block';
        videoEl.onerror = () => {
          if (engineSel) engineSel.value = 'mjpeg';
          updateComparisonVideoMode();
        };
        videoEl.src = `/api/jobs/${job.job_id}/files/${filename}?t=${Date.now()}`;
        videoEl.playbackRate = speed;
        videoEl.play().catch(() => {});
      }
    }
  }

  updateEpicenterMarkerPosition();

  const gifModeKey = modeKey === 'split_wipe' ? 'comparison_sbs_mp4' : modeKey;
  const dualSyncBtn =
    job.result.dual_camera || (job.result.files && job.result.files.dual_sync_mp4)
      ? `<a class="btn" href="/api/jobs/${job.job_id}/files/dual_sync.mp4" download="${job.job_id}_dual_sync.mp4">Dual A+B Sync MP4</a>`
      : '';
  const dlBar = document.getElementById('result-download-bar');
  dlBar.innerHTML = `
    <a class="btn primary" href="/api/jobs/${job.job_id}/files/comparison_sbs.mp4" download="${job.job_id}_sbs.mp4">Side-by-Side MP4</a>
    ${dualSyncBtn}
    <a class="btn" href="/api/jobs/${job.job_id}/files/magnified_share.mp4" download="${job.job_id}_magnified.mp4">Magnified MP4</a>
    <a class="btn" href="/api/jobs/${job.job_id}/files/heatmap.mp4" download="${job.job_id}_heatmap.mp4">Heatmap MP4</a>
    <a class="btn" href="/api/jobs/${job.job_id}/files/phase_ripple.mp4" download="${job.job_id}_ripple.mp4">Phase Ripple MP4</a>
    <a class="btn" href="/api/jobs/${job.job_id}/files/quiver_vectors.mp4" download="${job.job_id}_quiver.mp4">Quiver MP4</a>
    <a class="btn" style="border-color: var(--accent); color: var(--accent);" href="/api/jobs/${job.job_id}/gif?mode=${encodeURIComponent(gifModeKey)}" download="${job.job_id}_${gifModeKey}.gif" title="Export a looping palette-optimized GIF of the active output mode">Export Looping GIF</a>
    <a class="btn" href="/api/jobs/${job.job_id}/files/signal.csv" download="${job.job_id}_signal.csv">Signal CSV</a>
  `;
}

function initSplitWipeComparator() {
  const wipeBox = document.getElementById('split-wipe-container');
  if (!wipeBox) return;
  let dragging = false;

  const setWipeFromEvent = (evt) => {
    const rect = wipeBox.getBoundingClientRect();
    const relX = Math.max(0.02, Math.min(0.98, (evt.clientX - rect.left) / Math.max(1, rect.width)));
    wipeBox.style.setProperty('--wipe-pct', `${(relX * 100).toFixed(2)}%`);
  };

  wipeBox.addEventListener('pointerdown', (evt) => {
    if (evt.button !== 0) return;
    evt.preventDefault();
    dragging = true;
    wipeBox.setPointerCapture(evt.pointerId);
    setWipeFromEvent(evt);
  });
  wipeBox.addEventListener('pointermove', (evt) => {
    if (!dragging) return;
    setWipeFromEvent(evt);
  });
  const endDrag = () => {
    dragging = false;
  };
  wipeBox.addEventListener('pointerup', endDrag);
  wipeBox.addEventListener('pointercancel', endDrag);
}

async function loadComparisonForSelectedClip() {
  const clipId = document.getElementById('proc-clip').value;
  const wrap = document.getElementById('comparison-table-wrap');
  if (!clipId) {
    wrap.innerHTML = '<p style="color: var(--text-muted); font-size: 0.85rem;">Select a clip to compare methods.</p>';
    return;
  }
  const comp = await api(`/api/compare/${clipId}`);
  if (!comp.comparisons || comp.comparisons.length === 0) {
    wrap.innerHTML = '<p style="color: var(--text-muted); font-size: 0.85rem;">Run one or more methods on this clip to compare results side-by-side.</p>';
    return;
  }

  let html = `
    <p style="font-size: 0.82rem; color: var(--text-muted); margin-bottom: 0.6rem;">
      Comparing <strong>${comp.job_count}</strong> processed run(s) &bull; Inter-method frequency spread: <strong>${comp.frequency_spread_hz} Hz</strong>
    </p>
    <table>
      <thead>
        <tr>
          <th>Method</th>
          <th>Band (Hz)</th>
          <th>&alpha;</th>
          <th>Peak (Hz / BPM)</th>
          <th>SNR</th>
          <th>Action</th>
        </tr>
      </thead>
      <tbody>
  `;
  comp.comparisons.forEach((c) => {
    html += `
      <tr>
        <td><strong>${c.method.toUpperCase()}</strong></td>
        <td>${c.params.low_hz}–${c.params.high_hz}</td>
        <td>${c.params.alpha}</td>
        <td><strong>${c.dominant_hz} Hz</strong> (${c.dominant_bpm} BPM)</td>
        <td>${c.snr_db} dB</td>
        <td>
          <button onclick="selectJobForResultView('${c.job_id}')">Play</button>
          <button onclick="openJobInAnalysis('${c.job_id}')">Analyze</button>
        </td>
      </tr>
    `;
  });
  html += '</tbody></table>';
  wrap.innerHTML = html;
}

function openJobInAnalysis(jobId) {
  switchPage('analysis');
  const sel = document.getElementById('analysis-job-select');
  sel.value = jobId;
  loadJobAnalysis();
}

function redrawTimeSeriesPlot() {
  const spec = state.currentSpectrum;
  if (!spec || !spec.time_series) return;
  const series = [
    { values: spec.time_series.raw, color: '#64748b', label: 'Cam A Raw Detrended' },
    { values: spec.time_series.filtered, color: '#38bdf8', label: 'Cam A Bandpass Filtered' },
  ];
  const dSync = spec.dual_camera_sync;
  if (spec.dual_camera && dSync && dSync.enabled && dSync.cam_b_time_series && Array.isArray(dSync.cam_b_time_series.filtered)) {
    series.push({
      values: dSync.cam_b_time_series.filtered,
      color: '#f59e0b',
      label: 'Cam B Bandpass Filtered',
    });
  }
  drawLinePlot('canvas-timeseries', spec.time_series.t_s, series, 'Time (s)');
}

function switchAnalysisSubtab(tabId) {
  document.querySelectorAll('.subtab-btn').forEach((b) => {
    b.classList.toggle('active', b.getAttribute('data-subtab') === tabId);
  });
  document.querySelectorAll('.subtab-panel').forEach((p) => {
    p.classList.toggle('active', p.id === `subtab-panel-${tabId}`);
  });
  if (state.currentSpectrum) {
    if (tabId === 'fft') {
      redrawTimeSeriesPlot();
      redrawFftPlot();
    } else if (tabId === 'spectrogram') {
      drawSpectrogram('canvas-spectrogram', state.currentSpectrum.spectrogram);
    } else if (tabId === 'kymograph') {
      if (state.currentSpectrum.kymograph) {
        drawKymograph('canvas-kymograph', state.currentSpectrum.kymograph);
      } else {
        refreshKymograph();
      }
    } else if (tabId === 'dualroi') {
      if (state.currentSpectrum.dual_roi) {
        renderDualRoiResult(state.currentSpectrum.dual_roi);
      } else {
        runDualRoiComparison();
      }
    } else if (tabId === 'orbit') {
      if (state.currentSpectrum.orbit_2d) {
        drawMotionOrbit('canvas-orbit', state.currentSpectrum.orbit_2d);
      }
    } else if (tabId === 'orbit3d') {
      if (state.currentSpectrum.dual_camera_sync && state.currentSpectrum.dual_camera_sync.orbit_3d) {
        drawMotionOrbit3D('canvas-orbit-3d', state.currentSpectrum.dual_camera_sync.orbit_3d);
      }
    }
  }
}

function onFreqZoomChange(source = 'fft') {
  const fftSel = document.getElementById('fft-zoom-mode');
  const specSel = document.getElementById('spec-zoom-mode');
  const activeVal =
    source === 'spec' && specSel
      ? specSel.value
      : fftSel
      ? fftSel.value
      : 'auto';
  if (fftSel) fftSel.value = activeVal;
  if (specSel) specSel.value = activeVal;
  redrawFftPlot();
  if (state.currentSpectrum && state.currentSpectrum.spectrogram) {
    drawSpectrogram('canvas-spectrogram', state.currentSpectrum.spectrogram);
  }
}

function getActiveFreqMaxHz(spec, nyquistHz) {
  const zoomSel = document.getElementById('fft-zoom-mode') || document.getElementById('spec-zoom-mode');
  const zoomMode = zoomSel ? zoomSel.value : 'auto';
  const jobId = document.getElementById('analysis-job-select') ? document.getElementById('analysis-job-select').value : '';
  const job = state.jobs.find((j) => j.job_id === jobId);
  const jobHighHz =
    (job && job.params && job.params.high_hz) ||
    (spec && spec.suggested_band && spec.suggested_band.high_hz) ||
    Math.max(2.0, ((spec && spec.dominant_hz) || 1.0) * 1.5);

  if (zoomMode === 'auto') {
    return Math.min(nyquistHz, Math.max(3.0, jobHighHz * 2.5));
  }
  if (zoomMode === 'low5') {
    return Math.min(nyquistHz, 5.0);
  }
  return nyquistHz;
}

function getFftZoomedSeries(spec) {
  if (!spec || !Array.isArray(spec.freqs_hz) || spec.freqs_hz.length < 2) {
    return { freqs: [], mags: [] };
  }
  const nyquistHz = spec.freqs_hz[spec.freqs_hz.length - 1] || 15.0;
  const maxHz = getActiveFreqMaxHz(spec, nyquistHz);

  const freqs = [];
  const mags = [];
  for (let i = 0; i < spec.freqs_hz.length; i++) {
    const f = spec.freqs_hz[i];
    if (f <= maxHz || freqs.length < 4) {
      freqs.push(f);
      mags.push(spec.fft_magnitude[i]);
    }
  }
  return { freqs, mags };
}

async function loadJobAnalysis() {
  const jobId = document.getElementById('analysis-job-select').value;
  if (!jobId) return;
  const wasPlayingAudio = Boolean(state.audioSource);
  if (wasPlayingAudio) stopRoiSonification();
  state.selectedFftPeakHz = null;
  const actionWrap = document.getElementById('fft-action-buttons');
  if (actionWrap) actionWrap.style.display = 'none';
  const readout = document.getElementById('fft-hover-readout');
  if (readout) {
    readout.textContent = 'Hover or click any peak on the FFT spectrum to tune magnification cutoffs.';
  }
  document.getElementById('btn-export-csv').href = `/api/jobs/${jobId}/files/signal.csv`;

  const job = state.jobs.find((j) => j.job_id === jobId);
  if (job && job.clip_id) {
    const thumbUrl = `/api/library/${encodeURIComponent(job.clip_id)}/files/thumb.jpg`;
    const kymoImg = document.getElementById('kymo-ref-img');
    const dualImg = document.getElementById('dualroi-ref-img');
    if (kymoImg) kymoImg.src = thumbUrl;
    if (dualImg) dualImg.src = thumbUrl;
  }
  const inpA = document.getElementById('dual-roi-a');
  const inpB = document.getElementById('dual-roi-b');
  if (inpA) inpA.value = '';
  if (inpB) inpB.value = '';

  const spec = await api(`/api/jobs/${jobId}/spectrum`);
  state.currentSpectrum = spec;
  if (wasPlayingAudio) toggleRoiSonification();

  document.getElementById('kpi-dom-hz').textContent = `${spec.dominant_hz} Hz`;
  document.getElementById('kpi-dom-bpm').textContent = `${spec.dominant_bpm} BPM`;
  document.getElementById('kpi-snr').textContent = `${spec.snr_db} dB`;
  if (spec.suggested_band) {
    document.getElementById('kpi-sug-band').textContent =
      `${spec.suggested_band.low_hz}–${spec.suggested_band.high_hz} Hz`;
  }

  const dispEl = document.getElementById('kpi-disp-um');
  const isoEl = document.getElementById('kpi-iso-kinematics');
  const calBadge = document.getElementById('calibration-summary-badge');
  if (spec.calibration) {
    const um = spec.calibration.peak_displacement_um;
    const mm = spec.calibration.peak_displacement_mm;
    if (dispEl) {
      dispEl.textContent = um >= 1000 ? `${mm} mm` : `${um} µm`;
    }
    if (isoEl) {
      const f = spec.dominant_hz || 0;
      const dMm = mm || 0;
      const velRms =
        spec.calibration.velocity_rms_mm_s !== undefined
          ? spec.calibration.velocity_rms_mm_s
          : Number(((2.0 * Math.PI * f * dMm) / Math.SQRT2).toFixed(3));
      const accelG =
        spec.calibration.peak_accel_g !== undefined
          ? spec.calibration.peak_accel_g
          : Number((((2.0 * Math.PI * f) ** 2 * dMm) / 9806.65).toFixed(5));
      isoEl.textContent = `Vel: ${velRms} mm/s RMS | Accel: ${accelG} g`;
    }
    if (calBadge) {
      calBadge.textContent = `Scale: ${spec.calibration.mm_per_pixel} mm/px @ ${spec.calibration.target_distance_cm} cm`;
    }
  }

  const phaseEl = document.getElementById('kpi-phase-deg');
  if (spec.dual_roi && phaseEl) {
    phaseEl.textContent = `${spec.dual_roi.phase_shift_deg}°`;
    phaseEl.title = spec.dual_roi.mode_label || '';
    renderDualRoiResult(spec.dual_roi);
  }

  if (spec.kymograph) {
    const kymoPos = document.getElementById('kymo-pos');
    if (kymoPos && !kymoPos.value) kymoPos.value = spec.kymograph.line_pos;
    drawKymograph('canvas-kymograph', spec.kymograph);
  }

  if (spec.orbit_2d) {
    drawMotionOrbit('canvas-orbit', spec.orbit_2d);
  }

  const dualBanner = document.getElementById('dual-cam-analysis-banner');
  const subtabBtn3d = document.getElementById('subtab-btn-orbit3d');
  const dSync = spec.dual_camera_sync;
  if (spec.dual_camera && dSync && dSync.enabled) {
    if (dualBanner) dualBanner.classList.remove('dual-cam-only');
    if (subtabBtn3d && dSync.phase_coherent !== false) {
      subtabBtn3d.classList.remove('dual-cam-only');
    } else if (subtabBtn3d) {
      subtabBtn3d.classList.add('dual-cam-only');
    }
    const phBadge = document.getElementById('dual-cam-phase-badge');
    const jitBadge = document.getElementById('dual-cam-jitter-badge');
    const bPeakBadge = document.getElementById('dual-cam-b-peak-badge');
    const cohBadge = document.getElementById('dual-cam-coherence-badge');
    if (phBadge) {
      phBadge.textContent = dSync.phase_coherent !== false
        ? `Cross-Cam Phase: ${dSync.phase_shift_deg}° (${dSync.time_lag_ms} ms)`
        : 'Cross-Cam Phase: Independent Streams';
    }
    if (jitBadge) {
      jitBadge.textContent = `Sync Jitter: ±${dSync.sync_dt_mean_ms || 0} ms (${(dSync.sync_mode || 'software').toUpperCase()})`;
    }
    if (bPeakBadge) {
      bPeakBadge.textContent = `Cam B Peak: ${dSync.cam_b_dominant_hz} Hz (SNR ${dSync.cam_b_snr_db} dB)`;
    }
    if (cohBadge) {
      cohBadge.textContent = `Coherence: ${dSync.coherence}`;
    }
    if (dSync.orbit_3d) {
      drawMotionOrbit3D('canvas-orbit-3d', dSync.orbit_3d);
    }
  } else {
    if (dualBanner) dualBanner.classList.add('dual-cam-only');
    if (subtabBtn3d) subtabBtn3d.classList.add('dual-cam-only');
    const panel3d = document.getElementById('subtab-panel-orbit3d');
    if (panel3d && panel3d.classList.contains('active')) {
      switchAnalysisSubtab('fft');
    }
  }

  redrawTimeSeriesPlot();

  await onBaselineJobChange(false);
  updateLightingAliasWarning();
  redrawFftPlot();

  drawSpectrogram('canvas-spectrogram', spec.spectrogram);
}

async function onBaselineJobChange(shouldRedraw = true) {
  const baseSel = document.getElementById('analysis-baseline-select');
  const primarySel = document.getElementById('analysis-job-select');
  const deltaBadge = document.getElementById('baseline-delta-badge');
  const baseJobId = baseSel ? baseSel.value : '';
  const primaryJobId = primarySel ? primarySel.value : '';

  if (!baseJobId || baseJobId === primaryJobId || !state.currentSpectrum) {
    state.baselineSpectrum = null;
    if (deltaBadge) deltaBadge.style.display = 'none';
    if (shouldRedraw) redrawFftPlot();
    return;
  }

  try {
    const baseSpec = await api(`/api/jobs/${encodeURIComponent(baseJobId)}/spectrum`);
    state.baselineSpectrum = baseSpec;
    const curSpec = state.currentSpectrum;
    const curMax = Math.max(1e-9, ...(curSpec.fft_magnitude || [1e-9]));
    const baseMax = Math.max(1e-9, ...(baseSpec.fft_magnitude || [1e-9]));
    const deltaDb = 20.0 * Math.log10(curMax / baseMax);
    const deltaHz = (curSpec.dominant_hz || 0) - (baseSpec.dominant_hz || 0);
    const signDb = deltaDb >= 0 ? '+' : '';
    const signHz = deltaHz >= 0 ? '+' : '';
    if (deltaBadge) {
      deltaBadge.style.display = 'inline-block';
      deltaBadge.textContent = `Δ Peak: ${signDb}${deltaDb.toFixed(1)} dB | Δ Freq: ${signHz}${deltaHz.toFixed(2)} Hz`;
      deltaBadge.className = Math.abs(deltaDb) > 3.0 ? 'badge rolling' : 'badge global';
    }
  } catch (_err) {
    state.baselineSpectrum = null;
    if (deltaBadge) deltaBadge.style.display = 'none';
  }
  if (shouldRedraw) redrawFftPlot();
}

function updateLightingAliasWarning() {
  const warnEl = document.getElementById('fft-alias-warning');
  const spec = state.currentSpectrum;
  if (!warnEl || !spec) return;

  const targetHz = state.selectedFftPeakHz !== null ? state.selectedFftPeakHz : spec.dominant_hz || 0;
  const aliases = spec.lighting_aliases || {};
  const nyq = (spec.freqs_hz && spec.freqs_hz[spec.freqs_hz.length - 1]) || 15.0;
  const fps = aliases.fps || nyq * 2.0;
  const alias100 =
    aliases.alias_50hz_mains_hz !== undefined
      ? aliases.alias_50hz_mains_hz
      : Math.abs(100.0 - Math.round(100.0 / fps) * fps);
  const alias120 =
    aliases.alias_60hz_mains_hz !== undefined
      ? aliases.alias_60hz_mains_hz
      : Math.abs(120.0 - Math.round(120.0 / fps) * fps);

  const tol = 0.35;
  const match50 = targetHz > 0.1 && Math.abs(targetHz - alias100) <= tol;
  const match60 = targetHz > 0.1 && Math.abs(targetHz - alias120) <= tol;
  if (match50 || match60) {
    const mainsLabel =
      match50 && match60
        ? `50 Hz / 60 Hz AC mains flicker (100/120 Hz ripple -> ${alias100.toFixed(2)} / ${alias120.toFixed(2)} Hz alias)`
        : match50
        ? `50 Hz AC mains lighting flicker (100 Hz ripple aliased at ${fps.toFixed(1)} FPS -> ${alias100.toFixed(2)} Hz)`
        : `60 Hz AC mains lighting flicker (120 Hz ripple aliased at ${fps.toFixed(1)} FPS -> ${alias120.toFixed(2)} Hz)`;
    warnEl.style.display = 'block';
    warnEl.innerHTML =
      `<strong>&#9888; AC Lighting Flicker Alias Warning:</strong> Peak at <strong>${targetHz.toFixed(2)} Hz</strong> ` +
      `closely matches ${mainsLabel}. Verify with DC illumination or adjust camera FPS to confirm true mechanical motion.`;
  } else {
    warnEl.style.display = 'none';
  }
}

function redrawFftPlot() {
  const spec = state.currentSpectrum;
  if (!spec || !spec.freqs_hz) return;
  const { freqs, mags } = getFftZoomedSeries(spec);
  if (freqs.length < 2) return;

  const series = [];
  if (state.baselineSpectrum && Array.isArray(state.baselineSpectrum.freqs_hz)) {
    const bFreqs = state.baselineSpectrum.freqs_hz;
    const bMags = state.baselineSpectrum.fft_magnitude || [];
    const interpBase = freqs.map((f) => {
      if (bFreqs.length === 0) return 0;
      if (f <= bFreqs[0]) return bMags[0] || 0;
      if (f >= bFreqs[bFreqs.length - 1]) return bMags[bMags.length - 1] || 0;
      let idx = 0;
      while (idx < bFreqs.length - 2 && bFreqs[idx + 1] < f) idx++;
      const f0 = bFreqs[idx];
      const f1 = bFreqs[idx + 1];
      const u = f1 > f0 ? (f - f0) / (f1 - f0) : 0;
      return (1 - u) * (bMags[idx] || 0) + u * (bMags[idx + 1] || 0);
    });
    series.push({
      values: interpBase,
      color: 'rgba(245, 158, 11, 0.78)',
      dashed: true,
      label: 'Baseline FFT',
    });
  }
  const dSync = spec.dual_camera_sync;
  if (spec.dual_camera && dSync && dSync.enabled && Array.isArray(dSync.cam_b_fft_magnitude)) {
    const camBMags = freqs.map((_f, idx) => dSync.cam_b_fft_magnitude[idx] || 0);
    series.push({
      values: camBMags,
      color: '#f59e0b',
      label: 'Cam B FFT Magnitude',
    });
  }
  series.push({ values: mags, color: '#22c55e', label: 'Cam A FFT Magnitude' });

  drawLinePlot(
    'canvas-fft',
    freqs,
    series,
    'Frequency (Hz)',
    spec.dominant_hz,
    state.selectedFftPeakHz !== null ? state.selectedFftPeakHz : state.hoverFftHz
  );
}

function initInteractiveFftCanvas() {
  const canvas = document.getElementById('canvas-fft');
  if (!canvas) return;
  const pad = { l: 42, r: 16, t: 18, b: 26 };

  const eventToFreqBin = (evt) => {
    const spec = state.currentSpectrum;
    if (!spec || !spec.freqs_hz || spec.freqs_hz.length < 2) return null;
    const { freqs, mags } = getFftZoomedSeries(spec);
    if (freqs.length < 2) return null;
    const rect = canvas.getBoundingClientRect();
    const w = rect.width || 500;
    const pw = Math.max(1, w - pad.l - pad.r);
    const relX = Math.max(0, Math.min(1, (evt.clientX - rect.left - pad.l) / pw));
    const fMin = freqs[0];
    const fMax = freqs[freqs.length - 1] || 1;
    const targetHz = fMin + relX * (fMax - fMin);

    let bestIdx = 0;
    let bestDist = Infinity;
    freqs.forEach((f, idx) => {
      const d = Math.abs(f - targetHz);
      if (d < bestDist) {
        bestDist = d;
        bestIdx = idx;
      }
    });
    return {
      idx: bestIdx,
      hz: freqs[bestIdx],
      mag: mags[bestIdx],
      freqs,
      mags,
    };
  };

  canvas.addEventListener('mousemove', (evt) => {
    const bin = eventToFreqBin(evt);
    if (!bin) return;
    state.hoverFftHz = bin.hz;
    const readout = document.getElementById('fft-hover-readout');
    if (readout && state.selectedFftPeakHz === null) {
      readout.textContent = `Cursor: ${bin.hz.toFixed(2)} Hz (${(bin.hz * 60).toFixed(0)} BPM) — Click peak to tune passband`;
    }
    redrawFftPlot();
  });

  canvas.addEventListener('mouseleave', () => {
    state.hoverFftHz = null;
    const readout = document.getElementById('fft-hover-readout');
    if (readout && state.selectedFftPeakHz === null) {
      readout.textContent = 'Hover or click any peak on the FFT spectrum to tune magnification cutoffs.';
    }
    redrawFftPlot();
  });

  canvas.addEventListener('click', (evt) => {
    const bin = eventToFreqBin(evt);
    const spec = state.currentSpectrum;
    if (!bin || !spec) return;
    // Snap to local maximum within +-2 bins for effortless peak picking
    let peakIdx = bin.idx;
    let peakMag = bin.mags[peakIdx] || 0;
    const lo = Math.max(1, bin.idx - 2);
    const hi = Math.min(bin.freqs.length - 1, bin.idx + 2);
    for (let i = lo; i <= hi; i++) {
      if ((bin.mags[i] || 0) > peakMag) {
        peakMag = bin.mags[i];
        peakIdx = i;
      }
    }
    const fSel = Number(bin.freqs[peakIdx].toFixed(2));
    const bpmSel = Number((fSel * 60).toFixed(1));
    const halfBand = Math.max(0.15, 0.10 * fSel);
    const lowHz = Math.max(0.1, Number((fSel - halfBand).toFixed(2)));
    const highHz = Number((fSel + halfBand).toFixed(2));
    state.selectedFftPeakHz = fSel;

    const readout = document.getElementById('fft-hover-readout');
    if (readout) {
      readout.innerHTML =
        `Selected Peak: <strong style="color: var(--accent);">${fSel.toFixed(2)} Hz (${bpmSel.toFixed(1)} BPM)</strong> ` +
        `&mdash; Suggested Passband: <strong style="color: #22c55e;">[${lowHz.toFixed(2)} &ndash; ${highHz.toFixed(2)} Hz]</strong>`;
    }
    const actionWrap = document.getElementById('fft-action-buttons');
    if (actionWrap) {
      actionWrap.style.display = 'flex';
    }
    updateLightingAliasWarning();
    redrawFftPlot();
  });
}

async function applySelectedFftPeakToProcess(autoRun = false) {
  const fSel = state.selectedFftPeakHz;
  if (!fSel || fSel <= 0) return;
  const jobId = document.getElementById('analysis-job-select').value;
  const job = state.jobs.find((j) => j.job_id === jobId);

  const halfBand = Math.max(0.15, 0.10 * fSel);
  const lowHz = Math.max(0.1, Number((fSel - halfBand).toFixed(2)));
  const highHz = Number((fSel + halfBand).toFixed(2));

  switchPage('process');
  await loadLibrary();
  if (job && job.clip_id) {
    const clipSel = document.getElementById('proc-clip');
    if (clipSel) {
      clipSel.value = job.clip_id;
      onSelectProcessClip();
    }
  }
  const presetSel = document.getElementById('proc-preset');
  if (presetSel) presetSel.value = 'custom';
  const lowEl = document.getElementById('proc-low-hz');
  const highEl = document.getElementById('proc-high-hz');
  if (lowEl) lowEl.value = lowHz;
  if (highEl) highEl.value = highHz;

  const banner = document.getElementById('job-estimate-banner');
  if (banner) {
    banner.className = 'banner success';
    banner.textContent = autoRun
      ? `Auto-Running Magnification Job for FFT Peak ${fSel.toFixed(2)} Hz (${(fSel * 60).toFixed(1)} BPM) with Passband [${lowHz}, ${highHz}] Hz...`
      : `Tuned from FFT Peak ${fSel.toFixed(2)} Hz (${(fSel * 60).toFixed(1)} BPM) -> Target Passband loaded: [${lowHz}, ${highHz}] Hz`;
  }

  const runBtn = document.getElementById('btn-submit-job');
  [lowEl, highEl, runBtn].forEach((el) => {
    if (!el) return;
    el.classList.remove('pulse-highlight');
    void el.offsetWidth;
    el.classList.add('pulse-highlight');
    setTimeout(() => el.classList.remove('pulse-highlight'), 3000);
  });

  if (autoRun) {
    await submitProcessingJob();
  } else if (runBtn) {
    runBtn.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
}

function synthesizeEngineeringSummary(job, spec) {
  const domHz = spec.dominant_hz || 0;
  const domBpm = spec.dominant_bpm || Math.round(domHz * 60);
  const snrDb = spec.snr_db !== undefined ? spec.snr_db : 0;
  const cal = spec.calibration || {};
  const um = cal.peak_displacement_um !== undefined ? cal.peak_displacement_um : 0;
  const mm = cal.peak_displacement_mm !== undefined ? cal.peak_displacement_mm : 0;
  const mmPerPx = cal.mm_per_pixel !== undefined ? cal.mm_per_pixel : 0;
  const epi = (job && job.result && (job.result.epicenter || (job.result.extra_metrics && job.result.extra_metrics.epicenter))) || {};
  const dual = spec.dual_roi || {};
  const phaseDeg = dual.phase_shift_deg !== undefined ? dual.phase_shift_deg : 0;
  const absPhase = Math.abs(phaseDeg);

  let snrQuality = 'a moderate periodic signal above background noise';
  if (snrDb >= 10) {
    snrQuality = 'a high-clarity, strongly coherent periodic oscillation well above the sensor noise floor';
  } else if (snrDb < 3) {
    snrQuality = 'a low-SNR or broadband vibration near the camera noise floor (consider brighter illumination or tighter ROI)';
  }

  let epiLocation = 'within the analyzed frame';
  if (epi.x !== undefined && epi.y !== undefined) {
    const rx = epi.rel_x !== undefined ? epi.rel_x : 0.5;
    const ry = epi.rel_y !== undefined ? epi.rel_y : 0.5;
    const horiz = rx < 0.35 ? 'left' : rx > 0.65 ? 'right' : 'center';
    const vert = ry < 0.35 ? 'upper' : ry > 0.65 ? 'lower' : 'middle';
    epiLocation = `at pixel coordinates (${epi.x}, ${epi.y}) in the ${vert}-${horiz} region of the frame`;
  }

  const dispStr = um >= 1000 ? `${mm} mm (${um} µm)` : `${um} µm (${mm} mm)`;
  const velRms =
    cal.velocity_rms_mm_s !== undefined
      ? cal.velocity_rms_mm_s
      : Number(((2.0 * Math.PI * domHz * mm) / Math.SQRT2).toFixed(3));
  const accelG =
    cal.peak_accel_g !== undefined
      ? cal.peak_accel_g
      : Number((((2.0 * Math.PI * domHz) ** 2 * mm) / 9806.65).toFixed(5));
  const isoStr = `ISO kinematic severity at ${domHz} Hz corresponds to a velocity of ${velRms} mm/s RMS and a peak acceleration of ${accelG} g.`;
  let phaseAssessment = '';
  if (absPhase < 30.0) {
    phaseAssessment = `Dual-Probe cross-spectral analysis between Probe A and Probe B shows a phase shift of Δφ = ${phaseDeg}° (|Δφ| < 30°), indicating in-phase global deflection or translational heave where both regions move together synchronously.`;
  } else if (absPhase > 135.0) {
    phaseAssessment = `Dual-Probe cross-spectral analysis between Probe A and Probe B reveals a strong out-of-phase shift of Δφ = ${phaseDeg}° (|Δφ| > 135°), indicating an anti-phase mechanical pivot, seesaw rocking mode, or structural node between the two probes.`;
  } else {
    phaseAssessment = `Dual-Probe cross-spectral analysis between Probe A and Probe B measures an intermediate phase lag of Δφ = ${phaseDeg}°, characteristic of a traveling flexural wave or coupled multi-axis vibration propagating across the structure.`;
  }

  return (
    `Automated Diagnostic Assessment: The primary vibration epicenter is pinpointed ${epiLocation}, ` +
    `oscillating at a dominant fundamental frequency of ${domHz} Hz (${domBpm} BPM / CPM) with an in-band SNR of ${snrDb} dB, ` +
    `representing ${snrQuality}. Based on the active spatial scale calibration (${mmPerPx} mm/px), ` +
    `the estimated physical peak-to-peak displacement amplitude at the ROI is ${dispStr}. ${isoStr} ${phaseAssessment}`
  );
}

async function buildAnnotatedReportEvidenceDataUrl(jobId, dual) {
  const loadImg = (url) =>
    new Promise((resolve) => {
      const img = new Image();
      img.crossOrigin = 'anonymous';
      img.onload = () => resolve(img);
      img.onerror = () => resolve(null);
      img.src = url;
    });

  let img = await loadImg(`/api/jobs/${encodeURIComponent(jobId)}/files/report_base.jpg?t=${Date.now()}`);
  const usedBase = Boolean(img);
  if (!img) {
    img = await loadImg(`/api/jobs/${encodeURIComponent(jobId)}/files/report_frame.jpg?t=${Date.now()}`);
  }
  if (!img) {
    return `/api/jobs/${encodeURIComponent(jobId)}/files/report_frame.jpg?t=${Date.now()}`;
  }

  const canvas = document.createElement('canvas');
  const w = (canvas.width = img.naturalWidth || img.width || 640);
  const h = (canvas.height = img.naturalHeight || img.height || 480);
  const ctx = canvas.getContext('2d');
  ctx.drawImage(img, 0, 0, w, h);

  if (usedBase) {
    const { w: srcW, h: srcH } = getAnalysisFrameDims();
    const sx = w / Math.max(1, srcW);
    const sy = h / Math.max(1, srcH);
    const boxA = parseBoxCsv(document.getElementById('dual-roi-a')?.value) || (dual && dual.roi_a);
    const boxB = parseBoxCsv(document.getElementById('dual-roi-b')?.value) || (dual && dual.roi_b);

    const drawBox = (box, strokeColor, fillRgba, label) => {
      if (!Array.isArray(box) || box.length !== 4) return;
      const bx = Math.round(box[0] * sx);
      const by = Math.round(box[1] * sy);
      const bw = Math.max(6, Math.round(box[2] * sx));
      const bh = Math.max(6, Math.round(box[3] * sy));
      ctx.fillStyle = fillRgba;
      ctx.fillRect(bx, by, bw, bh);
      ctx.strokeStyle = strokeColor;
      ctx.lineWidth = Math.max(2, Math.round(w / 220));
      ctx.strokeRect(bx, by, bw, bh);

      const fontPx = Math.max(11, Math.round(w / 42));
      ctx.font = `bold ${fontPx}px sans-serif`;
      const textW = ctx.measureText(label).width;
      const badgeW = Math.min(w - bx, Math.ceil(textW + 10));
      const badgeH = fontPx + 6;
      ctx.fillStyle = strokeColor;
      ctx.fillRect(bx, by, badgeW, badgeH);
      ctx.fillStyle = '#0f172a';
      ctx.fillText(label, bx + 5, by + fontPx + 1);
    };

    drawBox(boxA, '#38bdf8', 'rgba(56, 189, 248, 0.16)', 'PROBE A');
    drawBox(boxB, '#f59e0b', 'rgba(245, 158, 11, 0.16)', 'PROBE B');
  }

  return canvas.toDataURL('image/jpeg', 0.92);
}

async function exportDiagnosticReport() {
  const jobId = document.getElementById('analysis-job-select').value;
  if (!jobId) {
    alert('Please select a completed job on Page 4 first.');
    return;
  }
  if (!state.currentSpectrum) {
    await loadJobAnalysis();
  }
  const spec = state.currentSpectrum;
  const job = state.jobs.find((j) => j.job_id === jobId) || state.activeResultJob || { job_id: jobId, method: 'eulerian', params: {} };
  const clip = state.clips.find((c) => c.clip_id === job.clip_id);

  // Ensure all canvases are freshly drawn so their snapshots are populated
  drawLinePlot(
    'canvas-timeseries',
    spec.time_series.t_s,
    [
      { values: spec.time_series.raw, color: '#64748b', label: 'Raw Detrended' },
      { values: spec.time_series.filtered, color: '#38bdf8', label: 'Bandpass Filtered' },
    ],
    'Time (s)'
  );
  redrawFftPlot();
  drawSpectrogram('canvas-spectrogram', spec.spectrogram);
  if (spec.kymograph) drawKymograph('canvas-kymograph', spec.kymograph);
  if (spec.dual_roi) renderDualRoiResult(spec.dual_roi);

  const tsDataUrl = document.getElementById('canvas-timeseries').toDataURL('image/png');
  const fftDataUrl = document.getElementById('canvas-fft').toDataURL('image/png');
  const specDataUrl = document.getElementById('canvas-spectrogram').toDataURL('image/png');
  const kymoDataUrl = document.getElementById('canvas-kymograph').toDataURL('image/png');

  const summaryText = synthesizeEngineeringSummary(job, spec);
  const epi = (job.result && (job.result.epicenter || (job.result.extra_metrics && job.result.extra_metrics.epicenter))) || {};
  const cal = spec.calibration || {};
  const dual = spec.dual_roi || {};
  const reportImgUrl = await buildAnnotatedReportEvidenceDataUrl(jobId, dual);

  const win = window.open('', '_blank');
  if (!win) {
    alert('Popup blocked by browser. Please allow popups for BenchVision to view/print the diagnostic report.');
    return;
  }

  const html = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>BenchVision Diagnostic Report — ${jobId}</title>
  <style>
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      color: #0f172a;
      background: #ffffff;
      margin: 0;
      padding: 24px 32px;
      line-height: 1.45;
    }
    .header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      border-bottom: 2px solid #0f172a;
      padding-bottom: 12px;
      margin-bottom: 18px;
    }
    h1 { font-size: 20px; margin: 0 0 4px 0; }
    h2 { font-size: 14px; text-transform: uppercase; letter-spacing: 0.04em; color: #334155; margin: 18px 0 8px 0; border-bottom: 1px solid #cbd5e1; padding-bottom: 4px; }
    .meta { font-size: 12px; color: #475569; }
    .summary-box {
      background: #f8fafc;
      border-left: 4px solid #0284c7;
      padding: 12px 16px;
      font-size: 13.5px;
      margin-bottom: 18px;
      border-radius: 4px;
    }
    .grid-top {
      display: grid;
      grid-template-columns: 1.15fr 1fr;
      gap: 20px;
      align-items: start;
    }
    .evidence-img {
      width: 100%;
      border: 1px solid #94a3b8;
      border-radius: 6px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    th, td {
      border: 1px solid #cbd5e1;
      padding: 7px 10px;
      text-align: left;
    }
    th { background: #f1f5f9; width: 46%; color: #334155; }
    .plots-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
      margin-top: 10px;
    }
    .plot-card {
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      padding: 8px;
      background: #050811;
      color: #e2e8f0;
    }
    .plot-card div {
      font-size: 11px;
      margin-bottom: 4px;
      color: #94a3b8;
    }
    .plot-card img {
      width: 100%;
      height: 150px;
      object-fit: fill;
      display: block;
    }
    .no-print {
      margin-bottom: 16px;
      display: flex;
      gap: 10px;
    }
    .btn-print {
      background: #0284c7;
      color: #fff;
      border: none;
      padding: 8px 16px;
      border-radius: 5px;
      font-weight: 600;
      cursor: pointer;
    }
    @media print {
      .no-print { display: none !important; }
      body { padding: 0; }
    }
  </style>
</head>
<body>
  <div class="no-print">
    <button class="btn-print" onclick="window.print()">Print / Save as PDF</button>
    <button onclick="window.close()" style="padding: 8px 14px; cursor: pointer;">Close Window</button>
  </div>

  <div class="header">
    <div>
      <h1>BenchVision — Vibration &amp; Motion Magnification Diagnostic Report</h1>
      <div class="meta">
        Clip: <strong>${clip ? clip.title : job.clip_id}</strong> (${job.clip_id}) &bull;
        Method: <strong>${(job.method || '').toUpperCase()}</strong> &bull;
        Passband: <strong>${job.params ? job.params.low_hz : '—'}–${job.params ? job.params.high_hz : '—'} Hz (&alpha;=${job.params ? job.params.alpha : '—'})</strong>
      </div>
    </div>
    <div class="meta" style="text-align: right;">
      Job ID: <strong>${jobId}</strong><br/>
      Generated: ${new Date().toLocaleString()}
    </div>
  </div>

  <h2>1. Automated Plain-English Engineering Summary</h2>
  <div class="summary-box">${summaryText}</div>

  <div class="grid-top">
    <div>
      <h2>2. Visual Evidence (Heatmap, Epicenter &amp; Probes)</h2>
      <img class="evidence-img" src="${reportImgUrl}" alt="Diagnostic Heatmap &amp; Epicenter Snapshot" />
    </div>
    <div>
      <h2>3. Key Diagnostic Metrics</h2>
      <table>
        <tbody>
          <tr><th>Dominant Frequency</th><td><strong>${spec.dominant_hz} Hz</strong> (${spec.dominant_bpm} BPM / CPM)</td></tr>
          <tr><th>In-Band SNR</th><td><strong>${spec.snr_db} dB</strong></td></tr>
          <tr><th>Calibrated Peak Displacement</th><td><strong>${cal.peak_displacement_um ?? '—'} &mu;m</strong> (${cal.peak_displacement_mm ?? '—'} mm)</td></tr>
          <tr><th>ISO Velocity (RMS) &amp; Peak Accel</th><td><strong>${cal.velocity_rms_mm_s ?? '—'} mm/s RMS</strong> &bull; <strong>${cal.peak_accel_g ?? '—'} g</strong></td></tr>
          <tr><th>Spatial Scale Factor</th><td>${cal.mm_per_pixel ?? '—'} mm/px (Target dist: ${cal.target_distance_cm ?? '—'} cm)</td></tr>
          <tr><th>Vibration Epicenter (X, Y)</th><td><strong>(${epi.x ?? '—'}, ${epi.y ?? '—'}) px</strong></td></tr>
          <tr><th>Dual-Probe Phase Shift (&Delta;&phi;)</th><td><strong>${dual.phase_shift_deg ?? '—'}&deg;</strong> (${dual.mode_label ?? '—'})</td></tr>
          <tr><th>Sample Rate / Frames</th><td>${clip ? clip.achieved_fps : '—'} FPS (${clip ? clip.frame_count : '—'} frames)</td></tr>
          <tr><th>Suggested Passband</th><td>${spec.suggested_band ? `${spec.suggested_band.low_hz}–${spec.suggested_band.high_hz} Hz` : '—'}</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <h2>4. Spectral &amp; Spacetime Plots</h2>
  <div class="plots-grid">
    <div class="plot-card">
      <div>ROI Temporal Waveform (Raw Detrended vs. Bandpass Filtered)</div>
      <img src="${tsDataUrl}" alt="Time Series Plot" />
    </div>
    <div class="plot-card">
      <div>FFT Amplitude &amp; Welch PSD Spectrum (Dominant Peak: ${spec.dominant_hz} Hz)</div>
      <img src="${fftDataUrl}" alt="FFT Plot" />
    </div>
    <div class="plot-card">
      <div>2D STFT Time-Frequency Spectrogram (dB)</div>
      <img src="${specDataUrl}" alt="Spectrogram Plot" />
    </div>
    <div class="plot-card">
      <div>1D Slit-Scan Spacetime Kymograph</div>
      <img src="${kymoDataUrl}" alt="Kymograph Plot" />
    </div>
  </div>
</body>
</html>`;

  win.document.open();
  win.document.write(html);
  win.document.close();
}

function drawKymograph(canvasId, kymo) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || !kymo || !kymo.matrix || kymo.matrix.length === 0) return;
  const ctx = canvas.getContext('2d');
  const w = (canvas.width = canvas.clientWidth || 800);
  const h = (canvas.height = canvas.clientHeight || 240);
  ctx.clearRect(0, 0, w, h);

  const nTimes = kymo.matrix.length;
  const nPts = kymo.matrix[0].length;
  const cellW = w / Math.max(1, nTimes);
  const cellH = h / Math.max(1, nPts);

  for (let t = 0; t < nTimes; t++) {
    const col = kymo.matrix[t];
    for (let s = 0; s < nPts; s++) {
      const v = col[s];
      ctx.fillStyle = `rgb(${Math.round(v * 0.7)},${v},${Math.min(255, Math.round(v * 1.15))})`;
      ctx.fillRect(t * cellW, s * cellH, Math.ceil(cellW), Math.ceil(cellH));
    }
  }
  renderKymographRefOverlay(kymo);
}

function getAnalysisFrameDims() {
  const jobId = document.getElementById('analysis-job-select') ? document.getElementById('analysis-job-select').value : '';
  const job = state.jobs.find((j) => j.job_id === jobId);
  const clip = job ? state.clips.find((c) => c.clip_id === job.clip_id) : null;
  const w = (clip && clip.width) || 640;
  const h = (clip && clip.height) || 480;
  return { w, h };
}

function renderKymographRefOverlay(kymo) {
  const svg = document.getElementById('kymo-ref-svg');
  if (!svg || !kymo) return;
  const { w, h } = getAnalysisFrameDims();
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  svg.innerHTML = '';

  const axis = kymo.axis || document.getElementById('kymo-axis').value || 'horizontal';
  const pos = kymo.line_pos !== undefined ? Number(kymo.line_pos) : Math.round((axis === 'horizontal' ? h : w) / 2);

  const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
  if (axis === 'horizontal') {
    line.setAttribute('x1', '0');
    line.setAttribute('y1', String(pos));
    line.setAttribute('x2', String(w));
    line.setAttribute('y2', String(pos));
  } else {
    line.setAttribute('x1', String(pos));
    line.setAttribute('y1', '0');
    line.setAttribute('x2', String(pos));
    line.setAttribute('y2', String(h));
  }
  line.setAttribute('stroke', '#38bdf8');
  line.setAttribute('stroke-width', String(Math.max(2, Math.round(w / 180))));
  line.setAttribute('stroke-dasharray', '6,4');
  svg.appendChild(line);
}

function initKymographRefPicker() {
  const box = document.getElementById('kymo-ref-box');
  if (!box) return;
  const img = document.getElementById('kymo-ref-img');
  if (img) {
    img.addEventListener('load', () => {
      if (state.currentSpectrum && state.currentSpectrum.kymograph) {
        renderKymographRefOverlay(state.currentSpectrum.kymograph);
      }
    });
  }
  let dragging = false;

  const applyClick = (evt, commit = false) => {
    const rect = box.getBoundingClientRect();
    const { w, h } = getAnalysisFrameDims();
    const relX = Math.max(0, Math.min(1, (evt.clientX - rect.left) / Math.max(1, rect.width)));
    const relY = Math.max(0, Math.min(1, (evt.clientY - rect.top) / Math.max(1, rect.height)));
    const axis = document.getElementById('kymo-axis').value || 'horizontal';
    const pos = axis === 'horizontal' ? Math.round(relY * h) : Math.round(relX * w);
    const posEl = document.getElementById('kymo-pos');
    if (posEl) posEl.value = pos;
    renderKymographRefOverlay({ axis, line_pos: pos });
    if (commit) {
      refreshKymograph();
    }
  };

  box.addEventListener('pointerdown', (evt) => {
    if (evt.button !== 0) return;
    evt.preventDefault();
    dragging = true;
    box.setPointerCapture(evt.pointerId);
    applyClick(evt, false);
  });
  box.addEventListener('pointermove', (evt) => {
    if (!dragging) return;
    applyClick(evt, false);
  });
  box.addEventListener('pointerup', (evt) => {
    if (!dragging) return;
    dragging = false;
    applyClick(evt, true);
  });
}

async function refreshKymograph() {
  const jobId = document.getElementById('analysis-job-select').value;
  if (!jobId) return;
  const axis = document.getElementById('kymo-axis').value || 'horizontal';
  const posVal = document.getElementById('kymo-pos').value.trim();
  const posParam = posVal !== '' ? `&pos=${encodeURIComponent(posVal)}` : '';
  const kymo = await api(`/api/jobs/${jobId}/kymograph?axis=${axis}${posParam}`);
  if (state.currentSpectrum) state.currentSpectrum.kymograph = kymo;
  drawKymograph('canvas-kymograph', kymo);
}

function parseBoxCsv(raw) {
  if (!raw || !raw.trim()) return null;
  const nums = raw.split(',').map((s) => parseInt(s.trim(), 10));
  if (nums.length === 4 && nums.every((n) => !Number.isNaN(n))) return nums;
  return null;
}

function setActiveDualProbe(probe) {
  state.activeDualProbe = probe === 'B' ? 'B' : 'A';
  const btnA = document.getElementById('btn-draw-probe-a');
  const btnB = document.getElementById('btn-draw-probe-b');
  if (btnA) {
    btnA.className = state.activeDualProbe === 'A' ? 'primary' : '';
    btnA.style.borderColor = state.activeDualProbe === 'A' ? '#38bdf8' : '';
  }
  if (btnB) {
    btnB.className = state.activeDualProbe === 'B' ? 'primary' : '';
    btnB.style.borderColor = state.activeDualProbe === 'B' ? '#f59e0b' : '';
    btnB.style.background = state.activeDualProbe === 'B' ? '#f59e0b' : '';
    btnB.style.color = state.activeDualProbe === 'B' ? '#0f172a' : '';
  }
}

function toggleActiveDualProbe() {
  setActiveDualProbe(state.activeDualProbe === 'A' ? 'B' : 'A');
}

async function resetDualRoiAuto() {
  const inpA = document.getElementById('dual-roi-a');
  const inpB = document.getElementById('dual-roi-b');
  if (inpA) inpA.value = '';
  if (inpB) inpB.value = '';
  await runDualRoiComparison();
}

function renderDualRoiRefOverlay(dual) {
  const svg = document.getElementById('dualroi-ref-svg');
  if (!svg) return;
  const { w, h } = getAnalysisFrameDims();
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  svg.innerHTML = '';

  const inpA = document.getElementById('dual-roi-a');
  const inpB = document.getElementById('dual-roi-b');
  const boxA = parseBoxCsv(inpA ? inpA.value : '') || (dual && dual.roi_a);
  const boxB = parseBoxCsv(inpB ? inpB.value : '') || (dual && dual.roi_b);

  const drawProbeRect = (box, color, label) => {
    if (!Array.isArray(box) || box.length !== 4) return;
    const [rx, ry, rw, rh] = box;
    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rect.setAttribute('x', String(rx));
    rect.setAttribute('y', String(ry));
    rect.setAttribute('width', String(Math.max(4, rw)));
    rect.setAttribute('height', String(Math.max(4, rh)));
    rect.setAttribute('fill', color === '#38bdf8' ? 'rgba(56, 189, 248, 0.18)' : 'rgba(245, 158, 11, 0.18)');
    rect.setAttribute('stroke', color);
    rect.setAttribute('stroke-width', String(Math.max(2, Math.round(w / 200))));
    svg.appendChild(rect);

    const fontPx = Math.max(11, Math.round(w / 42));
    const badgeW = Math.min(Math.max(4, w - rx), Math.round(fontPx * 5.2));
    const badgeH = Math.round(fontPx * 1.45);
    const badge = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    badge.setAttribute('x', String(rx));
    badge.setAttribute('y', String(ry));
    badge.setAttribute('width', String(badgeW));
    badge.setAttribute('height', String(badgeH));
    badge.setAttribute('fill', color);
    badge.setAttribute('rx', '2');
    svg.appendChild(badge);

    const txt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    txt.setAttribute('x', String(rx + 5));
    txt.setAttribute('y', String(ry + Math.round(fontPx * 1.08)));
    txt.setAttribute('fill', '#0f172a');
    txt.setAttribute('font-size', String(fontPx));
    txt.setAttribute('font-weight', '700');
    txt.textContent = label;
    svg.appendChild(txt);
  };

  drawProbeRect(boxA, '#38bdf8', 'PROBE A');
  drawProbeRect(boxB, '#f59e0b', 'PROBE B');
}

function initDualRoiRefPicker() {
  const box = document.getElementById('dualroi-ref-box');
  if (!box) return;
  const img = document.getElementById('dualroi-ref-img');
  if (img) {
    img.addEventListener('load', () => {
      renderDualRoiRefOverlay(state.currentSpectrum ? state.currentSpectrum.dual_roi : null);
    });
  }
  let startPt = null;

  const toCoord = (evt) => {
    const rect = box.getBoundingClientRect();
    const { w, h } = getAnalysisFrameDims();
    const relX = Math.max(0, Math.min(1, (evt.clientX - rect.left) / Math.max(1, rect.width)));
    const relY = Math.max(0, Math.min(1, (evt.clientY - rect.top) / Math.max(1, rect.height)));
    return { x: Math.round(relX * w), y: Math.round(relY * h), w, h };
  };

  box.addEventListener('pointerdown', (evt) => {
    if (evt.button !== 0) return;
    evt.preventDefault();
    startPt = toCoord(evt);
    box.setPointerCapture(evt.pointerId);
  });

  box.addEventListener('pointermove', (evt) => {
    if (!startPt) return;
    const cur = toCoord(evt);
    const rx = Math.min(startPt.x, cur.x);
    const ry = Math.min(startPt.y, cur.y);
    const rw = Math.max(12, Math.abs(cur.x - startPt.x));
    const rh = Math.max(12, Math.abs(cur.y - startPt.y));
    const inputId = state.activeDualProbe === 'A' ? 'dual-roi-a' : 'dual-roi-b';
    const inp = document.getElementById(inputId);
    if (inp) inp.value = `${rx},${ry},${rw},${rh}`;
    renderDualRoiRefOverlay(null);
  });

  box.addEventListener('pointerup', (evt) => {
    if (!startPt) return;
    const cur = toCoord(evt);
    let rx = Math.min(startPt.x, cur.x);
    let ry = Math.min(startPt.y, cur.y);
    let rw = Math.abs(cur.x - startPt.x);
    let rh = Math.abs(cur.y - startPt.y);
    if (rw < 12 || rh < 12) {
      rw = Math.max(24, Math.round(cur.w * 0.16));
      rh = Math.max(24, Math.round(cur.h * 0.16));
      rx = Math.max(0, Math.min(cur.w - rw, cur.x - Math.round(rw / 2)));
      ry = Math.max(0, Math.min(cur.h - rh, cur.y - Math.round(rh / 2)));
    }
    startPt = null;
    const inputId = state.activeDualProbe === 'A' ? 'dual-roi-a' : 'dual-roi-b';
    const inp = document.getElementById(inputId);
    if (inp) inp.value = `${rx},${ry},${rw},${rh}`;
    renderDualRoiRefOverlay(null);
    runDualRoiComparison();
  });
}

function renderDualRoiResult(dual) {
  if (!dual) return;
  const sumBadge = document.getElementById('dual-roi-summary');
  if (sumBadge) {
    sumBadge.textContent = `${dual.mode_label} (Δφ = ${dual.phase_shift_deg}°, r = ${dual.correlation})`;
  }
  const phaseEl = document.getElementById('kpi-phase-deg');
  if (phaseEl) phaseEl.textContent = `${dual.phase_shift_deg}°`;
  const inpA = document.getElementById('dual-roi-a');
  const inpB = document.getElementById('dual-roi-b');
  if (inpA && !inpA.value && Array.isArray(dual.roi_a)) inpA.value = dual.roi_a.join(',');
  if (inpB && !inpB.value && Array.isArray(dual.roi_b)) inpB.value = dual.roi_b.join(',');
  renderDualRoiRefOverlay(dual);
  drawLinePlot(
    'canvas-dual-roi',
    dual.t_s,
    [
      { values: dual.signal_a, color: '#38bdf8', label: 'Probe A' },
      { values: dual.signal_b, color: '#f59e0b', label: 'Probe B' },
    ],
    'Time (s)'
  );
}

async function runDualRoiComparison() {
  const jobId = document.getElementById('analysis-job-select').value;
  if (!jobId) return;
  const roiA = parseBoxCsv(document.getElementById('dual-roi-a').value);
  const roiB = parseBoxCsv(document.getElementById('dual-roi-b').value);
  const dual = await api(`/api/jobs/${jobId}/dual-roi`, {
    method: 'POST',
    body: JSON.stringify({ roi_a: roiA, roi_b: roiB }),
  });
  if (state.currentSpectrum) state.currentSpectrum.dual_roi = dual;
  renderDualRoiResult(dual);
}

function sanitizeSignalForPlotAndAudio(rawArr) {
  if (!Array.isArray(rawArr) || rawArr.length === 0) return { values: [], p95: 1.0, skip: 0 };
  const arr = rawArr.map((v) => Number(v) || 0);
  if (arr.length <= 4) {
    return { values: arr, p95: Math.max(1e-6, ...arr.map((v) => Math.abs(v))), skip: 0 };
  }
  const skip = Math.min(arr.length - 1, Math.max(4, Math.ceil(arr.length * 0.05)));
  for (let i = 0; i < Math.min(3, skip); i++) {
    arr[i] = arr[skip];
  }
  const tailAbs = arr
    .slice(skip)
    .map((v) => Math.abs(v))
    .sort((a, b) => a - b);
  const p95Idx = Math.min(tailAbs.length - 1, Math.floor(tailAbs.length * 0.95));
  const p95 = Math.max(1e-6, tailAbs[p95Idx] || 1e-6);
  for (let i = 0; i < skip; i++) {
    arr[i] = Math.max(-p95, Math.min(p95, arr[i]));
  }
  return { values: arr, p95, skip };
}

function stopRoiSonification() {
  const btn = document.getElementById('btn-sonify');
  if (state.audioSource) {
    try {
      state.audioSource.stop();
      state.audioSource.disconnect();
    } catch (_e) {
      // ignore
    }
    state.audioSource = null;
  }
  if (btn) {
    btn.textContent = '▶ Listen to ROI Signal';
    btn.className = '';
  }
}

function onSonifyModeChange() {
  if (state.audioSource) {
    stopRoiSonification();
    toggleRoiSonification();
  }
}

function buildSonificationPcmFloat32(spec, sampleRate, modeVal) {
  const rawFiltered =
    (spec && spec.time_series && spec.time_series.filtered) ||
    (spec && spec.filtered_signal) ||
    [];
  if (!spec || rawFiltered.length < 8) {
    return null;
  }

  const tArr = (spec.time_series && spec.time_series.t_s) || [];
  const durationS = Math.max(1.5, (tArr[tArr.length - 1] - tArr[0]) || 4.0);

  // Pre-processing: sanitize first 4 frames (or 5%) and normalize by 95th percentile amplitude to [-1, +1]
  const { values: cleanSig, p95, skip } = sanitizeSignalForPlotAndAudio(rawFiltered);
  const normSig = cleanSig.map((v, idx) => {
    const clamped = Math.max(-1.0, Math.min(1.0, v / p95));
    // Smoothly ramp in the first 4 frames from 0 so t=0.0s never produces a startup blip
    if (idx < Math.min(4, skip)) {
      return clamped * (idx / Math.max(1, Math.min(4, skip)));
    }
    return clamped;
  });

  const sampleNormAt = (frac01) => {
    const pos = Math.max(0, Math.min(1, frac01)) * (normSig.length - 1);
    const idx0 = Math.floor(pos);
    const idx1 = Math.min(normSig.length - 1, idx0 + 1);
    const frac = pos - idx0;
    return (1 - frac) * normSig[idx0] + frac * normSig[idx1];
  };

  let bufDurationS = durationS;
  if (modeVal === '10') {
    bufDurationS = Math.max(0.75, durationS / 4.0);
  } else if (modeVal === '50') {
    bufDurationS = Math.max(1.5, Math.min(4.0, durationS));
  }

  const totalSamples = Math.max(256, Math.floor(bufDurationS * sampleRate));
  const channel = new Float32Array(totalSamples);
  const fadeSamples = Math.min(Math.floor(sampleRate * 0.03), Math.floor(totalSamples * 0.1));
  const domHz = spec.dominant_hz || 1.0;

  let phase = 0;
  for (let i = 0; i < totalSamples; i++) {
    let env = 1.0;
    if (i < fadeSamples) env = i / Math.max(1, fadeSamples);
    else if (i > totalSamples - fadeSamples) env = (totalSamples - i) / Math.max(1, fadeSamples);

    if (modeVal === '1') {
      const normVal = sampleNormAt(i / totalSamples);
      const u = Math.max(0.0, Math.min(1.0, 0.5 * (normVal + 1.0)));
      const instFreq = 140.0 + 180.0 * u;
      const gain = 0.08 + 0.77 * Math.pow(u, 1.15);
      phase += (2 * Math.PI * instFreq) / sampleRate;
      const tri = (2.0 / Math.PI) * Math.asin(Math.sin(phase));
      channel[i] = (0.65 * tri + 0.35 * Math.sin(phase)) * gain * env * 0.7;
    } else if (modeVal === '10') {
      const normVal = sampleNormAt(i / totalSamples);
      const u = Math.max(0.0, Math.min(1.0, 0.5 * (normVal + 1.0)));
      const instFreq = 200.0 + 320.0 * u;
      const gain = 0.05 + 0.85 * Math.pow(u, 1.5);
      phase += (2 * Math.PI * instFreq) / sampleRate;
      const wave = 0.65 * Math.sin(phase) + 0.35 * Math.sin(2.0 * phase);
      channel[i] = wave * gain * env * 0.65;
    } else {
      const tSec = i / sampleRate;
      const cycles50x = (tSec * 50.0) / durationS;
      const wrappedFrac = cycles50x - Math.floor(cycles50x);
      const seamEnv = Math.sin(Math.PI * wrappedFrac) ** 0.15;
      const normVal = sampleNormAt(wrappedFrac) * seamEnv;
      const shiftedHz = domHz * 50.0;
      if (shiftedHz < 75.0) {
        const subCarrier =
          0.7 * Math.sin(2 * Math.PI * 110.0 * tSec) +
          0.3 * Math.sin(2 * Math.PI * 220.0 * tSec);
        channel[i] = (0.25 + 0.75 * normVal) * subCarrier * env * 0.65;
      } else {
        const subBody = 0.2 * Math.sin(2 * Math.PI * 110.0 * tSec) * normVal;
        channel[i] = (0.8 * normVal + subBody) * env * 0.75;
      }
    }
  }
  return channel;
}

function toggleRoiSonification() {
  const btn = document.getElementById('btn-sonify');
  if (state.audioSource) {
    stopRoiSonification();
    return;
  }

  const spec = state.currentSpectrum;
  if (!spec) return;

  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtx) return;
  if (!state.audioCtx) state.audioCtx = new AudioCtx();
  if (state.audioCtx.state === 'suspended') {
    state.audioCtx.resume().catch(() => {});
  }

  const modeVal = String(document.getElementById('sonify-pitch').value || '1');
  const sampleRate = state.audioCtx.sampleRate;
  const pcm = buildSonificationPcmFloat32(spec, sampleRate, modeVal);
  if (!pcm) return;

  const buf = state.audioCtx.createBuffer(1, pcm.length, sampleRate);
  buf.getChannelData(0).set(pcm);

  const src = state.audioCtx.createBufferSource();
  src.buffer = buf;
  src.loop = true;
  src.connect(state.audioCtx.destination);
  src.start();
  state.audioSource = src;
  if (btn) {
    btn.textContent = '⏹ Stop Audio';
    btn.className = 'primary';
  }
}

function downloadSonifiedWav() {
  const spec = state.currentSpectrum;
  if (!spec) {
    alert('Please load a completed job on Page 4 first.');
    return;
  }
  const modeVal = String(document.getElementById('sonify-pitch').value || '1');
  const sampleRate = 44100;
  const pcm = buildSonificationPcmFloat32(spec, sampleRate, modeVal);
  if (!pcm || pcm.length === 0) return;

  const numSamples = pcm.length;
  const dataBytes = numSamples * 2;
  const buffer = new ArrayBuffer(44 + dataBytes);
  const view = new DataView(buffer);

  const writeStr = (offset, str) => {
    for (let i = 0; i < str.length; i++) {
      view.setUint8(offset + i, str.charCodeAt(i));
    }
  };

  writeStr(0, 'RIFF');
  view.setUint32(4, 36 + dataBytes, true);
  writeStr(8, 'WAVE');
  writeStr(12, 'fmt ');
  view.setUint32(16, 16, true); // PCM chunk size
  view.setUint16(20, 1, true);  // PCM format = 1
  view.setUint16(22, 1, true);  // Mono = 1 channel
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // ByteRate
  view.setUint16(32, 2, true);  // BlockAlign
  view.setUint16(34, 16, true); // BitsPerSample = 16
  writeStr(36, 'data');
  view.setUint32(40, dataBytes, true);

  let offset = 44;
  for (let i = 0; i < numSamples; i++) {
    const s = Math.max(-1.0, Math.min(1.0, pcm[i]));
    view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    offset += 2;
  }

  const jobId = document.getElementById('analysis-job-select')?.value || 'roi';
  const blob = new Blob([buffer], { type: 'audio/wav' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${jobId}_sonified_${modeVal}x.wav`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

function drawLinePlot(canvasId, xVals, seriesList, xLabel, markerX = null, secondaryMarkerX = null) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const w = (canvas.width = canvas.clientWidth || 500);
  const h = (canvas.height = canvas.clientHeight || 200);
  ctx.clearRect(0, 0, w, h);

  if (!xVals || xVals.length < 2) return;
  const pad = { l: 42, r: 16, t: 18, b: 26 };
  const pw = w - pad.l - pad.r;
  const ph = h - pad.t - pad.b;

  const isTimeSeries =
    canvasId === 'canvas-timeseries' ||
    canvasId === 'canvas-dual-roi' ||
    canvasId === 'live-roi-canvas';

  const processedSeries = seriesList.map((s) => {
    if (!isTimeSeries) return s;
    const { values } = sanitizeSignalForPlotAndAudio(s.values);
    return { ...s, values };
  });

  let yMin = Infinity;
  let yMax = -Infinity;
  processedSeries.forEach((s) => {
    const skip = isTimeSeries && s.values.length > 6
      ? Math.min(s.values.length - 1, Math.max(4, Math.ceil(s.values.length * 0.05)))
      : 0;
    for (let idx = skip; idx < s.values.length; idx++) {
      const v = s.values[idx];
      if (v < yMin) yMin = v;
      if (v > yMax) yMax = v;
    }
  });
  if (!Number.isFinite(yMin) || !Number.isFinite(yMax) || yMax <= yMin) {
    yMin = -1;
    yMax = 1;
  }

  const xMin = xVals[0];
  const xMax = xVals[xVals.length - 1] || 1;

  // Grid
  ctx.strokeStyle = '#1e293b';
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + (ph * i) / 4;
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(w - pad.r, y);
    ctx.stroke();
  }

  processedSeries.forEach((s) => {
    ctx.save();
    ctx.strokeStyle = s.color;
    ctx.lineWidth = s.dashed ? 1.5 : 1.8;
    if (s.dashed) ctx.setLineDash([5, 4]);
    ctx.beginPath();
    s.values.forEach((rawV, idx) => {
      const v = Math.max(yMin, Math.min(yMax, rawV));
      const x = pad.l + ((xVals[idx] - xMin) / Math.max(xMax - xMin, 1e-6)) * pw;
      const y = pad.t + ph - ((v - yMin) / (yMax - yMin)) * ph;
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.restore();
  });

  // Draw 2f0 and 3f0 harmonic markers on FFT spectrum canvas
  if (canvasId === 'canvas-fft') {
    const f0 =
      state.selectedFftPeakHz !== null && state.selectedFftPeakHz > 0
        ? state.selectedFftPeakHz
        : markerX || 0;
    if (f0 > 0.1) {
      [
        { mult: 2, label: '2f₀' },
        { mult: 3, label: '3f₀' },
      ].forEach(({ mult, label }) => {
        const fh = f0 * mult;
        if (fh >= xMin && fh <= xMax) {
          const hx = pad.l + ((fh - xMin) / Math.max(xMax - xMin, 1e-6)) * pw;
          ctx.save();
          ctx.strokeStyle = 'rgba(168, 85, 247, 0.65)';
          ctx.lineWidth = 1.2;
          ctx.setLineDash([3, 3]);
          ctx.beginPath();
          ctx.moveTo(hx, pad.t);
          ctx.lineTo(hx, pad.t + ph);
          ctx.stroke();
          ctx.fillStyle = '#c084fc';
          ctx.font = 'bold 9.5px sans-serif';
          ctx.fillText(`${label} (${fh.toFixed(1)}Hz)`, Math.min(w - pad.r - 58, hx + 3), pad.t + 22);
          ctx.restore();
        }
      });
    }
  }

  if (markerX !== null && markerX >= xMin && markerX <= xMax) {
    const mx = pad.l + ((markerX - xMin) / Math.max(xMax - xMin, 1e-6)) * pw;
    ctx.strokeStyle = '#f59e0b';
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(mx, pad.t);
    ctx.lineTo(mx, pad.t + ph);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  if (secondaryMarkerX !== null && secondaryMarkerX >= xMin && secondaryMarkerX <= xMax) {
    const sx = pad.l + ((secondaryMarkerX - xMin) / Math.max(xMax - xMin, 1e-6)) * pw;
    ctx.strokeStyle = '#38bdf8';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(sx, pad.t);
    ctx.lineTo(sx, pad.t + ph);
    ctx.stroke();
    ctx.fillStyle = '#38bdf8';
    ctx.font = 'bold 10px sans-serif';
    ctx.fillText(`${secondaryMarkerX.toFixed(2)} Hz`, Math.min(w - pad.r - 52, sx + 4), pad.t + 11);
  }

  ctx.fillStyle = '#94a3b8';
  ctx.font = '11px sans-serif';
  ctx.fillText(`${xMin.toFixed(1)}`, pad.l, h - 6);
  ctx.fillText(`${xLabel}`, pad.l + pw / 2 - 25, h - 6);
  ctx.fillText(`${xMax.toFixed(1)}`, w - pad.r - 28, h - 6);
}

function sampleSpectrogramColormap(norm01) {
  const u = Math.max(0.0, Math.min(1.0, norm01));
  // 5-stop perceptual dark-to-bright Inferno-style palette
  const stops = [
    [5, 8, 22],      // 0.00 (-40 dB floor: deep midnight)
    [48, 18, 98],    // 0.28 (-29 dB: deep indigo-violet)
    [156, 28, 91],   // 0.55 (-18 dB: crimson-magenta)
    [242, 118, 28],  // 0.80 (-8 dB: bright orange-amber)
    [253, 238, 128], // 1.00 (0 dB peak: warm yellow-gold)
  ];
  const scaled = u * (stops.length - 1);
  const idx = Math.min(stops.length - 2, Math.floor(scaled));
  const frac = scaled - idx;
  const c0 = stops[idx];
  const c1 = stops[idx + 1];
  return [
    Math.round(c0[0] + (c1[0] - c0[0]) * frac),
    Math.round(c0[1] + (c1[1] - c0[1]) * frac),
    Math.round(c0[2] + (c1[2] - c0[2]) * frac),
  ];
}

function drawSpectrogram(canvasId, spec) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || !spec || !spec.power_db || spec.power_db.length === 0) return;
  const ctx = canvas.getContext('2d');
  const w = (canvas.width = canvas.clientWidth || 800);
  const h = (canvas.height = canvas.clientHeight || 260);
  ctx.clearRect(0, 0, w, h);

  const allFreqs = Array.isArray(spec.freqs_hz) && spec.freqs_hz.length === spec.power_db.length
    ? spec.freqs_hz
    : spec.power_db.map((_, idx) => idx);
  const nTimes = spec.power_db[0].length;
  if (nTimes === 0) return;

  const fullSpec = state.currentSpectrum || {};
  const nyquistHz = allFreqs[allFreqs.length - 1] || 15.0;
  const maxTargetHz = getActiveFreqMaxHz(fullSpec, nyquistHz);

  const dispFreqs = [];
  const dispRows = [];
  for (let f = 0; f < allFreqs.length; f++) {
    if (allFreqs[f] <= maxTargetHz || dispFreqs.length < 6) {
      dispFreqs.push(allFreqs[f]);
      dispRows.push(spec.power_db[f]);
    }
  }
  const nFreqs = dispRows.length;
  const fMin = dispFreqs[0] || 0.0;
  const fMax = Math.max(fMin + 0.5, dispFreqs[dispFreqs.length - 1] || maxTargetHz);

  const tsArr = (fullSpec.time_series && fullSpec.time_series.t_s) || [];
  const tArr = Array.isArray(spec.times_s) && spec.times_s.length === nTimes ? spec.times_s : [];
  const tMin = 0.0;
  const tMax = Math.max(
    0.5,
    (tsArr.length >= 2 ? tsArr[tsArr.length - 1] - tsArr[0] : 0) ||
      (tArr.length >= 1 ? tArr[tArr.length - 1] : 1.0)
  );

  // Compute robust dB bounds: vmax = 98th percentile (skipping t=0 if nTimes > 2), vmin = vmax - 40 dB
  const sampleVals = [];
  const startT = nTimes > 2 ? 1 : 0;
  for (let f = 0; f < nFreqs; f++) {
    const row = dispRows[f];
    for (let t = startT; t < nTimes; t++) {
      const val = Number(row[t]);
      if (Number.isFinite(val)) sampleVals.push(val);
    }
  }
  sampleVals.sort((a, b) => a - b);
  const p98Idx = sampleVals.length > 0 ? Math.min(sampleVals.length - 1, Math.floor(sampleVals.length * 0.98)) : 0;
  const vmax = sampleVals.length > 0 ? sampleVals[p98Idx] : 0.0;
  const vmin = vmax - 40.0;

  // Render spectrogram cells into offscreen canvas and bilinearly interpolate onto plot region
  const pad = { l: 50, r: 18, t: 22, b: 28 };
  const pw = Math.max(10, w - pad.l - pad.r);
  const ph = Math.max(10, h - pad.t - pad.b);

  const off = document.createElement('canvas');
  off.width = nTimes;
  off.height = nFreqs;
  const offCtx = off.getContext('2d');
  const imgData = offCtx.createImageData(nTimes, nFreqs);
  const data = imgData.data;

  for (let f = 0; f < nFreqs; f++) {
    const yRow = nFreqs - 1 - f;
    const row = dispRows[f];
    for (let t = 0; t < nTimes; t++) {
      // Clamp t=0 against t=1 so first window never produces a left-edge wall
      const rawVal = t === 0 && nTimes > 1 ? Math.min(row[0], row[1] + 1.5) : row[t];
      const norm = (rawVal - vmin) / 40.0;
      const [r, g, b] = sampleSpectrogramColormap(norm);
      const pIdx = (yRow * nTimes + t) * 4;
      data[pIdx] = r;
      data[pIdx + 1] = g;
      data[pIdx + 2] = b;
      data[pIdx + 3] = 255;
    }
  }
  offCtx.putImageData(imgData, 0, 0);

  ctx.save();
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(off, pad.l, pad.t, pw, ph);
  ctx.restore();

  // Plot border
  ctx.strokeStyle = '#334155';
  ctx.lineWidth = 1;
  ctx.strokeRect(pad.l, pad.t, pw, ph);

  // Passband [low_hz, high_hz] horizontal dashed guides
  const jobId = document.getElementById('analysis-job-select') ? document.getElementById('analysis-job-select').value : '';
  const job = state.jobs.find((j) => j.job_id === jobId);
  const lowHz =
    (job && job.params && job.params.low_hz) ||
    (fullSpec.suggested_band && fullSpec.suggested_band.low_hz) ||
    0.5;
  const highHz =
    (job && job.params && job.params.high_hz) ||
    (fullSpec.suggested_band && fullSpec.suggested_band.high_hz) ||
    3.0;

  const freqToY = (hz) => pad.t + ph - ((hz - fMin) / Math.max(fMax - fMin, 1e-6)) * ph;

  ctx.save();
  ctx.strokeStyle = 'rgba(245, 158, 11, 0.72)';
  ctx.lineWidth = 1.2;
  ctx.setLineDash([5, 4]);
  [lowHz, highHz].forEach((hz) => {
    if (hz >= fMin && hz <= fMax) {
      const y = freqToY(hz);
      ctx.beginPath();
      ctx.moveTo(pad.l, y);
      ctx.lineTo(pad.l + pw, y);
      ctx.stroke();
    }
  });
  ctx.restore();

  // Peak frequency ridge line over time (within passband or displayed range)
  const peakPts = [];
  for (let t = 0; t < nTimes; t++) {
    let bestF = -1;
    let bestDb = -Infinity;
    for (let f = 1; f < nFreqs; f++) {
      const hz = dispFreqs[f];
      if (hz >= Math.max(fMin, lowHz * 0.7) && hz <= Math.min(fMax, highHz * 1.3)) {
        const val = t === 0 && nTimes > 1 ? Math.min(dispRows[f][0], dispRows[f][1]) : dispRows[f][t];
        if (val > bestDb) {
          bestDb = val;
          bestF = f;
        }
      }
    }
    if (bestF >= 0) {
      const x = pad.l + (nTimes === 1 ? 0.5 : t / (nTimes - 1)) * pw;
      const y = freqToY(dispFreqs[bestF]);
      peakPts.push({ x, y });
    }
  }
  if (peakPts.length >= 2) {
    ctx.save();
    ctx.strokeStyle = 'rgba(56, 189, 248, 0.9)';
    ctx.lineWidth = 1.75;
    ctx.beginPath();
    peakPts.forEach((pt, idx) => {
      if (idx === 0) ctx.moveTo(pt.x, pt.y);
      else ctx.lineTo(pt.x, pt.y);
    });
    ctx.stroke();
    ctx.restore();
  }

  // Y-Axis (Frequency Hz) ticks & labels
  ctx.fillStyle = '#94a3b8';
  ctx.font = '11px sans-serif';
  ctx.textAlign = 'right';
  const yTicks = 4;
  for (let i = 0; i <= yTicks; i++) {
    const frac = i / yTicks;
    const hz = fMin + frac * (fMax - fMin);
    const y = pad.t + ph - frac * ph;
    ctx.fillText(`${hz.toFixed(hz < 3 ? 2 : 1)}`, pad.l - 6, y + 3);
    ctx.strokeStyle = '#475569';
    ctx.beginPath();
    ctx.moveTo(pad.l - 3, y);
    ctx.lineTo(pad.l, y);
    ctx.stroke();
  }
  ctx.textAlign = 'left';
  ctx.fillStyle = '#cbd5e1';
  ctx.font = 'bold 10px sans-serif';
  ctx.fillText('Hz', 6, pad.t + 10);

  // X-Axis (Time s) ticks & labels
  ctx.fillStyle = '#94a3b8';
  ctx.font = '11px sans-serif';
  const xTicks = 4;
  for (let i = 0; i <= xTicks; i++) {
    const frac = i / xTicks;
    const tVal = tMin + frac * (tMax - tMin);
    const x = pad.l + frac * pw;
    ctx.textAlign = i === 0 ? 'left' : i === xTicks ? 'right' : 'center';
    ctx.fillText(`${tVal.toFixed(1)}s`, x, h - 7);
  }
  ctx.textAlign = 'center';
  ctx.fillStyle = '#cbd5e1';
  ctx.fillText('Time (s)', pad.l + pw * 0.5, pad.t - 6);

  // Top-right compact -40 dB to 0 dB colorbar legend
  const cbW = 88;
  const cbH = 8;
  const cbX = pad.l + pw - cbW - 8;
  const cbY = pad.t + 8;
  ctx.fillStyle = 'rgba(5, 8, 17, 0.78)';
  ctx.fillRect(cbX - 42, cbY - 4, cbW + 76, cbH + 10);
  for (let i = 0; i < cbW; i++) {
    const [r, g, b] = sampleSpectrogramColormap(i / Math.max(1, cbW - 1));
    ctx.fillStyle = `rgb(${r},${g},${b})`;
    ctx.fillRect(cbX + i, cbY, 1, cbH);
  }
  ctx.strokeStyle = '#475569';
  ctx.strokeRect(cbX, cbY, cbW, cbH);
  ctx.fillStyle = '#e2e8f0';
  ctx.font = '9.5px sans-serif';
  ctx.textAlign = 'right';
  ctx.fillText('-40 dB', cbX - 4, cbY + 7);
  ctx.textAlign = 'left';
  ctx.fillText('0 dB', cbX + cbW + 4, cbY + 7);
}

async function pollLiveRoiSignal() {
  try {
    const recPage = document.getElementById('page-record');
    if (recPage && recPage.classList.contains('active')) {
      const sigEl = document.getElementById('trig-sigma');
      const pctEl = document.getElementById('trig-pct');
      const threshSigma = sigEl ? parseFloat(sigEl.value) || 3.5 : 3.5;
      const threshPct = pctEl ? parseFloat(pctEl.value) || 2.5 : 2.5;
      const data = await api(`/api/cameras/live-signal?threshold_sigma=${threshSigma}&threshold_pct=${threshPct}`);
      const pts = data.points || [];
      if (pts.length > 2) {
        drawLinePlot(
          'live-roi-canvas',
          pts.map((p) => p.t),
          [{ values: pts.map((p) => p.detrended), color: '#38bdf8', label: 'ROI' }],
          'Time (s)'
        );
      }
      if (data.trigger) {
        const badge = document.getElementById('trigger-status-badge');
        if (state.triggerArmed && !state.triggerRecordingInFlight) {
          if (data.trigger.triggered) {
            state.triggerArmed = false;
            const btn = document.getElementById('btn-arm-trigger');
            if (btn) {
              btn.textContent = 'Arm Motion Trigger';
              btn.className = '';
            }
            if (badge) {
              badge.textContent = `TRIGGERED! (${data.trigger.current_z_sigma}σ / ${data.trigger.current_pct_jump}% jump) — Capturing with 2s Pre-Roll`;
              badge.className = 'badge danger';
            }
            startRecording({ includePreroll: true, prerollSeconds: 2.0 });
          } else if (badge) {
            badge.textContent = `Armed (${data.trigger.preroll_frames} pre-roll frames | live ${data.trigger.current_z_sigma}σ, ${data.trigger.current_pct_jump}%)`;
            badge.className = 'badge global';
          }
        }
      }
    }
  } catch (_e) {
    // Ignore transient preview poll errors
  }
}

async function loadSystemStatus() {
  const data = await api('/api/system');
  document.getElementById('sys-version').textContent = data.current_version;
  if (data.hardware) {
    updateThermalBadge(data.hardware);
    const hwEl = document.getElementById('sys-hw-model');
    if (hwEl) hwEl.textContent = data.hardware.model;
    const ramEl = document.getElementById('sys-ram-gb');
    if (ramEl) ramEl.textContent = `${data.hardware.total_ram_gb} GB (${data.hardware.available_ram_gb} GB free)`;
    const tempEl = document.getElementById('sys-cpu-temp');
    if (tempEl && data.hardware.cpu_temp_c !== undefined) {
      tempEl.textContent = `${data.hardware.cpu_temp_c} °C`;
    }
    const thermEl = document.getElementById('sys-thermal-status');
    if (thermEl) {
      thermEl.textContent = data.hardware.is_throttled
        ? `Throttling (${data.hardware.throttled_hex})`
        : `${(data.hardware.thermal_status || 'nominal').toUpperCase()} (${data.hardware.throttled_hex || '0x0'})`;
    }
  }
  document.getElementById('sys-free-gb').textContent = `${data.disk.free_gb} GB`;
  const dataMb = (data.disk.data_dir_bytes / (1024 * 1024)).toFixed(2);
  document.getElementById('sys-data-mb').textContent = `${dataMb} MB`;
  document.getElementById('sys-data-dir').textContent = data.data_dir;

  const clEl = document.getElementById('sys-changelog');
  clEl.innerHTML = data.changelog
    .map(
      (c) =>
        `<div><strong>v${c.version}</strong> (${c.date})<ul style="padding-left:1.2rem;margin-top:0.25rem;">` +
        c.notes.map((n) => `<li>${n}</li>`).join('') +
        `</ul></div>`
    )
    .join('');

  const logEl = document.getElementById('sys-logs');
  logEl.innerHTML = (data.logs || [])
    .slice()
    .reverse()
    .map((l) => `<div>[${l.timestamp || ''}] <strong>${l.level}</strong>: ${l.message}</div>`)
    .join('');
}

async function triggerSystemUpdate() {
  const res = await api('/api/system/update', { method: 'POST' });
  alert(`Update Status: ${res.status}\n${res.output || res.message || ''}`);
  await loadSystemStatus();
}

function drawMotionOrbit(canvasId, orbit) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || !orbit) return;
  const ctx = canvas.getContext('2d');
  const w = (canvas.width = canvas.clientWidth || 720);
  const h = (canvas.height = canvas.clientHeight || 280);
  ctx.clearRect(0, 0, w, h);

  const xs = Array.isArray(orbit.x_um) && orbit.x_um.length > 2 ? orbit.x_um : orbit.x_px || [];
  const ys = Array.isArray(orbit.y_um) && orbit.y_um.length > 2 ? orbit.y_um : orbit.y_px || [];
  const unit = Array.isArray(orbit.x_um) && orbit.x_um.length > 2 ? 'µm' : 'px';
  const n = Math.min(xs.length, ys.length);
  if (n < 3) return;

  const badge = document.getElementById('orbit-summary-badge');
  if (badge) {
    const theta = orbit.principal_angle_deg !== undefined ? orbit.principal_angle_deg : 0;
    const sx = orbit.span_x_um !== undefined ? orbit.span_x_um : 0;
    const sy = orbit.span_y_um !== undefined ? orbit.span_y_um : 0;
    const ecc = orbit.eccentricity !== undefined ? orbit.eccentricity : 0.9;
    const shapeType = ecc > 0.85 ? 'Rectilinear / Uniaxial' : 'Elliptical / Whirling';
    badge.textContent = `Principal Axis: θ = ${theta}° | Span: ΔX = ${sx} µm, ΔY = ${sy} µm (${shapeType})`;
  }

  let maxAbs = 1e-3;
  for (let i = 0; i < n; i++) {
    maxAbs = Math.max(maxAbs, Math.abs(xs[i]), Math.abs(ys[i]));
  }
  maxAbs *= 1.18;

  const pad = { l: 56, r: 24, t: 22, b: 28 };
  const pw = Math.max(20, w - pad.l - pad.r);
  const ph = Math.max(20, h - pad.t - pad.b);
  const cx = pad.l + pw * 0.5;
  const cy = pad.t + ph * 0.5;
  const scaleX = (pw * 0.5) / maxAbs;
  const scaleY = (ph * 0.5) / maxAbs;

  const toCanvas = (xVal, yVal) => ({
    x: cx + xVal * scaleX,
    y: cy - yVal * scaleY,
  });

  // Grid & zero axes
  ctx.strokeStyle = '#1e293b';
  ctx.lineWidth = 1;
  ctx.strokeRect(pad.l, pad.t, pw, ph);

  ctx.strokeStyle = '#334155';
  ctx.beginPath();
  ctx.moveTo(pad.l, cy);
  ctx.lineTo(pad.l + pw, cy);
  ctx.moveTo(cx, pad.t);
  ctx.lineTo(cx, pad.t + ph);
  ctx.stroke();

  // Principal axis dashed guide line at angle theta
  const thetaRad = ((orbit.principal_angle_deg || 0) * Math.PI) / 180.0;
  const p0 = toCanvas(-maxAbs * 0.92 * Math.cos(thetaRad), -maxAbs * 0.92 * Math.sin(thetaRad));
  const p1 = toCanvas(maxAbs * 0.92 * Math.cos(thetaRad), maxAbs * 0.92 * Math.sin(thetaRad));
  ctx.save();
  ctx.strokeStyle = 'rgba(148, 163, 184, 0.45)';
  ctx.lineWidth = 1.25;
  ctx.setLineDash([6, 4]);
  ctx.beginPath();
  ctx.moveTo(p0.x, p0.y);
  ctx.lineTo(p1.x, p1.y);
  ctx.stroke();
  ctx.restore();

  // Trajectory color-graded from Cyan (#38bdf8 = 56,189,248) to Amber (#f59e0b = 245,158,11)
  for (let i = 0; i < n - 1; i++) {
    const u = i / Math.max(1, n - 2);
    const r = Math.round(56 + (245 - 56) * u);
    const g = Math.round(189 + (158 - 189) * u);
    const b = Math.round(248 + (11 - 248) * u);
    const a0 = toCanvas(xs[i], ys[i]);
    const a1 = toCanvas(xs[i + 1], ys[i + 1]);
    ctx.strokeStyle = `rgb(${r},${g},${b})`;
    ctx.lineWidth = 2.0;
    ctx.beginPath();
    ctx.moveTo(a0.x, a0.y);
    ctx.lineTo(a1.x, a1.y);
    ctx.stroke();

    // Directional arrowheads at 35% and 75% of the trajectory
    if (i === Math.floor(n * 0.35) || i === Math.floor(n * 0.75)) {
      const dx = a1.x - a0.x;
      const dy = a1.y - a0.y;
      const len = Math.hypot(dx, dy);
      if (len > 1.5) {
        const ang = Math.atan2(dy, dx);
        const headLen = 8;
        ctx.fillStyle = `rgb(${r},${g},${b})`;
        ctx.beginPath();
        ctx.moveTo(a1.x, a1.y);
        ctx.lineTo(a1.x - headLen * Math.cos(ang - 0.45), a1.y - headLen * Math.sin(ang - 0.45));
        ctx.lineTo(a1.x - headLen * Math.cos(ang + 0.45), a1.y - headLen * Math.sin(ang + 0.45));
        ctx.closePath();
        ctx.fill();
      }
    }
  }

  // Axis labels
  ctx.fillStyle = '#94a3b8';
  ctx.font = '11px sans-serif';
  ctx.textAlign = 'center';
  ctx.fillText(`Horizontal X (${unit})`, cx, h - 7);
  ctx.textAlign = 'left';
  ctx.fillText(`-${maxAbs.toFixed(1)}`, pad.l, h - 7);
  ctx.textAlign = 'right';
  ctx.fillText(`+${maxAbs.toFixed(1)}`, pad.l + pw, h - 7);
  ctx.fillText(`+${maxAbs.toFixed(1)} ${unit}`, pad.l - 6, pad.t + 10);
  ctx.fillText(`0`, pad.l - 6, cy + 4);
  ctx.fillText(`-${maxAbs.toFixed(1)} ${unit}`, pad.l - 6, pad.t + ph);
}

function drawMotionOrbit3D(canvasId, orbit3d) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || !orbit3d) return;
  const ctx = canvas.getContext('2d');
  const w = (canvas.width = canvas.clientWidth || 580);
  const h = (canvas.height = canvas.clientHeight || 300);
  ctx.clearRect(0, 0, w, h);

  const xs = orbit3d.x_um || [];
  const ys = orbit3d.y_um || [];
  const zs = orbit3d.z_um || [];
  const n = Math.min(xs.length, ys.length, zs.length);
  if (n < 3) return;

  const badge = document.getElementById('orbit3d-summary-badge');
  if (badge) {
    badge.textContent =
      `3D RMS Radius: ${orbit3d.radius_3d_rms_um ?? 0} µm | Span ΔX=${orbit3d.span_x_um ?? 0} µm, ΔY=${orbit3d.span_y_um ?? 0} µm, ΔZ=${orbit3d.span_z_um ?? 0} µm`;
  }

  let maxAbs = 1e-3;
  for (let i = 0; i < n; i++) {
    maxAbs = Math.max(maxAbs, Math.abs(xs[i]), Math.abs(ys[i]), Math.abs(zs[i]));
  }
  maxAbs *= 1.25;

  const cx = w * 0.5;
  const cy = h * 0.54;
  const scale = (Math.min(w, h) * 0.36) / maxAbs;
  const cos30 = Math.cos(Math.PI / 6);
  const sin30 = Math.sin(Math.PI / 6);

  const proj3D = (x, y, z) => ({
    px: cx + (x - z) * cos30 * scale,
    py: cy - y * scale + (x + z) * sin30 * 0.55 * scale,
  });

  // Draw isometric reference cube axes
  const axes = [
    { end: [maxAbs, 0, 0], neg: [-maxAbs, 0, 0], color: '#38bdf8', label: '+X (Cam A)' },
    { end: [0, maxAbs, 0], neg: [0, -maxAbs, 0], color: '#22c55e', label: '+Y (Shared)' },
    { end: [0, 0, maxAbs], neg: [0, 0, -maxAbs], color: '#f59e0b', label: '+Z (Cam B)' },
  ];
  axes.forEach((ax) => {
    const pNeg = proj3D(ax.neg[0], ax.neg[1], ax.neg[2]);
    const pPos = proj3D(ax.end[0], ax.end[1], ax.end[2]);
    ctx.strokeStyle = 'rgba(148, 163, 184, 0.35)';
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.moveTo(pNeg.px, pNeg.py);
    ctx.lineTo(pPos.px, pPos.py);
    ctx.stroke();

    ctx.fillStyle = ax.color;
    ctx.font = 'bold 11px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(ax.label, pPos.px, pPos.py - 6);
  });

  // Draw 3D trajectory color-graded Cyan -> Amber
  for (let i = 0; i < n - 1; i++) {
    const u = i / Math.max(1, n - 2);
    const r = Math.round(56 + (245 - 56) * u);
    const g = Math.round(189 + (158 - 189) * u);
    const b = Math.round(248 + (11 - 248) * u);
    const p0 = proj3D(xs[i], ys[i], zs[i]);
    const p1 = proj3D(xs[i + 1], ys[i + 1], zs[i + 1]);
    ctx.strokeStyle = `rgb(${r},${g},${b})`;
    ctx.lineWidth = 2.1;
    ctx.beginPath();
    ctx.moveTo(p0.px, p0.py);
    ctx.lineTo(p1.px, p1.py);
    ctx.stroke();
  }
}

window.addEventListener('DOMContentLoaded', async () => {
  refreshDefaultClipTitleIfUnedited();
  const titleEl = document.getElementById('rec-title');
  if (titleEl) {
    titleEl.addEventListener('input', () => {
      state.userEditedClipTitle = true;
    });
  }
  const maxHzEl = document.getElementById('rec-max-hz');
  if (maxHzEl) {
    maxHzEl.addEventListener('input', () => {
      state.userEditedMaxHz = true;
    });
  }
  initPreviewRoiDrag();
  initSplitWipeComparator();
  initInteractiveFftCanvas();
  initKymographRefPicker();
  initDualRoiRefPicker();
  await initCameras();
  await initPresetsAndMethods();
  await runPreflight();
  await loadLibrary();
  setInterval(pollLiveRoiSignal, 1200);
  setInterval(() => {
    const procPage = document.getElementById('page-process');
    if (procPage && procPage.classList.contains('active')) {
      loadJobs();
    }
  }, 2500);
});

