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
};

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

function getOptionalRoi() {
  const rx = parseInt(document.getElementById('roi-x').value, 10);
  const ry = parseInt(document.getElementById('roi-y').value, 10);
  const rw = parseInt(document.getElementById('roi-w').value, 10);
  const rh = parseInt(document.getElementById('roi-h').value, 10);
  if (!Number.isNaN(rw) && !Number.isNaN(rh) && rw > 8 && rh > 8) {
    return [Number.isNaN(rx) ? 0 : rx, Number.isNaN(ry) ? 0 : ry, rw, rh];
  }
  return null;
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

  const warnBanner = document.getElementById('shutter-warning-banner');
  if (data.capabilities.rolling_shutter_warning) {
    warnBanner.textContent = data.capabilities.rolling_shutter_warning;
    warnBanner.style.display = 'block';
  } else {
    warnBanner.style.display = 'none';
  }

  const sel = document.getElementById('setup-camera-select');
  sel.innerHTML = '';
  state.cameras.forEach((c) => {
    const opt = document.createElement('option');
    opt.value = c.camera_id;
    opt.textContent = `${c.name} [${c.camera_id}] (${c.shutter_type} shutter)`;
    if (c.camera_id === state.activeCameraId) opt.selected = true;
    sel.appendChild(opt);
  });

  if (data.capabilities.lens_info) {
    document.getElementById('setup-focal').value = data.capabilities.lens_info.focal_length_mm || 4.0;
    document.getElementById('setup-fov').value = data.capabilities.lens_info.fov_degrees || 60.0;
    document.getElementById('setup-focus-mode').value = data.capabilities.lens_info.focus_mode || 'fixed';
  }

  if (!data.setup_completed) {
    openSetupModal();
  }
}

async function saveCameraSetup() {
  const camId = document.getElementById('setup-camera-select').value;
  await api(`/api/cameras/${encodeURIComponent(camId)}/lens`, {
    method: 'POST',
    body: JSON.stringify({
      focal_length_mm: parseFloat(document.getElementById('setup-focal').value),
      fov_degrees: parseFloat(document.getElementById('setup-fov').value),
      focus_mode: document.getElementById('setup-focus-mode').value,
    }),
  });
  await api('/api/cameras/select', {
    method: 'POST',
    body: JSON.stringify({ camera_id: camId, complete_setup: true }),
  });
  closeSetupModal();
  await initCameras();
  // Refresh MJPEG preview stream
  document.getElementById('live-preview-img').src = `/api/cameras/preview.mjpg?t=${Date.now()}`;
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
  const lockBadge = document.getElementById('lock-status-badge');
  lockBadge.textContent = `Locked (${res.controls.exposure_us} us, Gain ${res.controls.gain})`;
  lockBadge.className = 'badge global';
  await runPreflight();
}

async function runPreflight() {
  const payload = {
    width: parseInt(document.getElementById('rec-width').value, 10) || 640,
    height: parseInt(document.getElementById('rec-height').value, 10) || 480,
    fps: parseFloat(document.getElementById('rec-fps').value) || 30.0,
    duration_s: parseFloat(document.getElementById('rec-duration').value) || 6.0,
    min_freq_hz: 0.5,
    max_freq_hz: parseFloat(document.getElementById('rec-max-hz').value) || 3.5,
    storage_format: document.getElementById('rec-format').value,
    roi: getOptionalRoi(),
  };
  const res = await api('/api/record/preflight', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
  const banner = document.getElementById('preflight-banner');
  const nyq = res.nyquist;
  const st = res.storage;

  let html = `<strong>Nyquist Limit:</strong> ${nyq.nyquist_limit_hz} Hz (Freq Resolution &Delta;f = ${nyq.freq_resolution_hz} Hz) &bull; `;
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

async function startRecording() {
  const btn = document.getElementById('btn-record');
  const resBanner = document.getElementById('record-result-banner');
  btn.disabled = true;
  btn.textContent = 'Recording in progress (controls locked)...';
  resBanner.style.display = 'none';

  try {
    const payload = {
      title: document.getElementById('rec-title').value || 'Vibration Capture',
      duration_s: parseFloat(document.getElementById('rec-duration').value) || 5.0,
      width: parseInt(document.getElementById('rec-width').value, 10) || 640,
      height: parseInt(document.getElementById('rec-height').value, 10) || 480,
      fps: parseInt(document.getElementById('rec-fps').value, 10) || 30,
      storage_format: document.getElementById('rec-format').value,
      roi: getOptionalRoi(),
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
    resBanner.style.display = 'block';
    resBanner.innerHTML = `
      <strong>Clip Recorded:</strong> ${meta.title} (${meta.clip_id})<br/>
      Achieved Sample Rate: <strong>${meta.achieved_fps} FPS</strong> (Target ${meta.target_fps} FPS, Jitter &sigma; = ${meta.jitter_std_ms} ms) &bull;
      Frames: ${meta.frame_count} &bull;
      <a href="#" style="color: var(--accent);" onclick="openClipInProcess('${meta.clip_id}'); return false;">Process This Clip &rarr;</a>
    `;
    await loadLibrary();
  } catch (err) {
    resBanner.style.display = 'block';
    resBanner.className = 'banner danger';
    resBanner.textContent = `Recording failed: ${err.message}`;
  } finally {
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
    card.innerHTML = `
      <div class="video-box" style="margin-bottom: 0.75rem;">
        <img src="/api/library/${c.clip_id}/files/thumb.jpg" alt="${c.title}" />
      </div>
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.35rem;">
        <strong>${c.title}</strong>
        <span class="badge ${c.shutter_type}">${c.shutter_type}</span>
      </div>
      <p style="font-size: 0.8rem; color: var(--text-muted); margin-bottom: 0.75rem;">
        ${c.width}&times;${c.height} &bull; Achieved <strong>${c.achieved_fps} FPS</strong> (Jitter ${c.jitter_std_ms || 0} ms)<br/>
        ${c.frame_count} frames (${c.duration_s}s) &bull; ${mb} MB &bull; ${jobCount} processed run(s)
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

function onSelectProcessClip() {
  updateJobTimeEstimate();
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
  document.getElementById('job-estimate-banner').textContent =
    `Estimated offline processing time on Raspberry Pi: ~${res.estimated_seconds}s (${frames} frames @ ${w}x${h})`;
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
  const prevAnalysisJob = analysisSelect.value;

  listEl.innerHTML = '';
  analysisSelect.innerHTML = '';

  const completedJobs = state.jobs.filter((j) => j.status === 'completed');
  completedJobs.forEach((j) => {
    const opt = document.createElement('option');
    opt.value = j.job_id;
    const hz = j.result ? j.result.dominant_hz : 0;
    const bpm = j.result ? j.result.dominant_bpm : 0;
    opt.textContent = `${j.method.toUpperCase()} — ${hz} Hz (${bpm} BPM) [${j.job_id}]`;
    analysisSelect.appendChild(opt);
  });

  if (prevAnalysisJob && completedJobs.some((j) => j.job_id === prevAnalysisJob)) {
    analysisSelect.value = prevAnalysisJob;
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

  if (!state.activeResultJob && completedJobs.length > 0) {
    selectJobForResultView(completedJobs[0].job_id);
  }
  await loadComparisonForSelectedClip();
}

function selectJobForResultView(jobId) {
  const job = state.jobs.find((j) => j.job_id === jobId);
  if (!job || job.status !== 'completed') return;
  state.activeResultJob = job;
  updateComparisonVideoMode();
}

function updateComparisonVideoMode() {
  const job = state.activeResultJob;
  if (!job || !job.result || !job.result.files) return;
  const modeKey = document.getElementById('view-output-mode').value;
  const filename = job.result.files[modeKey] || 'comparison_sbs.mp4';
  const player = document.getElementById('result-video-player');
  player.src = `/api/jobs/${job.job_id}/files/${filename}`;
  player.play().catch(() => {});

  const dlBar = document.getElementById('result-download-bar');
  dlBar.innerHTML = `
    <a class="btn primary" href="/api/jobs/${job.job_id}/files/comparison_sbs.mp4" download="${job.job_id}_sbs.mp4">Download Side-by-Side MP4</a>
    <a class="btn" href="/api/jobs/${job.job_id}/files/magnified_share.mp4" download="${job.job_id}_magnified.mp4">Download Magnified MP4</a>
    <a class="btn" href="/api/jobs/${job.job_id}/files/heatmap.mp4" download="${job.job_id}_heatmap.mp4">Download Heatmap MP4</a>
    <a class="btn" href="/api/jobs/${job.job_id}/files/magnified_lossless.avi" download="${job.job_id}_lossless.avi">Lossless AVI</a>
    <a class="btn" href="/api/jobs/${job.job_id}/files/signal.csv" download="${job.job_id}_signal.csv">Signal CSV</a>
  `;
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

async function loadJobAnalysis() {
  const jobId = document.getElementById('analysis-job-select').value;
  if (!jobId) return;
  document.getElementById('btn-export-csv').href = `/api/jobs/${jobId}/files/signal.csv`;
  const spec = await api(`/api/jobs/${jobId}/spectrum`);

  document.getElementById('kpi-dom-hz').textContent = `${spec.dominant_hz} Hz`;
  document.getElementById('kpi-dom-bpm').textContent = `${spec.dominant_bpm} BPM`;
  document.getElementById('kpi-snr').textContent = `${spec.snr_db} dB`;
  if (spec.suggested_band) {
    document.getElementById('kpi-sug-band').textContent =
      `${spec.suggested_band.low_hz}–${spec.suggested_band.high_hz} Hz`;
  }

  drawLinePlot(
    'canvas-timeseries',
    spec.time_series.t_s,
    [
      { values: spec.time_series.raw, color: '#64748b', label: 'Raw Detrended' },
      { values: spec.time_series.filtered, color: '#38bdf8', label: 'Bandpass Filtered' },
    ],
    'Time (s)'
  );

  drawLinePlot(
    'canvas-fft',
    spec.freqs_hz,
    [{ values: spec.fft_magnitude, color: '#22c55e', label: 'FFT Magnitude' }],
    'Frequency (Hz)',
    spec.dominant_hz
  );

  drawSpectrogram('canvas-spectrogram', spec.spectrogram);
}

function drawLinePlot(canvasId, xVals, seriesList, xLabel, markerX = null) {
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

  let yMin = Infinity;
  let yMax = -Infinity;
  seriesList.forEach((s) => {
    s.values.forEach((v) => {
      if (v < yMin) yMin = v;
      if (v > yMax) yMax = v;
    });
  });
  if (yMax <= yMin) yMax = yMin + 1;

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

  seriesList.forEach((s) => {
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 1.8;
    ctx.beginPath();
    s.values.forEach((v, idx) => {
      const x = pad.l + ((xVals[idx] - xMin) / Math.max(xMax - xMin, 1e-6)) * pw;
      const y = pad.t + ph - ((v - yMin) / (yMax - yMin)) * ph;
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  });

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

  ctx.fillStyle = '#94a3b8';
  ctx.font = '11px sans-serif';
  ctx.fillText(`${xMin.toFixed(1)}`, pad.l, h - 6);
  ctx.fillText(`${xLabel}`, pad.l + pw / 2 - 25, h - 6);
  ctx.fillText(`${xMax.toFixed(1)}`, w - pad.r - 28, h - 6);
}

function drawSpectrogram(canvasId, spec) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || !spec || !spec.power_db || spec.power_db.length === 0) return;
  const ctx = canvas.getContext('2d');
  const w = (canvas.width = canvas.clientWidth || 800);
  const h = (canvas.height = canvas.clientHeight || 200);
  ctx.clearRect(0, 0, w, h);

  const nFreqs = spec.power_db.length;
  const nTimes = spec.power_db[0].length;
  if (nTimes === 0) return;

  let minDb = Infinity;
  let maxDb = -Infinity;
  spec.power_db.forEach((row) =>
    row.forEach((v) => {
      if (v < minDb) minDb = v;
      if (v > maxDb) maxDb = v;
    })
  );
  const cellW = w / nTimes;
  const cellH = h / nFreqs;

  for (let f = 0; f < nFreqs; f++) {
    for (let t = 0; t < nTimes; t++) {
      const norm = (spec.power_db[f][t] - minDb) / Math.max(maxDb - minDb, 1e-6);
      const r = Math.round(255 * Math.min(1, norm * 1.4));
      const g = Math.round(180 * Math.max(0, norm - 0.3));
      const b = Math.round(160 * (1 - norm));
      ctx.fillStyle = `rgb(${r},${g},${b})`;
      ctx.fillRect(t * cellW, h - (f + 1) * cellH, Math.ceil(cellW), Math.ceil(cellH));
    }
  }
}

async function pollLiveRoiSignal() {
  try {
    const recPage = document.getElementById('page-record');
    if (recPage && recPage.classList.contains('active')) {
      const data = await api('/api/cameras/live-signal');
      const pts = data.points || [];
      if (pts.length > 2) {
        drawLinePlot(
          'live-roi-canvas',
          pts.map((p) => p.t),
          [{ values: pts.map((p) => p.detrended), color: '#38bdf8', label: 'ROI' }],
          'Time (s)'
        );
      }
    }
  } catch (_e) {
    // Ignore transient preview poll errors
  }
}

async function loadSystemStatus() {
  const data = await api('/api/system');
  document.getElementById('sys-version').textContent = data.current_version;
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

window.addEventListener('DOMContentLoaded', async () => {
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
