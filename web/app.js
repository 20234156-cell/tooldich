/* DuanLayLink Web UI — Fast Hardsub Dubbing Studio v3.0 */

const API = {
  async request(method, url, body) {
    if(method==='POST' && body?.repair_existing && /\/(voiceover|run-all)$/.test(url)){
      const contract=await API.request('GET','/openapi.json');
      if(!contract?.components?.schemas?.VoiceoverRequest?.properties?.repair_existing){
        throw new Error('Backend đang chạy bản cũ, chưa hỗ trợ AI sửa bản dịch cũ. Hãy khởi động lại ứng dụng/server; F5 không nạp lại backend. Chưa gửi tác vụ.');
      }
      const health=await API.request('GET','/api/system/health');
      if(health?.timing_repair_version!=='source-grounded-recovery-v2'){
        throw new Error('Backend chưa nạp bản sửa timing mới (chuyển model và tách nhãn cảnh). Hãy khởi động lại server trước khi chạy; chưa gửi tác vụ.');
      }
    }
    const options = { method, headers: { "Content-Type": "application/json" } };
    if (body !== undefined) options.body = JSON.stringify(body);
    let res;
    try {
      res = await fetch(url, options);
    } catch (netErr) {
      const err = new Error(
        "Không thể kết nối đến máy chủ backend (127.0.0.1:8088). Vui lòng kiểm tra cửa sổ chạy lệnh start_server.bat xem server còn đang chạy không."
      );
      err.code = "NETWORK_ERROR";
      throw err;
    }
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {}
    if (!res.ok) {
      let msg = data && data.message;
      if (!msg && data && data.detail) {
        msg = typeof data.detail === "string"
          ? data.detail
          : (Array.isArray(data.detail) ? data.detail.map((d) => d.msg || JSON.stringify(d)).join(", ") : JSON.stringify(data.detail));
      }
      if (!msg) msg = res.statusText || ("HTTP " + res.status);
      const err = new Error(msg);
      err.code = (data && data.error_code) || ("HTTP_" + res.status);
      throw err;
    }
    return data;
  },

  // Projects CRUD
  listProjects: (status) =>
    API.request("GET", "/api/projects" + (status ? `?status=${encodeURIComponent(status)}` : "")),
  createProject: (url, trimStart, trimEnd) =>
    API.request("POST", "/api/projects", {
      url,
      trim_start: trimStart || null,
      trim_end: trimEnd || null,
    }),
  createBatch: (urls, trimStart, trimEnd) =>
    API.request("POST", "/api/projects/batch", {
      urls,
      trim_start: trimStart || null,
      trim_end: trimEnd || null,
    }),
  getProject: (id) => API.request("GET", `/api/projects/${id}`),
  deleteProject: (id) => API.request("DELETE", `/api/projects/${id}`),
  getProjectState: (id) => API.request("GET", `/api/projects/${id}/state`),

  // Pipeline execution & tasks
  runStep: (id, action, payload) => API.request("POST", `/api/projects/${id}/${action}`, payload || {}),
  getTask: (taskId) => API.request("GET", `/api/tasks/${taskId}`),
  cancelJob: (jobId) => API.request("POST", `/api/jobs/${jobId}/cancel`, {}),
  runAll: (id, payload) => API.request("POST", `/api/projects/${id}/run-all`, payload || {}),
  remix: (id, payload) => API.request("POST", `/api/projects/${id}/remix`, payload),

  // ROI operations
  getROI: (id) => API.request("GET", `/api/projects/${id}/roi`),
  saveROI: (id, roi) => API.request("POST", `/api/projects/${id}/roi`, { rois: [{
    id: "roi_sub", type: "chinese_subtitle", x: roi.xmin, y: roi.ymin,
    width: roi.xmax - roi.xmin, height: roi.ymax - roi.ymin,
  }] }),
  autoDetectROI: (id) => API.request("POST", `/api/projects/${id}/roi/auto-detect`),
  previewSample: (id, seconds) => API.request("POST", `/api/projects/${id}/roi/preview-sample`, { sample_seconds: seconds }),

  // OCR cues
  runOCR: (id, payload) => API.request("POST", `/api/projects/${id}/ocr`, payload || {}),
  getCues: (id) => API.request("GET", `/api/projects/${id}/cues`),
  updateCue: (id, index, payload) => API.request("PATCH", `/api/projects/${id}/cues/${index}`, payload),
  deleteCue: (id, index) => API.request("DELETE", `/api/projects/${id}/cues/${index}`),
  splitCue: (id, index, payload) => API.request("POST", `/api/projects/${id}/cues/${index}/split`, payload),
  mergeCue: (id, index, targetCueIndex) => API.request("POST", `/api/projects/${id}/cues/${index}/merge`, { target_cue_index: targetCueIndex }),
  findReplace: (id, payload) => API.request("POST", `/api/projects/${id}/cues/find-replace`, payload),
  singleCueTTS: (id, index, payload) => API.request("POST", `/api/projects/${id}/cues/${index}/tts`, payload || {}),
  approveCues: (id) => API.request("POST", `/api/projects/${id}/cues/approve`),

  // Translation
  runTranslate: (id, payload) => API.request("POST", `/api/projects/${id}/translate`, {provider: document.getElementById("translation-provider")?.value || "gemini", ...payload}),
  approveTranslate: (id) => API.request("POST", `/api/projects/${id}/translate/approve`),
  getSubtitleSettings: (id) => API.request("GET", `/api/projects/${id}/subtitle-settings`),
  saveSubtitleSettings: (id, payload) => API.request("POST", `/api/projects/${id}/subtitle-settings`, payload),

  // Render & Verify
  getRenderPreviewUrl: (id, options) => {
    const params = new URLSearchParams({ ...options, _r: Date.now() });
    return `/api/projects/${id}/render-preview.jpg?${params.toString()}`;
  },
  runRender: (id, payload) => API.request("POST", `/api/projects/${id}/render`, payload || {}),
  getVerify: (id) => API.request("POST", `/api/projects/${id}/verify`, {}),

  // System & Keys
  systemHealth: () => API.request("GET", "/api/system/health"),
  getKeys: () => API.request("GET", "/api/settings/keys"),
  addKeys: (keys) => API.request("POST", "/api/settings/keys", { keys }),
  testKey: (keyId) => API.request("POST", `/api/settings/keys/${keyId}/test`),
  deleteKey: (keyId) => API.request("DELETE", `/api/settings/keys/${keyId}`),
};

const TASK_ACTION_LABELS = {
  source: "Kiểm tra nguồn video",
  download: "Tải và kiểm tra video",
  ocr: "Bóc phụ đề tiếng Trung",
  translate: "Dịch phụ đề tiếng Việt",
  voiceover: "Tạo giọng đọc",
  remix: "Trộn lại âm thanh",
  render: "Render video hoàn chỉnh",
  run_all: "Chạy toàn bộ quy trình",
};

function formatClock(epochSeconds) {
  if (!epochSeconds) return "—";
  return new Date(epochSeconds * 1000).toLocaleTimeString("vi-VN", {
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

function formatSpan(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds))) return "Đang tính…";
  const total = Math.max(0, Math.round(Number(seconds)));
  if (total < 60) return `${total} giây`;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return [hours ? `${hours} giờ` : "", minutes ? `${minutes} phút` : "", (!hours && secs) ? `${secs} giây` : ""].filter(Boolean).join(" ");
}

function renderTaskActivity(task) {
  if (!task || !DOM.taskActivity) return;
  if (task.project_id && task.project_id !== currentProjectId) return;
  if(typeof window!=="undefined"&&typeof window.dispatchEvent==="function")window.dispatchEvent(new CustomEvent("workspace-task-update",{detail:task}));
  if (DOM.nextActionDesc && task.state === 'RUNNING') {
    DOM.nextActionDesc.textContent = `Đang chạy: ${task.stage || task.action}`;
  }
  DOM.taskActivity.hidden = false;
  DOM.taskActivity.dataset.state = task.state || "";
  const pct = Math.max(0, Math.min(100, Number(task.progress) || 0));
  const stateLabels = { QUEUED: "ĐANG CHỜ", RUNNING: "ĐANG CHẠY", DONE: "ĐÃ XONG", FAILED: "THẤT BẠI", CANCELLED: "ĐÃ HUỶ", INTERRUPTED: "BỊ GIÁN ĐOẠN" };
  DOM.taskActivityTitle.textContent = TASK_ACTION_LABELS[task.action] || task.action || "Đang xử lý";
  DOM.taskActivityStage.textContent = task.error_message || task.stage || "Đang chuẩn bị…";
  DOM.taskActivityState.textContent = stateLabels[task.state] || task.state || "ĐANG CHỜ";
  DOM.taskActivityState.className = `task-state ${(task.state || "").toLowerCase()}`;
  DOM.taskActivityBar.style.width = `${pct}%`;
  DOM.taskActivityPercent.textContent = `${Math.round(pct)}%`;
  DOM.taskStartedAt.textContent = formatClock(task.started_at || task.created_at);
  DOM.taskElapsed.textContent = formatSpan(task.elapsed_seconds || 0);
  DOM.taskEta.textContent = task.state === "DONE" ? "Đã hoàn tất" : (task.state === "FAILED" || task.state === "CANCELLED" || task.state === "INTERRUPTED" ? "—" : formatSpan(task.eta_seconds));
  DOM.taskFinishAt.textContent = task.finished_at ? formatClock(task.finished_at) : formatClock(task.estimated_finish_at);
  if (task.action === 'voiceover' && task.state === 'RUNNING' && pct < 70) {
    DOM.taskEta.textContent = 'Đang đo; phụ thuộc phản hồi dịch vụ';
    DOM.taskFinishAt.textContent = 'Chưa xác định';
  }
  if (task.action === 'render' && task.state === 'RUNNING' && pct >= 94) {
    DOM.taskEta.textContent = 'Đang kiểm tra AAC; chưa đủ dữ liệu ước tính';
    DOM.taskFinishAt.textContent = 'Chưa xác định';
  }

  const events = Array.isArray(task.events) ? task.events.slice(-12) : [];
  DOM.taskHistory.innerHTML = events.length
    ? events.map((event) => `<div class="task-event"><time>${escapeHtml(formatClock(event.timestamp))}</time><b>${Number(event.progress) || 0}%</b><span>${escapeHtml(event.stage || event.state || "Cập nhật")}</span></div>`).join("")
    : `<div class="task-event"><time>${escapeHtml(formatClock(task.created_at))}</time><b>${Math.round(pct)}%</b><span>${escapeHtml(task.stage || "Đang chuẩn bị")}</span></div>`;
  DOM.taskHistory.scrollTop = DOM.taskHistory.scrollHeight;
}

async function waitForTask(task, onProgress) {
  if (!task || !task.task_id) return task;
  let current = task;
  renderTaskActivity(current);
  while (current.state === "QUEUED" || current.state === "RUNNING") {
    if (onProgress && typeof onProgress === "function") {
      onProgress(current.progress || 0, current.stage || current.state);
    }
    await new Promise((r) => setTimeout(r, 1000));
    try {
      current = await API.getTask(task.task_id);
      renderTaskActivity(current);
    } catch (err) {
      throw new Error(`Không đọc được trạng thái tác vụ. Tác vụ có thể vẫn đang chạy; hãy tải lại trang để kiểm tra. ${err.message}`);
    }
  }
  renderTaskActivity(current);
  if(current.state==='INTERRUPTED')window.dispatchEvent(new CustomEvent('workspace-task-interrupted',{detail:current.project_id}));
  if (current.state === "FAILED" || current.state === "INTERRUPTED") {
    const err = new Error(current.error_message || "Tác vụ thất bại");
    err.code = current.error_code;
    throw err;
  }
  if (current.state === "CANCELLED") {
    const err = new Error("Tác vụ đã bị huỷ");
    err.code = "CANCELLED";
    throw err;
  }
  return current;
}

// Global App State
let currentProjectId = null;
let currentProject = null;
let currentDuration = 0;
let pollTimer = null;
let currentROI = null; // { ymin, ymax, xmin, xmax }
let currentCues = [];
let selectedCueIndices = new Set();
let cueAudioPlayer = null;
let currentWorkflow = null;
let lastHydratedStage = null;

// =============================================================================
// DOM Elements Cache
// =============================================================================
const DOM = {
  // Views
  views: {
    dashboard: document.getElementById("view-dashboard"),
    new: document.getElementById("view-new"),
    detail: document.getElementById("view-detail"),
  },
  tabs: document.querySelectorAll(".sidebar-nav .tab[data-view]"),

  // Dashboard
  filterStatus: document.getElementById("filter-status"),
  btnRefresh: document.getElementById("btn-refresh"),
  projectRows: document.getElementById("project-rows"),
  statTotal: document.getElementById("stat-total"),
  statVerified: document.getElementById("stat-verified"),
  statProcessing: document.getElementById("stat-processing"),
  topbarHealth: document.getElementById("topbar-health"),
  healthText: document.getElementById("health-text"),

  // New Project
  formNew: document.getElementById("form-new"),
  inputUrl: document.getElementById("input-url"),
  inputBatchUrls: document.getElementById("input-batch-urls"),
  boxSingleUrl: document.getElementById("box-single-url"),
  boxBatchUrls: document.getElementById("box-batch-urls"),
  btnModeSingle: document.getElementById("btn-mode-single"),
  btnModeBatch: document.getElementById("btn-mode-batch"),
  inputTrimStart: document.getElementById("input-trim-start"),
  inputTrimEnd: document.getElementById("input-trim-end"),
  newResult: document.getElementById("new-result"),

  // Detail View
  btnBack: document.getElementById("btn-back"),
  detailTitle: document.getElementById("detail-title"),
  detailStage: document.getElementById("detail-stage"),
  detailMetaPill: document.getElementById("detail-meta-pill"),
  btnCancel: document.getElementById("btn-cancel"),
  btnDelete: document.getElementById("btn-delete"),
  detailBar: document.getElementById("detail-bar"),
  detailEta: document.getElementById("detail-eta"),
  detailError: document.getElementById("detail-error"),
  nextActionIndex: document.getElementById("next-action-index"),
  nextActionTitle: document.getElementById("next-action-title"),
  nextActionDesc: document.getElementById("next-action-desc"),
  btnNextAction: document.getElementById("btn-next-action"),
  taskActivity: document.getElementById("task-activity"),
  taskActivityTitle: document.getElementById("task-activity-title"),
  taskActivityStage: document.getElementById("task-activity-stage"),
  taskActivityState: document.getElementById("task-activity-state"),
  taskActivityBar: document.getElementById("task-activity-bar"),
  taskActivityPercent: document.getElementById("task-activity-percent"),
  taskStartedAt: document.getElementById("task-started-at"),
  taskElapsed: document.getElementById("task-elapsed"),
  taskEta: document.getElementById("task-eta"),
  taskFinishAt: document.getElementById("task-finish-at"),
  taskHistory: document.getElementById("task-history"),
  videoStatusBadge: document.getElementById("video-status-badge"),

  // Video & Canvas ROI
  detailVideo: document.getElementById("detail-video"),
  btnWatchOriginal: document.getElementById("btn-watch-original"),
  btnWatchDubbed: document.getElementById("btn-watch-dubbed"),
  videoSourceLabel: document.getElementById("video-source-label"),
  roiCanvas: document.getElementById("roi-canvas"),
  previewEmpty: document.getElementById("preview-empty"),
  roiContainer: document.getElementById("roi-container"),
  roiVideoWrapper: document.getElementById("roi-video-wrapper"),
  roiCoordsBadge: document.getElementById("roi-coords-badge"),
  btnRoiAutoDetect: document.getElementById("btn-roi-auto-detect"),
  btnRoiPresetBottom: document.getElementById("btn-roi-preset-bottom"),
  btnRoiClear: document.getElementById("btn-roi-clear"),
  btnRoiSave: document.getElementById("btn-roi-save"),
  btnRoiTest30s: document.getElementById("btn-roi-test-30s"),
  roiResult: document.getElementById("roi-result"),
  roiTimeDisplay: document.getElementById("roi-time-display"),

  // Step seek buttons
  btnSeekBack5: document.getElementById("btn-seek-back-5"),
  btnSeekBack1: document.getElementById("btn-seek-back-1"),
  btnSeekFwd1: document.getElementById("btn-seek-fwd-1"),
  btnSeekFwd5: document.getElementById("btn-seek-fwd-5"),
  btnCaptureFrame: document.getElementById("btn-capture-frame"),

  // Render preview
  btnRenderPreview: document.getElementById("btn-render-preview"),
  renderPreviewWrap: document.getElementById("render-preview-wrap"),
  renderPreviewImg: document.getElementById("render-preview-img"),
  renderPreviewNote: document.getElementById("render-preview-note"),
  cardRenderPreview: document.getElementById("card-render-preview"),

  // Audio Mixer
  audioPreview: document.getElementById("audio-preview"),
  volVoice: document.getElementById("vol-voice"),
  volVoiceVal: document.getElementById("vol-voice-val"),
  volOriginal: document.getElementById("vol-original"),
  volOriginalVal: document.getElementById("vol-original-val"),
  btnRemix: document.getElementById("btn-remix"),
  remixResult: document.getElementById("remix-result"),
  cardAudioMix: document.getElementById("card-audio-mix"),

  // Studio tabs & panels
  studioTabs: document.querySelectorAll(".studio-tab"),
  studioContents: document.querySelectorAll(".studio-content"),

  // Step cards (Workflow tab)
  btnRunAll: document.getElementById("btn-run-all"),
  btnStepSource: document.getElementById("btn-step-source"),
  btnStepDownload: document.getElementById("btn-step-download"),
  step2StatusBox: document.getElementById("step-2-status-box"),
  step2StatusText: document.getElementById("step-2-status-text"),
  step2StatusPct: document.getElementById("step-2-status-pct"),
  step2StatusBar: document.getElementById("step-2-status-bar"),
  step2StatusDetail: document.getElementById("step-2-status-detail"),
  btnFocusRoi: document.getElementById("btn-focus-roi"),
  btnStepOcr: document.getElementById("btn-step-ocr"),
  btnStepOcrCancel: document.getElementById("btn-step-ocr-cancel"),
  btnStepOcrRerun: document.getElementById("btn-step-ocr-rerun"),
  step4StatusBox: document.getElementById("step-4-status-box"),
  step4StatusText: document.getElementById("step-4-status-text"),
  step4StatusPct: document.getElementById("step-4-status-pct"),
  step4StatusBar: document.getElementById("step-4-status-bar"),
  step4StatusDetail: document.getElementById("step-4-status-detail"),
  step4StatusCueCount: document.getElementById("step-4-status-cue-count"),
  roiSampleBox: document.getElementById("roi-sample-box"),
  btnCloseRoiSample: document.getElementById("btn-close-roi-sample"),
  roiSampleCuesContent: document.getElementById("roi-sample-cues-content"),
  roiSampleTip: document.getElementById("roi-sample-tip"),
  btnStepTranslate: document.getElementById("btn-step-translate"),
  btnSwitchToSub: document.getElementById("btn-switch-to-sub"),
  btnStepVoiceover: document.getElementById("btn-step-voiceover"),
  btnStepRender: document.getElementById("btn-step-render"),
  btnStepVerify: document.getElementById("btn-step-verify"),

  // OCR Tab
  cuesList: document.getElementById("cues-list"),
  ocrSummaryText: document.getElementById("ocr-summary-text"),
  btnOcrRunTab: document.getElementById("btn-ocr-run-tab"),
  btnApproveOcr: document.getElementById("btn-approve-ocr"),

  // Translation Tab
  transList: document.getElementById("trans-list"),
  transSummaryText: document.getElementById("trans-summary-text"),
  transTone: document.getElementById("trans-tone"),
  transKeyStatus: document.getElementById("trans-key-status"),
  btnTransRunTab: document.getElementById("btn-trans-run-tab"),
  btnApproveTrans: document.getElementById("btn-approve-trans"),
  transFindInput: document.getElementById("trans-find-input"),
  transReplaceInput: document.getElementById("trans-replace-input"),
  transMatchCase: document.getElementById("trans-match-case"),
  btnTransFindReplace: document.getElementById("btn-trans-find-replace"),
  btnRetranslateSelected: document.getElementById("btn-retranslate-selected"),
  selectedCuesCount: document.getElementById("selected-cues-count"),
  cbSelectAllCues: document.getElementById("cb-select-all-cues"),

  // Subtitle Tab
  subMode: document.getElementById("sub-mode"),
  coverBoxColor: document.getElementById("cover-box-color"),
  coverBoxOpacity: document.getElementById("cover-box-opacity"),
  coverBoxOpacityVal: document.getElementById("cover-box-opacity-val"),
  subFontSize: document.getElementById("sub-font-size"),
  subFontSizeVal: document.getElementById("sub-font-size-val"),
  subMarginV: document.getElementById("sub-margin-v"),
  subMarginVVal: document.getElementById("sub-margin-v-val"),
  btnSaveSubSettings: document.getElementById("btn-save-sub-settings"),
  btnPreviewSubStyle: document.getElementById("btn-preview-sub-style"),
  subSettingsResult: document.getElementById("sub-settings-result"),

  // Voice & Render Tab
  voicePick: document.getElementById("voice-pick"),
  voiceRate: document.getElementById("voice-rate"),
  voiceRateVal: document.getElementById("voice-rate-val"),
  inputNormTest: document.getElementById("input-norm-test"),
  btnNormTest: document.getElementById("btn-norm-test"),
  normTestOutput: document.getElementById("norm-test-output"),
  btnVoiceRunTab: document.getElementById("btn-voice-run-tab"),
  voiceTabResult: document.getElementById("voice-tab-result"),
  voiceCuesList: document.getElementById("voice-cues-list"),
  voiceSummaryText: document.getElementById("voice-summary-text"),
  renderPreset: document.getElementById("render-preset"),
  btnRenderRunTab: document.getElementById("btn-render-run-tab"),
  renderTabResult: document.getElementById("render-tab-result"),

  // QA Verification Tab
  btnVerifyRunTab: document.getElementById("btn-verify-run-tab"),
  qaMetricDuration: document.getElementById("qa-metric-duration"),
  qaMetricDrift: document.getElementById("qa-metric-drift"),
  qaMetricCoverage: document.getElementById("qa-metric-coverage"),
  qaMetricDegraded: document.getElementById("qa-metric-degraded"),
  linkDownloadVideo: document.getElementById("link-download-video"),
  linkDownloadViSrt: document.getElementById("link-download-vi-srt"),
  linkDownloadSourceSrt: document.getElementById("link-download-source-srt"),
  linkDownloadTimeline: document.getElementById("link-download-timeline"),
  linkDownloadVerify: document.getElementById("link-download-verify"),

  // Logs Tab
  detailMeta: document.getElementById("detail-meta"),
  detailLogs: document.getElementById("detail-logs"),

  // Keys Modal
  btnOpenKeys: document.getElementById("btn-open-keys"),
  modalKeys: document.getElementById("modal-keys"),
  btnCloseKeys: document.getElementById("btn-close-keys"),
  inputAddKey: document.getElementById("input-add-key"),
  btnSubmitKey: document.getElementById("btn-submit-key"),
  keyAddResult: document.getElementById("key-add-result"),
  keysListContainer: document.getElementById("keys-list-container"),
};

// =============================================================================
// Navigation & Views
// =============================================================================
const workspaceLocationKey = 'duanlaylink_workspace_location_v1';
function rememberWorkspace(name) {
  try {
    sessionStorage.setItem(workspaceLocationKey, JSON.stringify({
      view: name, projectId: name === 'detail' ? currentProjectId : null,
    }));
  } catch {}
}

async function restoreWorkspace() {
  if (typeof window !== 'undefined' && window.workspaceServerReady) await window.workspaceServerReady;
  let saved;
  try { saved = JSON.parse(sessionStorage.getItem(workspaceLocationKey)); } catch {}
  if (saved?.view === 'detail' && /^[A-Za-z0-9_-]{1,64}$/.test(saved.projectId || '')) {
    try {
      await openProject(saved.projectId);
    } catch (err) {
      if (currentProjectId !== saved.projectId) return;
      stopPolling();
      if (err.code === 'HTTP_404') {
        currentProjectId = null;
        currentProject = null;
        showView('dashboard');
      } else {
        DOM.detailError.textContent = 'Không mở lại được dự án: ' + err.message;
      }
    }
    return;
  }
  showView(saved?.view === 'new' ? 'new' : 'dashboard');
}

function showView(name, resumeMonitoring = true) {
  rememberWorkspace(name);
  Object.keys(DOM.views).forEach((k) => {
    DOM.views[k].classList.toggle("active", k === name);
  });
  DOM.tabs.forEach((t) => {
    t.classList.toggle("active", t.dataset.view === name);
  });
  if (name === "dashboard") {
    loadProjects();
    stopPolling();
  }
  if (name === "detail" && currentProjectId && resumeMonitoring) {
    refreshProjectData().catch(() => {});
    startPolling();
  }
  const labels = {dashboard:['Tổng quan','Theo dõi video, tài nguyên và tiến độ xử lý.'],new:['Dự án mới','Bắt đầu với liên kết hoặc video trên máy.'],detail:['Studio video','Xử lý, chỉnh sửa và duyệt từng công đoạn.']};
  if (labels[name]) {
    document.getElementById('page-label').textContent=labels[name][0];
    document.getElementById('page-description').textContent=labels[name][1];
  }
  if(name==='detail') document.getElementById('sidebar-studio').disabled=false;
  document.getElementById('sidebar-studio').classList.toggle('active',name==='detail');
  window.dispatchEvent(new CustomEvent('workspace-view',{detail:name}));
}

DOM.tabs.forEach((tab) => {
  tab.addEventListener("click", () => showView(tab.dataset.view));
});

DOM.btnBack.addEventListener("click", () => showView("dashboard"));

document.getElementById("nav-brand").addEventListener("click", () => showView("dashboard"));

// Appearance is managed by shell.js with the shared mint/light-dark palette.

// Studio Tabs Switching
DOM.studioTabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    const target = tab.dataset.studio;
    switchStudioTab(target);
  });
});

function switchStudioTab(name) {
  DOM.studioTabs.forEach((t) => t.classList.toggle("active", t.dataset.studio === name));
  DOM.studioContents.forEach((c) => {
    c.classList.toggle("active", c.id === `studio-${name}`);
  });
  if (name === 'voice') loadVoiceTiming();
}

// Stepper Node click jump to tabs
document.querySelectorAll(".pipeline-stepper .step-node").forEach((node) => {
  node.addEventListener("click", () => {
    navigateToStep(parseInt(node.dataset.step, 10));
  });
});

function navigateToStep(step) {
  if (step === 1) {
    switchStudioTab("workflow");
    const card = document.getElementById("card-step-1");
    if (card) card.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "center" });
  } else if (step === 2) {
    switchStudioTab("workflow");
    const card = document.getElementById("card-step-2");
    if (card) {
      card.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "center" });
      if (DOM.btnStepDownload && !DOM.btnStepDownload.disabled) {
        DOM.btnStepDownload.style.boxShadow = "0 0 0 4px var(--primary)";
        setTimeout(() => { DOM.btnStepDownload.style.boxShadow = ""; }, 1800);
      }
    }
  } else if (step === 3) {
    DOM.detailVideo.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "center" });
  } else if (step === 4) {
    switchStudioTab("ocr");
  } else if (step === 5) {
    switchStudioTab("trans");
  } else if (step === 6) {
    switchStudioTab("sub");
  } else if (step === 7) {
    switchStudioTab("voice");
  } else if (step === 8) {
    switchStudioTab("render");
  } else {
    switchStudioTab("verify");
  }
}

DOM.btnNextAction.addEventListener("click", () => {
  navigateToStep(parseInt(DOM.btnNextAction.dataset.targetStep || "1", 10));
});

// =============================================================================
// Dashboard View
// =============================================================================
let projectListGeneration = 0;
async function loadProjects() {
  const generation = ++projectListGeneration;
  const filter = DOM.filterStatus.value;
  try {
    const projects = await API.listProjects(filter);
    if (generation !== projectListGeneration) return false;
    renderProjects(projects || []);
    window.dispatchEvent(new CustomEvent('workspace-projects-loaded'));
    return true;
  } catch (err) {
    if (generation !== projectListGeneration) return false;
    DOM.projectRows.innerHTML = `<tr><td colspan="7" class="error" style="text-align: center; padding: 24px;">Lỗi tải dự án: ${escapeHtml(err.message)}</td></tr>`;
  }
}

DOM.btnRefresh.addEventListener("click", async () => {
  if (DOM.btnRefresh.disabled) return;
  DOM.btnRefresh.disabled = true;
  DOM.btnRefresh.setAttribute('aria-busy', 'true');
  try {
    const projectsOK = await loadProjects();
    const overviewOK = await window.refreshWorkspaceOverview();
    window.workspaceToast(projectsOK && overviewOK ? 'Đã làm mới dữ liệu' : 'Chưa làm mới được đầy đủ dữ liệu. Xem thông báo tại danh sách và tổng quan, rồi thử lại.', !(projectsOK && overviewOK));
  } finally {
    DOM.btnRefresh.disabled = false;
    DOM.btnRefresh.removeAttribute('aria-busy');
  }
});
DOM.filterStatus.addEventListener("change", loadProjects);

function renderProjects(projects) {
  document.getElementById('project-list-count').textContent=`${projects.length} dự án trong danh sách`;

  if (projects.length === 0) {
    DOM.projectRows.innerHTML = `<tr><td colspan="7" class="muted" style="text-align: center; padding: 36px;">${DOM.filterStatus.value ? 'Không có dự án khớp trạng thái đã chọn. Hãy chọn Tất cả trạng thái.' : 'Chưa có dự án nào. Bấm <b>Dự án mới</b> để bắt đầu.'}</td></tr>`;
    return;
  }

  DOM.projectRows.innerHTML = projects
    .map((p) => {
      const stageText = p.stage || p.status || "CREATED";
      const progress = p.progress !== undefined ? p.progress : 0;
      const isComplete = stageText === "VERIFIED";
      const badgeClass = isComplete ? "badge-success" : p.status === "FAILED" ? "badge-danger" : "badge";
      const platform = p.platform || "mp4";

      return `
        <tr data-id="${escapeHtml(p.id)}">
          <td><code style="font-size: 12px;">${escapeHtml(p.id)}</code></td>
          <td>
            <div style="font-weight: 600; color: var(--text-main); max-width: 380px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
              ${escapeHtml(p.title || "Dự án Fast Hardsub")}
            </div>
            <div class="muted" style="font-size: 11px;">${p.created_at ? new Date(/[zZ]|[+-]\d{2}:\d{2}$/.test(p.created_at) ? p.created_at : p.created_at + "Z").toLocaleString("vi-VN") : ""}</div>
          </td>
          <td style="text-align: center;">
            <span class="badge badge-platform ${platform.toLowerCase()}">${escapeHtml(platform.toUpperCase())}</span>
          </td>
          <td>
            <span class="${badgeClass}">${escapeHtml(stageText)}</span>
          </td>
          <td>
            <div class="table-progress">
              <div class="table-progress-bar" style="width: ${progress}%;"></div>
            </div>
            <span style="font-size: 11px; font-family: var(--font-mono); color: var(--text-dim);">${progress}%</span>
          </td>
          <td style="text-align: center;">
            <button class="btn small primary btn-open-project" data-id="${escapeHtml(p.id)}">Mở Studio</button>
          </td>
          <td style="text-align: center;">
            <button class="btn link small btn-delete-project" data-id="${escapeHtml(p.id)}" title="Xoá dự án" style="color: var(--danger); font-size: 14px;">✕</button>
          </td>
        </tr>
      `;
    })
    .join("");

  document.querySelectorAll(".btn-open-project").forEach((btn) => {
    btn.addEventListener("click", () => openProject(btn.dataset.id));
  });

  document.querySelectorAll(".btn-delete-project").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const id = btn.dataset.id;
      if (confirm(`Bạn có chắc chắn muốn xoá dự án ${id}? Dữ liệu sẽ không thể khôi phục.`)) {
        try {
          await API.deleteProject(id);
          loadProjects();
        } catch (err) {
          alert("Lỗi xoá dự án: " + err.message);
        }
      }
    });
  });
}

// System Health Poller
async function checkHealth() {
  try {
    const health = await API.systemHealth();
    if (health.ready && health.ffmpeg && health.ffmpeg.nvenc) {
      DOM.healthText.textContent = `VSF + RapidOCR + NVENC: Sẵn sàng`;
      DOM.topbarHealth.style.borderColor = "var(--ok)";
    } else {
      const missing = [];
      if (!health.videosubfinder || !health.videosubfinder.available) missing.push("VSF");
      if (!health.rapidocr || !health.rapidocr.available) missing.push("RapidOCR");
      DOM.healthText.textContent = missing.length ? `Thiếu: ${missing.join(", ")}` : "CPU render: Sẵn sàng";
      DOM.topbarHealth.style.borderColor = missing.length ? "var(--warning)" : "var(--border-subtle)";
    }
  } catch {
    DOM.healthText.textContent = "Hệ thống: Offline";
    DOM.topbarHealth.style.borderColor = "var(--danger)";
  }
}
setInterval(checkHealth, 15000);
checkHealth();

// =============================================================================
// New Project View
// =============================================================================
DOM.btnModeSingle.addEventListener("click", () => {
  DOM.btnModeSingle.classList.add("active");
  DOM.btnModeBatch.classList.remove("active");
  DOM.boxSingleUrl.style.display = "block";
  DOM.boxBatchUrls.style.display = "none";
});

DOM.btnModeBatch.addEventListener("click", () => {
  DOM.btnModeBatch.classList.add("active");
  DOM.btnModeSingle.classList.remove("active");
  DOM.boxSingleUrl.style.display = "none";
  DOM.boxBatchUrls.style.display = "block";
});

DOM.formNew.addEventListener("submit", async (e) => {
  e.preventDefault();
  DOM.newResult.textContent = "Đang khởi tạo...";
  DOM.newResult.className = "result";

  const isBatch = DOM.btnModeBatch.classList.contains("active");
  const trimStart = DOM.inputTrimStart.value.trim();
  const trimEnd = DOM.inputTrimEnd.value.trim();

  try {
    if (isBatch) {
      const raw = DOM.inputBatchUrls.value.trim();
      const urls = raw.split("\n").map((u) => u.trim()).filter(Boolean);
      if (urls.length === 0) throw new Error("Vui lòng nhập ít nhất một URL");
      const res = await API.createBatch(urls, trimStart, trimEnd);
      DOM.newResult.textContent = `Đã xếp ${res.created ? res.created.length : 0} dự án vào hàng đợi!`;
      DOM.newResult.classList.add("ok");
      setTimeout(() => showView("dashboard"), 1200);
    } else {
      const url = DOM.inputUrl.value.trim();
      if (!url) throw new Error("Vui lòng nhập URL hoặc đường dẫn video");
      const project = await API.createProject(url, trimStart, trimEnd);
      DOM.newResult.textContent = `Tạo thành công dự án ${project.project_id}!`;
      DOM.newResult.classList.add("ok");
      await openProject(project.project_id);
    }
  } catch (err) {
    DOM.newResult.textContent = err.message;
    DOM.newResult.classList.add("err");
  }
});

// =============================================================================
// Detail View (Studio) & Poller
// =============================================================================
async function openProject(projectId) {
  currentProjectId = projectId;
  window.dispatchEvent(new CustomEvent('workspace-project-open', {detail: projectId}));
  ocrReviewFilter = 'all';
  cuePages.ocr = 0;
  delete DOM.detailVideo.dataset.selectedSource;
  delete DOM.detailVideo.dataset.renderVersion;
  showView("detail", false);
  DOM.detailTitle.textContent = `Dự án ${projectId}`;
  DOM.detailError.textContent = "";
  DOM.detailBar.style.width = "0%";
  DOM.previewEmpty.style.display = "block";
  DOM.detailVideo.style.display = "none";
  DOM.roiCanvas.style.display = "none";
  currentROI = null;
  currentCues = [];
  currentWorkflow = null;
  lastHydratedStage = null;

  await refreshProjectData();
  await loadSubtitleSettings();
  startPolling();
}

function startPolling() {
  stopPolling();
  pollTimer = setInterval(async () => {
    if (!currentProjectId) return;
    try {
      await refreshProjectData();
    } catch {}
  }, 2500);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function refreshProjectData() {
  if (!currentProjectId) return;
  const project = await API.getProject(currentProjectId);
  currentProject = project;
  currentWorkflow = project.workflow || null;

  DOM.detailTitle.textContent = project.title || `Dự án ${project.id}`;
  const stage = project.stage || project.status || "CREATED";
  DOM.detailStage.textContent = stage;
  const platform = (project.source && project.source.platform) || project.platform || "video";
  const duration = (project.media && project.media.duration) || (project.source && project.source.duration) || 0;
  DOM.detailMetaPill.textContent = `${platform.toUpperCase()} • ${duration ? formatDuration(duration) : "Chưa rõ thời lượng"}`;

  if(project.task?.state==='INTERRUPTED')window.dispatchEvent(new CustomEvent('workspace-task-interrupted',{detail:currentProjectId}));
  const activeTask = project.task && ["QUEUED", "RUNNING"].includes(project.task.state) ? project.task : null;
  if (project.task) renderTaskActivity(project.task);
  else if (DOM.taskActivity) {
    DOM.taskActivity.hidden = true;
    delete DOM.taskActivity.dataset.state;
  }
  const progress = activeTask ? (activeTask.progress || 0) : (project.progress !== undefined ? project.progress : 0);
  DOM.detailBar.style.width = `${progress}%`;

  const jobError = project.job && ["FAILED", "CANCELLED", "INTERRUPTED"].includes(project.job.status)
    ? project.job.error_message
    : "";
  const errorMessage = activeTask ? (activeTask.error_message || "") : jobError;
  if (errorMessage) {
    const interrupted = /gián đoạn|interrupted/i.test(errorMessage);
    DOM.detailError.className = interrupted ? "warn" : "error";
    DOM.detailError.textContent = interrupted
      ? "⚠ Tác vụ trước đã dừng khi ứng dụng khởi động lại. Dữ liệu các bước hoàn tất vẫn được giữ; bạn có thể tiếp tục từ bước đang sáng."
      : `⚠️ Lỗi: ${errorMessage}`;
  } else {
    DOM.detailError.className = "error";
    DOM.detailError.textContent = "";
  }

  updateWorkflowUI(stage, activeTask);
  updateVideoSource(project);
  if (lastHydratedStage !== stage) {
    if (stageRank(stage) >= stageRank("MEDIA_READY")) await loadROI();
    if (stageRank(stage) >= stageRank("OCR_READY")) await loadCues();
    lastHydratedStage = stage;
  }
  updateLogs(project);
}

const STAGE_ORDER = [
  "CREATED",
  "SOURCE_READY",
  "MEDIA_READY",
  "ROI_READY",
  "OCR_READY",
  "SOURCE_REVIEWED",
  "TRANSLATED",
  "TRANSLATION_REVIEWED",
  "VOICE_READY",
  "RENDERED",
  "VERIFIED"
];

function stageToStepNumber(stage) {
  if (!stage) return 1;
  if (stage === "CREATED") return 1;
  if (stage === "SOURCE_READY") return 2;
  if (stage === "MEDIA_READY") return document.getElementById('extraction-mode')?.value === 'hardsub' ? 3 : 4;
  if (stage === "ROI_READY") return 4;
  if (stage === "OCR_READY") return 4;
  if (stage === "SOURCE_REVIEWED" || stage === "TRANSLATED") return 5;
  if (stage === "TRANSLATION_REVIEWED") {
    return subtitleConfigured() ? (document.getElementById('audio-mode')?.value === 'original_only' ? 8 : 7) : 6;
  }
  if (stage === "VOICE_READY") return 8;
  if (stage === "RENDERED") return 9;
  if (stage === "VERIFIED") return 9;
  return 1;
}

function stageRank(stage) {
  const idx = STAGE_ORDER.indexOf(stage);
  return idx < 0 ? 0 : idx;
}

function subtitleConfigured() {
  return !!(currentWorkflow && currentWorkflow.stages &&
    currentWorkflow.stages.SUBTITLE_CONFIGURED &&
    currentWorkflow.stages.SUBTITLE_CONFIGURED.status === "COMPLETED");
}

function isStepComplete(step, stage) {
  const rank = stageRank(stage);
  if (step === 1) return rank >= stageRank("SOURCE_READY");
  if (step === 2) return rank >= stageRank("MEDIA_READY");
  if (step === 3) return rank >= stageRank("ROI_READY") || (rank >= stageRank("MEDIA_READY") && document.getElementById('extraction-mode')?.value !== 'hardsub');
  if (step === 4) return rank >= stageRank("SOURCE_REVIEWED");
  if (step === 5) return rank >= stageRank("TRANSLATION_REVIEWED");
  if (step === 6) return subtitleConfigured();
  if (step === 7) return rank >= stageRank("VOICE_READY") || (document.getElementById('audio-mode')?.value === 'original_only' && rank >= stageRank('TRANSLATION_REVIEWED') && subtitleConfigured());
  if (step === 8) return rank >= stageRank("RENDERED");
  return rank >= stageRank("VERIFIED");
}

function setEnabled(element, enabled, reason = "") {
  if (!element) return;
  element.disabled = !enabled;
  element.title = enabled ? "" : reason;
}

function updateWorkflowUI(stage, activeTask) {
  updateStepper(stage);
  const busy = !!activeTask;
  const rank = stageRank(stage);
  const step = stageToStepNumber(stage);
  const voiceRequired = document.getElementById('audio-mode')?.value !== 'original_only';

  document.querySelectorAll(".step-card[data-workflow-step]").forEach((card) => {
    const cardStep = parseInt(card.dataset.workflowStep, 10);
    card.classList.toggle("completed", isStepComplete(cardStep, stage));
    card.classList.toggle("active", cardStep === step && !isStepComplete(cardStep, stage));
    card.classList.toggle("locked", cardStep > step && !isStepComplete(cardStep, stage));
    let badge = card.querySelector(".step-state-badge");
    if (!badge) {
      badge = document.createElement("span");
      badge.className = "step-state-badge";
      card.querySelector(".step-card-title").appendChild(badge);
    }
    badge.textContent = isStepComplete(cardStep, stage) ? "Đã xong" : (cardStep === step ? "Đang chờ bạn" : "Chưa mở");
  });

  const reason = "Hãy hoàn thành bước trước trước khi dùng chức năng này";
  setEnabled(DOM.btnStepSource, !busy, "Đang có tác vụ khác chạy");
  setEnabled(DOM.btnStepDownload, !busy && rank >= stageRank("SOURCE_READY"), reason);
  setEnabled(DOM.btnFocusRoi, !busy && rank >= stageRank("MEDIA_READY"), reason);
  setEnabled(DOM.btnRoiPresetBottom, !busy && rank >= stageRank("MEDIA_READY"), reason);
  setEnabled(DOM.btnRoiClear, !busy && rank >= stageRank("MEDIA_READY"), reason);
  setEnabled(DOM.btnRoiSave, !busy && rank >= stageRank("MEDIA_READY"), reason);
  setEnabled(DOM.btnRoiAutoDetect, !busy && rank >= stageRank("MEDIA_READY"), reason);
  setEnabled(DOM.btnRoiTest30s, !busy && rank >= stageRank("ROI_READY"), reason);
  const extractionMode = document.getElementById("extraction-mode")?.value || "hardsub";
  const audioOnly = extractionMode === 'stt';
  for (const button of [DOM.btnFocusRoi, DOM.btnRoiPresetBottom, DOM.btnRoiClear, DOM.btnRoiSave, DOM.btnRoiTest30s, DOM.btnRoiAutoDetect]) {
    if (audioOnly && button) { button.disabled = true; button.title = 'Nhận dạng âm thanh không cần vùng ROI'; }
  }
  setEnabled(document.getElementById('btn-no-subtitles'), !busy && rank >= stageRank('MEDIA_READY'), reason);
  const extractionReady = rank >= stageRank(extractionMode === "hardsub" ? "ROI_READY" : "MEDIA_READY");
  setEnabled(DOM.btnStepOcr, !busy && extractionReady, reason);
  setEnabled(DOM.btnOcrRunTab, !busy && extractionReady, reason);
  setEnabled(DOM.btnApproveOcr, !busy && rank >= stageRank("OCR_READY"), reason);
  setEnabled(DOM.btnStepTranslate, !busy && rank >= stageRank("SOURCE_REVIEWED"), reason);
  setEnabled(DOM.btnTransRunTab, !busy && rank >= stageRank("SOURCE_REVIEWED"), reason);
  setEnabled(DOM.btnApproveTrans, !busy && rank >= stageRank("TRANSLATED"), reason);
  setEnabled(DOM.btnSaveSubSettings, !busy && rank >= stageRank("TRANSLATION_REVIEWED"), reason);
  setEnabled(DOM.btnPreviewSubStyle, !busy && rank >= stageRank("TRANSLATION_REVIEWED"), reason);
  setEnabled(DOM.btnRenderPreview, !busy && rank >= stageRank("TRANSLATION_REVIEWED"), reason);
  setEnabled(DOM.btnStepVoiceover, !busy && voiceRequired && subtitleConfigured(), voiceRequired ? "Hãy lưu cấu hình phụ đề ở Bước 6" : 'Đang giữ audio gốc; không cần TTS');
  setEnabled(DOM.btnVoiceRunTab, !busy && voiceRequired && subtitleConfigured(), voiceRequired ? "Hãy lưu cấu hình phụ đề ở Bước 6" : 'Đang giữ audio gốc; không cần TTS');
  setEnabled(DOM.btnRemix, !busy && rank >= stageRank("VOICE_READY"), reason);
  const renderReady = rank >= stageRank(voiceRequired ? 'VOICE_READY' : 'TRANSLATION_REVIEWED');
  setEnabled(DOM.btnStepRender, !busy && renderReady, reason);
  setEnabled(DOM.btnRenderRunTab, !busy && renderReady, reason);
  setEnabled(DOM.btnStepVerify, !busy && rank >= stageRank("RENDERED"), reason);
  setEnabled(DOM.btnVerifyRunTab, !busy && rank >= stageRank("RENDERED"), reason);
  setEnabled(DOM.btnRunAll, !busy && extractionReady, "Hãy tải video và chọn chế độ bóc phụ đề");
  DOM.btnCancel.disabled = !busy;
  DOM.cardRenderPreview.hidden = rank < stageRank("TRANSLATION_REVIEWED");
  DOM.cardAudioMix.hidden = rank < stageRank("VOICE_READY");

  // Step 2 Button & Live Box Dynamic State
  if (rank >= stageRank("MEDIA_READY")) {
    if (DOM.btnStepDownload) {
      DOM.btnStepDownload.textContent = "✔ Đã Tải Xong";
      DOM.btnStepDownload.classList.remove("primary");
    }
    if (DOM.step2StatusBox) {
      DOM.step2StatusBox.style.display = "block";
      DOM.step2StatusText.textContent = "✔ Video 1080p đã tải xong và sẵn sàng trên khung xem bên trái.";
      DOM.step2StatusPct.textContent = "100%";
      DOM.step2StatusBar.style.width = "100%";
    }
  } else if (activeTask && activeTask.action === "download") {
    if (DOM.btnStepDownload) {
      DOM.btnStepDownload.textContent = `⏳ Đang tải ${activeTask.progress || 0}%...`;
      DOM.btnStepDownload.disabled = true;
    }
    if (DOM.step2StatusBox) {
      DOM.step2StatusBox.style.display = "block";
      DOM.step2StatusText.textContent = activeTask.stage || "Đang tải video...";
      DOM.step2StatusPct.textContent = `${activeTask.progress || 0}%`;
      DOM.step2StatusBar.style.width = `${activeTask.progress || 0}%`;
      DOM.step2StatusDetail.textContent = activeTask.stage || "";
    }
  } else if (rank >= stageRank("SOURCE_READY")) {
    if (DOM.btnStepDownload) {
      DOM.btnStepDownload.textContent = "⚡ Bắt Đầu Tải Video";
      DOM.btnStepDownload.classList.add("primary");
    }
  }

  // Step 4 Button & Live Box Dynamic State
  if (activeTask && activeTask.action === "ocr") {
    if (DOM.btnStepOcr) {
      DOM.btnStepOcr.textContent = `⏳ Đang bóc ${activeTask.progress || 0}%...`;
      DOM.btnStepOcr.disabled = true;
    }
    if (DOM.btnStepOcrCancel) {
      DOM.btnStepOcrCancel.style.display = "inline-flex";
    }
    if (DOM.btnStepOcrRerun) {
      DOM.btnStepOcrRerun.style.display = "none";
    }
    if (DOM.step4StatusBox) {
      DOM.step4StatusBox.style.display = "block";
      DOM.step4StatusText.textContent = activeTask.stage || "Đang bóc phụ đề...";
      DOM.step4StatusPct.textContent = `${activeTask.progress || 0}%`;
      DOM.step4StatusBar.style.width = `${activeTask.progress || 0}%`;
      DOM.step4StatusDetail.textContent = activeTask.stage || "";
    }
  } else if (rank >= stageRank("OCR_READY")) {
    if (DOM.btnStepOcr) {
      DOM.btnStepOcr.textContent = "✔ Đã Bóc Xong";
      DOM.btnStepOcr.classList.remove("primary");
      DOM.btnStepOcr.disabled = busy;
    }
    if (DOM.btnStepOcrCancel) {
      DOM.btnStepOcrCancel.style.display = "none";
    }
    if (DOM.btnStepOcrRerun) {
      DOM.btnStepOcrRerun.style.display = "inline-flex";
      DOM.btnStepOcrRerun.disabled = busy;
    }
    if (DOM.step4StatusBox) {
      DOM.step4StatusBox.style.display = "block";
      const cueCount = currentCues ? currentCues.length : 0;
      DOM.step4StatusText.textContent = `✔ Đã trích xuất ${cueCount} câu phụ đề thành công.`;
      DOM.step4StatusPct.textContent = "100%";
      DOM.step4StatusBar.style.width = "100%";
      if (DOM.step4StatusCueCount) DOM.step4StatusCueCount.textContent = `${cueCount} câu`;
      if (DOM.step4StatusDetail) DOM.step4StatusDetail.textContent = "Bạn có thể vào tab RapidOCR để sửa chữ, hoặc kéo lại ROI ở Bước 3 rồi bấm 'Bóc lại'.";
    }
  } else {
    if (DOM.btnStepOcr) {
      DOM.btnStepOcr.textContent = extractionMode === "stt" ? "Nhận dạng lời thoại" : extractionMode === "auto" ? "Tự động bóc lời thoại" : "Chạy OCR vùng phụ đề";
      DOM.btnStepOcr.classList.add("primary");
    }
    if (DOM.btnStepOcrCancel) {
      DOM.btnStepOcrCancel.style.display = "none";
    }
    if (DOM.btnStepOcrRerun) {
      DOM.btnStepOcrRerun.style.display = "none";
    }
    if (DOM.step4StatusBox) {
      DOM.step4StatusBox.style.display = "none";
    }
  }

  updateNextAction(step, stage, busy);
}

const NEXT_ACTIONS = {
  1: ["Kiểm tra nguồn video", "Đọc metadata; chưa tải bất kỳ dữ liệu video nào.", "workflow"],
  2: ["Tải hoặc nhập video", "Tải media và kiểm tra video/audio bằng ffprobe.", "workflow"],
  3: ["Khoanh vùng phụ đề Trung", "Kéo một hình chữ nhật chính xác quanh vùng chữ trên video.", "roi"],
  4: ["Bóc và duyệt phụ đề gốc", "Chạy OCR, sửa câu sai rồi bấm Phê duyệt OCR.", "ocr"],
  5: ["Dịch và duyệt tiếng Việt", "Dịch batch, sửa bản dịch rồi xác nhận trước khi tạo giọng.", "trans"],
  6: ["Chọn cách che và kiểu phụ đề", "Lưu cấu hình để preview và render dùng đúng cùng một kiểu.", "sub"],
  7: ["Tạo giọng tiếng Việt", "Tạo TTS theo từng cue và kiểm tra bản nghe thử.", "voice"],
  8: ["Render video hoàn chỉnh", "Trộn audio, che chữ và encode trong một pass.", "render"],
  9: ["Kiểm tra và tải thành phẩm", "QA thời lượng, coverage, drift rồi tải các artifact.", "verify"],
};

function updateNextAction(step, stage, busy) {
  const action = NEXT_ACTIONS[step] || NEXT_ACTIONS[1];
  DOM.nextActionIndex.textContent = stage === "VERIFIED" ? "✓" : String(step);
  DOM.nextActionTitle.textContent = stage === "VERIFIED" ? "Dự án đã hoàn tất" : action[0];
  DOM.nextActionDesc.textContent = busy ? `Đang chạy: ${currentProject.task.stage || currentProject.task.action}` : (stage === "VERIFIED" ? "QA đã đạt. Bạn có thể xem và tải video thành phẩm." : action[1]);
  DOM.btnNextAction.dataset.targetStep = String(step);
  DOM.btnNextAction.disabled = busy;
}

function updateStepper(stage) {
  const currentStep = stageToStepNumber(stage);

  for (let s = 1; s <= 9; s++) {
    const node = document.getElementById(`step-node-${s}`);
    const line = document.getElementById(`step-line-${s}`);

    if (node) {
      node.classList.remove("active", "completed");
      if (s < currentStep || (stage === "VERIFIED" && s === 9)) {
        node.classList.add("completed");
      } else if (s === currentStep) {
        node.classList.add("active");
      }
    }
    if (line) {
      line.classList.toggle("completed", s < currentStep);
    }
  }
}

DOM.btnWatchOriginal.addEventListener("click", () => {
  if (!currentProject) return;
  DOM.detailVideo.dataset.selectedSource = "original";
  updateVideoSource(currentProject);
});

DOM.btnWatchDubbed.addEventListener("click", () => {
  if (!currentProject || DOM.btnWatchDubbed.disabled) return;
  DOM.detailVideo.dataset.selectedSource = "dubbed";
  updateVideoSource(currentProject);
  DOM.detailVideo.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "center" });
  DOM.detailVideo.play().catch(() => {});
});

DOM.detailVideo.addEventListener("error", () => {
  if (DOM.detailVideo.getAttribute("src")) {
    DOM.videoSourceLabel.textContent = "Không phát được video. Thử chọn lại nguồn hoặc tải lại trang.";
  }
});

function updateVideoSource(project) {
  const hasOutput = ["RENDERED", "VERIFIED"].includes(project.stage || project.status)
    || project.workflow?.stages?.RENDERED?.status === "COMPLETED";
  const renderVersion = String(project.workflow?.stages?.RENDERED?.completed_at || "ready");
  DOM.btnWatchDubbed.disabled = !hasOutput;
  DOM.btnWatchDubbed.title = hasOutput ? "Xem ngay trong ứng dụng, không cần tải về" : "Có thể xem sau khi render hoàn tất";
  const newOutput = hasOutput && DOM.detailVideo.dataset.renderVersion !== renderVersion;
  if (newOutput) {
    DOM.detailVideo.dataset.selectedSource = "dubbed";
    DOM.detailVideo.dataset.renderVersion = renderVersion;
  }
  if (!hasOutput && DOM.detailVideo.dataset.selectedSource === "dubbed") {
    DOM.detailVideo.dataset.selectedSource = "original";
    delete DOM.detailVideo.dataset.renderVersion;
  }
  const source = DOM.detailVideo.dataset.selectedSource || "original";
  // If video is downloaded, load stream
  const hasMedia = !!(project.media && project.media.file_path);
  DOM.btnWatchOriginal.disabled = !hasMedia;
  if (hasMedia) {
    const loadedProjectId = DOM.detailVideo.dataset.loadedProjectId;
    const streamKey = `${project.id}:${source}:${source === "dubbed" ? renderVersion : ""}`;
    if (loadedProjectId !== streamKey) {
      const sameProject = loadedProjectId?.startsWith(`${project.id}:`);
      const position = sameProject ? DOM.detailVideo.currentTime : 0;
      const resume = sameProject && !DOM.detailVideo.paused;
      DOM.detailVideo.dataset.loadedProjectId = streamKey;
      DOM.detailVideo.src = `/api/projects/${project.id}/preview?source=${source === "dubbed" ? "dubbed" : "original"}&v=${encodeURIComponent(renderVersion)}`;
      DOM.detailVideo.addEventListener("loadedmetadata", () => {
        DOM.detailVideo.currentTime = Math.min(position || 0, Math.max(0, DOM.detailVideo.duration - 0.1));
        if (resume) DOM.detailVideo.play().catch(() => {});
      }, { once: true });
      DOM.detailVideo.style.display = "block";
      DOM.previewEmpty.style.display = "none";
      DOM.roiCanvas.style.display = source === "dubbed" ? "none" : "block";
      DOM.videoStatusBadge.textContent = source === "dubbed" ? "Video thành phẩm" : "Video gốc";
      DOM.videoStatusBadge.className = "badge badge-success";
    }
  } else {
    delete DOM.detailVideo.dataset.loadedProjectId;
    DOM.detailVideo.removeAttribute("src");
    DOM.detailVideo.style.display = "none";
    DOM.previewEmpty.style.display = "block";
    DOM.roiCanvas.style.display = "none";
    DOM.videoStatusBadge.textContent = "Chưa tải video";
    DOM.videoStatusBadge.className = "badge";
  }
  DOM.videoSourceLabel.textContent = !hasMedia ? "Chưa có video" : source === "dubbed" ? "Đang xem bản lồng tiếng" : "Đang xem video gốc";
  DOM.btnWatchOriginal.setAttribute("aria-pressed", String(source === "original"));
  DOM.btnWatchDubbed.setAttribute("aria-pressed", String(source === "dubbed"));

  // Audio preview
  if (project.stage === "VOICE_READY" || project.stage === "RENDERED" || project.stage === "VERIFIED") {
    const audioVersion = project.workflow?.stages?.VOICE_READY?.completed_at || project.task?.finished_at || 'ready';
    const audioKey = `${project.id}:${audioVersion}`;
    const loadedAudioProject = DOM.audioPreview.dataset.loadedProjectId;
    if (loadedAudioProject !== audioKey) {
      DOM.audioPreview.dataset.loadedProjectId = audioKey;
      DOM.audioPreview.src = `/api/projects/${project.id}/files/voiceover?v=${encodeURIComponent(audioVersion)}`;
      DOM.audioPreview.style.display = "block";
    }
  } else {
    delete DOM.audioPreview.dataset.loadedProjectId;
    DOM.audioPreview.removeAttribute("src");
    DOM.audioPreview.style.display = "none";
  }
}

// =============================================================================
// Interactive Canvas ROI Mouse Dragging
// =============================================================================
let isDraggingROI = false;
let startX = 0;
let startY = 0;
let dragBox = null;

function getVideoRenderRect() {
  const v = DOM.detailVideo;
  const c = DOM.roiCanvas;
  if (!v || !v.videoWidth || !v.videoHeight) {
    return { offsetX: 0, offsetY: 0, renderW: c.width, renderH: c.height };
  }
  const videoRatio = v.videoWidth / v.videoHeight;
  const canvasRatio = c.width / c.height;
  let renderW, renderH, offsetX, offsetY;

  if (canvasRatio > videoRatio) {
    renderH = c.height;
    renderW = c.height * videoRatio;
    offsetX = (c.width - renderW) / 2;
    offsetY = 0;
  } else {
    renderW = c.width;
    renderH = c.width / videoRatio;
    offsetX = 0;
    offsetY = (c.height - renderH) / 2;
  }
  return { offsetX, offsetY, renderW, renderH };
}

function syncCanvasSize() {
  const rect = DOM.roiVideoWrapper.getBoundingClientRect();
  if (rect.width > 0 && rect.height > 0) {
    DOM.roiCanvas.width = rect.width;
    DOM.roiCanvas.height = rect.height;
    drawROI();
  }
}

const resizeObserver = new ResizeObserver(() => syncCanvasSize());
resizeObserver.observe(DOM.roiVideoWrapper);

DOM.detailVideo.addEventListener("loadedmetadata", () => {
  currentDuration = DOM.detailVideo.duration;
  syncCanvasSize();
});

DOM.roiCanvas.addEventListener("mousedown", (e) => {
  const r = DOM.roiCanvas.getBoundingClientRect();
  startX = e.clientX - r.left;
  startY = e.clientY - r.top;
  isDraggingROI = true;
  dragBox = { xmin: startX, ymin: startY, xmax: startX, ymax: startY };
});

window.addEventListener("mousemove", (e) => {
  if (!isDraggingROI) return;
  const r = DOM.roiCanvas.getBoundingClientRect();
  const curX = Math.max(0, Math.min(r.width, e.clientX - r.left));
  const curY = Math.max(0, Math.min(r.height, e.clientY - r.top));

  dragBox = {
    xmin: Math.min(startX, curX),
    xmax: Math.max(startX, curX),
    ymin: Math.min(startY, curY),
    ymax: Math.max(startY, curY),
  };
  drawROI(dragBox);
});

window.addEventListener("mouseup", () => {
  if (!isDraggingROI) return;
  isDraggingROI = false;
  if (dragBox && dragBox.xmax - dragBox.xmin > 10 && dragBox.ymax - dragBox.ymin > 6) {
    const { offsetX, offsetY, renderW, renderH } = getVideoRenderRect();
    const normXmin = Math.max(0, Math.min(1, (dragBox.xmin - offsetX) / renderW));
    const normXmax = Math.max(0, Math.min(1, (dragBox.xmax - offsetX) / renderW));
    const normYmin = Math.max(0, Math.min(1, (dragBox.ymin - offsetY) / renderH));
    const normYmax = Math.max(0, Math.min(1, (dragBox.ymax - offsetY) / renderH));

    currentROI = {
      ymin: parseFloat(normYmin.toFixed(4)),
      ymax: parseFloat(normYmax.toFixed(4)),
      xmin: parseFloat(normXmin.toFixed(4)),
      xmax: parseFloat(normXmax.toFixed(4)),
    };
    updateROIBadge();
  }
  drawROI();
});

function drawROI(transientBox) {
  const c = DOM.roiCanvas;
  const ctx = c.getContext("2d");
  ctx.clearRect(0, 0, c.width, c.height);

  let pixelBox = null;
  if (transientBox) {
    pixelBox = transientBox;
  } else if (currentROI) {
    const { offsetX, offsetY, renderW, renderH } = getVideoRenderRect();
    pixelBox = {
      xmin: offsetX + currentROI.xmin * renderW,
      xmax: offsetX + currentROI.xmax * renderW,
      ymin: offsetY + currentROI.ymin * renderH,
      ymax: offsetY + currentROI.ymax * renderH,
    };
  }

  if (pixelBox) {
    const w = pixelBox.xmax - pixelBox.xmin;
    const h = pixelBox.ymax - pixelBox.ymin;

    // Glowing fill
    ctx.fillStyle = "rgba(99, 102, 241, 0.22)";
    ctx.fillRect(pixelBox.xmin, pixelBox.ymin, w, h);

    // Glowing border
    ctx.strokeStyle = "#818cf8";
    ctx.lineWidth = 2;
    ctx.strokeRect(pixelBox.xmin, pixelBox.ymin, w, h);

    // Corner handle ticks
    ctx.fillStyle = "#ffffff";
    const cornerSize = 4;
    ctx.fillRect(pixelBox.xmin - cornerSize / 2, pixelBox.ymin - cornerSize / 2, cornerSize, cornerSize);
    ctx.fillRect(pixelBox.xmax - cornerSize / 2, pixelBox.ymin - cornerSize / 2, cornerSize, cornerSize);
    ctx.fillRect(pixelBox.xmin - cornerSize / 2, pixelBox.ymax - cornerSize / 2, cornerSize, cornerSize);
    ctx.fillRect(pixelBox.xmax - cornerSize / 2, pixelBox.ymax - cornerSize / 2, cornerSize, cornerSize);

    // Label tag
    ctx.font = "11px Inter, sans-serif";
    ctx.fillStyle = "#818cf8";
    ctx.fillText("VÙNG SUB TRUNG (ROI)", pixelBox.xmin, Math.max(14, pixelBox.ymin - 5));
  }
}

function updateROIBadge() {
  if (currentROI) {
    DOM.roiCoordsBadge.textContent = `Y: [${Math.round(currentROI.ymin * 100)}%–${Math.round(currentROI.ymax * 100)}%] · X: [${Math.round(currentROI.xmin * 100)}%–${Math.round(currentROI.xmax * 100)}%]`;
    DOM.roiCoordsBadge.style.background = "rgba(16, 185, 129, 0.15)";
    DOM.roiCoordsBadge.style.color = "var(--ok)";
    DOM.roiCoordsBadge.style.borderColor = "var(--ok)";
  } else {
    DOM.roiCoordsBadge.textContent = "Chưa chọn vùng ROI";
    DOM.roiCoordsBadge.style.background = "rgba(139, 92, 246, 0.15)";
    DOM.roiCoordsBadge.style.color = "var(--primary-light)";
    DOM.roiCoordsBadge.style.borderColor = "rgba(139, 92, 246, 0.3)";
  }
}

// Auto-detect Subtitle ROI Button
if (DOM.btnRoiAutoDetect) {
  DOM.btnRoiAutoDetect.addEventListener("click", async () => {
    if (!currentProjectId) {
      alert("Vui lòng chọn hoặc tải video dự án trước.");
      return;
    }
    const origText = DOM.btnRoiAutoDetect.textContent;
    DOM.btnRoiAutoDetect.disabled = true;
    DOM.btnRoiAutoDetect.textContent = "⏳ Đang quét AI...";
    DOM.roiResult.textContent = "Đang phân tích các khung hình video để dò tìm phụ đề tiếng Trung...";
    DOM.roiResult.className = "result";

    try {
      const box = await API.autoDetectROI(currentProjectId);
      currentROI = {
        ymin: box.y,
        ymax: Number((box.y + box.height).toFixed(4)),
        xmin: box.x,
        xmax: Number((box.x + box.width).toFixed(4)),
      };
      updateROIBadge();
      drawROI();
      DOM.roiResult.textContent = `🎯 Đã phát hiện vị trí phụ đề: Y: [${Math.round(box.y * 100)}% - ${Math.round((box.y + box.height) * 100)}%], X: [${Math.round(box.x * 100)}% - ${Math.round((box.x + box.width) * 100)}%]. Hãy nhấn "💾 Lưu ROI" để áp dụng!`;
      DOM.roiResult.classList.add("ok");
    } catch (err) {
      DOM.roiResult.textContent = "Không thể tự động dò phụ đề: " + err.message;
      DOM.roiResult.classList.add("err");
    } finally {
      DOM.btnRoiAutoDetect.disabled = false;
      DOM.btnRoiAutoDetect.textContent = origText;
    }
  });
}

// Preset Bottom Subtitle Button
DOM.btnRoiPresetBottom.addEventListener("click", () => {
  currentROI = { ymin: 0.88, ymax: 0.98, xmin: 0.08, xmax: 0.92 };
  updateROIBadge();
  drawROI();
});

// Clear ROI
DOM.btnRoiClear.addEventListener("click", () => {
  currentROI = null;
  updateROIBadge();
  drawROI();
});

// Save ROI
DOM.btnRoiSave.addEventListener("click", async () => {
  if (!currentProjectId) return;
  if (!currentROI) {
    alert("Vui lòng kéo chọn hình chữ nhật quanh phụ đề Trung trước khi lưu.");
    return;
  }
  DOM.roiResult.textContent = "Đang lưu ROI...";
  DOM.roiResult.className = "result";
  try {
    await API.saveROI(currentProjectId, currentROI);
    DOM.roiResult.textContent = "Đã lưu ROI thành công! Các bước OCR, dịch và render đã được cập nhật.";
    DOM.roiResult.classList.add("ok");
    await refreshProjectData();
  } catch (err) {
    DOM.roiResult.textContent = err.message;
    DOM.roiResult.classList.add("err");
  }
});

// 30s Sample Test
DOM.btnRoiTest30s.addEventListener("click", async () => {
  if (!currentProjectId) return;
  if (!currentROI) {
    alert("Vui lòng lưu hoặc chọn vùng ROI trước khi thử mẫu 30s.");
    return;
  }
  DOM.btnRoiTest30s.disabled = true;
  DOM.btnRoiTest30s.textContent = "⏳ Đang thử 30s...";
  DOM.roiResult.textContent = "Đang cắt và bóc tách mẫu 30 giây đầu...";
  DOM.roiResult.className = "result";
  if (DOM.roiSampleBox) DOM.roiSampleBox.style.display = "none";

  try {
    const res = await API.previewSample(currentProjectId, 30);
    const cues = res.cues || (Array.isArray(res) ? res : []);
    const count = cues.length;
    const avgConf = Math.round(res.avg_confidence || 0);

    DOM.roiResult.textContent = `Thử mẫu 30s xong: Phát hiện ${count} câu thoại! Độ tin cậy TB: ${avgConf}%`;
    DOM.roiResult.classList.add("ok");

    if (DOM.roiSampleBox) {
      DOM.roiSampleBox.style.display = "block";
      if (count === 0) {
        DOM.roiSampleCuesContent.innerHTML = `<p class="muted" style="padding: 10px; margin: 0;">Không phát hiện thấy câu chữ nào trong 30 giây đầu ở vùng này.</p>`;
        DOM.roiSampleTip.innerHTML = `⚠️ <b>Lời khuyên:</b> Không tìm thấy chữ. Có thể trong 30 giây đầu video chưa có phụ đề, hoặc khung ROI vẽ chưa đúng vị trí dòng chữ. Bạn hãy kéo thanh tua video sang đoạn có chữ để kiểm tra vị trí ROI.`;
        DOM.roiSampleTip.style.color = "var(--warning)";
      } else {
        DOM.roiSampleCuesContent.innerHTML = cues.map(c => `
          <div style="padding: 4px 0; border-bottom: 1px dashed var(--border-subtle); display: flex; justify-content: space-between;">
            <span>[${formatDuration(c.start, true)} - ${formatDuration(c.end, true)}] <b>${escapeHtml(c.text || "(trống)")}</b></span>
            <span class="muted">${c.confidence || 0}% conf</span>
          </div>
        `).join("");

        const isSuspicious = cues.every(c => (c.text || "").length <= 3) || avgConf < 65;
        if (isSuspicious) {
          DOM.roiSampleTip.innerHTML = `⚠️ <b>Cảnh báo nhận diện:</b> Các câu bóc được có vẻ là chữ HUD game / ký tự ngắn (như <i>${escapeHtml(cues.map(c => c.text).slice(0, 3).join(", "))}</i>) thay vì câu hội thoại tiếng Trung. Nếu đây không phải dòng phụ đề bạn muốn dịch, hãy kéo lại khung ROI để bọc chính xác hơn trước khi bấm chạy cả video!`;
          DOM.roiSampleTip.style.color = "var(--warning)";
        } else {
          DOM.roiSampleTip.innerHTML = `✔ <b>Vùng chọn tốt!</b> Đã bóc ra câu thoại chuẩn. Bạn có thể yên tâm bấm <b>Chạy RapidOCR</b> ở Bước 4.`;
          DOM.roiSampleTip.style.color = "var(--ok)";
        }
      }
    }
  } catch (err) {
    DOM.roiResult.textContent = err.message;
    DOM.roiResult.classList.add("err");
  } finally {
    DOM.btnRoiTest30s.disabled = false;
    DOM.btnRoiTest30s.textContent = "🧪 Thử mẫu 30s";
  }
});

if (DOM.btnCloseRoiSample) {
  DOM.btnCloseRoiSample.addEventListener("click", () => {
    if (DOM.roiSampleBox) DOM.roiSampleBox.style.display = "none";
  });
}

// Micro Timeline Seekers
DOM.btnSeekBack5.addEventListener("click", () => { DOM.detailVideo.currentTime = Math.max(0, DOM.detailVideo.currentTime - 5); });
DOM.btnSeekBack1.addEventListener("click", () => { DOM.detailVideo.currentTime = Math.max(0, DOM.detailVideo.currentTime - 1); });
DOM.btnSeekFwd1.addEventListener("click", () => { DOM.detailVideo.currentTime = Math.min(DOM.detailVideo.duration || 9999, DOM.detailVideo.currentTime + 1); });
DOM.btnSeekFwd5.addEventListener("click", () => { DOM.detailVideo.currentTime = Math.min(DOM.detailVideo.duration || 9999, DOM.detailVideo.currentTime + 5); });

DOM.detailVideo.addEventListener("timeupdate", () => {
  DOM.roiTimeDisplay.textContent = formatDuration(DOM.detailVideo.currentTime, true);
});

DOM.btnCaptureFrame.addEventListener("click", () => {
  const v = DOM.detailVideo;
  if (!v || !v.videoWidth) return;
  const offCanvas = document.createElement("canvas");
  offCanvas.width = v.videoWidth;
  offCanvas.height = v.videoHeight;
  const offCtx = offCanvas.getContext("2d");
  offCtx.drawImage(v, 0, 0);
  const dataUrl = offCanvas.toDataURL("image/jpeg", 0.9);
  const win = window.open();
  if (win) {
    win.document.write(`<img src="${dataUrl}" style="max-width:100%;" /><br/><p>Khung hình tại ${formatDuration(v.currentTime, true)}</p>`);
  }
});

async function loadROI() {
  if (!currentProjectId || isDraggingROI) return;
  try {
    const roiData = await API.getROI(currentProjectId);
    if (Array.isArray(roiData) && roiData.length > 0) {
      const b = roiData[0];
      const newROI = { ymin: b.y, ymax: b.y + b.height, xmin: b.x, xmax: b.x + b.width };
      if (!currentROI || currentROI.ymin !== newROI.ymin || currentROI.ymax !== newROI.ymax || currentROI.xmin !== newROI.xmin || currentROI.xmax !== newROI.xmax) {
        currentROI = newROI;
        updateROIBadge();
        drawROI();
      }
    }
  } catch {}
}

// =============================================================================
// Live Render Preview (FFmpeg Filter Preview)
// =============================================================================
let previewRequestSequence = 0;
const previewNearestButton = document.getElementById('preview-nearest-cue');
if (previewNearestButton) previewNearestButton.addEventListener('click', () => {
  DOM.detailVideo.currentTime = Number(previewNearestButton.dataset.time);
  DOM.btnRenderPreview.click();
});
DOM.btnRenderPreview.addEventListener("click", async () => {
  if (!currentProjectId) return;
  const requestSequence = ++previewRequestSequence;
  const projectId = currentProjectId;
  const t = Number.isFinite(DOM.detailVideo.currentTime) ? DOM.detailVideo.currentTime : 0;
  const mode = normalizeRenderStyle(DOM.subMode ? DOM.subMode.value : "fast_cover");
  DOM.renderPreviewWrap.style.display = "block";
  DOM.renderPreviewNote.textContent = `Đang kết xuất khung hình tại ${formatDuration(t, true)} bằng FFmpeg...`;

  const previewUrl = API.getRenderPreviewUrl(currentProjectId, {
    ...collectRenderSettings(), t, style: mode,
  });
  if (previewNearestButton) previewNearestButton.style.display = 'none';
  try {
    const response = await fetch(previewUrl);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const blob = await response.blob();
    if (requestSequence !== previewRequestSequence || projectId !== currentProjectId) return;
    const state = response.headers.get('X-Preview-Subtitle-State');
    const nearest = response.headers.get('X-Preview-Nearest-Time');
    const explanation = state === 'gap'
      ? ' — Mốc này nằm ngoài câu sub Việt; hộp che chỉ hiện trong thời gian có phụ đề gốc.'
      : state === 'untranslated' ? ' — Chưa có bản dịch tiếng Việt để hiển thị.' : '';
    const url = URL.createObjectURL(blob);
    DOM.renderPreviewImg.onload = () => {
      URL.revokeObjectURL(url);
      if (requestSequence === previewRequestSequence && projectId === currentProjectId)
        DOM.renderPreviewNote.textContent = `Khung hình tại ${formatDuration(t, true)} (Chế độ: ${mode})${explanation}`;
    };
    DOM.renderPreviewImg.onerror = () => { URL.revokeObjectURL(url); };
    DOM.renderPreviewImg.src = url;
    if (previewNearestButton && state === 'gap' && nearest !== null && Number.isFinite(Number(nearest))) {
      previewNearestButton.dataset.time = nearest;
      previewNearestButton.textContent = `Xem câu có sub tại ${formatDuration(Number(nearest), true)}`;
      previewNearestButton.style.display = 'inline-flex';
    }
  } catch (err) {
    if (requestSequence === previewRequestSequence && projectId === currentProjectId)
      DOM.renderPreviewNote.textContent = 'Không tải được khung hình xem trước: ' + err.message;
  }
});

// =============================================================================
// Step 4: RapidOCR Cues & Review Table
// =============================================================================
let videoStopInterval = null;
function playVideoSegment(start, end) {
  const v = DOM.detailVideo;
  if (!v || !v.duration) {
    alert("Vui lòng tải video trước khi phát đoạn.");
    return;
  }
  if (videoStopInterval) {
    clearInterval(videoStopInterval);
    videoStopInterval = null;
  }
  v.currentTime = Math.max(0, start);
  v.play().catch(() => {});
  videoStopInterval = setInterval(() => {
    if (v.currentTime >= end || v.paused) {
      v.pause();
      clearInterval(videoStopInterval);
      videoStopInterval = null;
    }
  }, 100);
}

function playCueAudio(audioUrl) {
  if (cueAudioPlayer) {
    cueAudioPlayer.pause();
    cueAudioPlayer = null;
  }
  cueAudioPlayer = new Audio(audioUrl + (audioUrl.includes("?") ? "&" : "?") + "_r=" + Date.now());
  cueAudioPlayer.play().catch((err) => {
    alert("Không thể phát âm thanh: " + err.message);
  });
}

async function loadCues(force = false) {
  if (!currentProjectId) return;
  try {
    const res = await API.getCues(currentProjectId);
    const newCues = Array.isArray(res) ? res : (res.cues || []);
    if (force || JSON.stringify(newCues) !== JSON.stringify(currentCues)) {
      currentCues = newCues;
      renderCuesTable();
      renderTransTable();
      renderVoiceTable();
      loadVoiceTiming();
    }
  } catch {}
}

const cuePages = {ocr: 0, trans: 0, voice: 0};
let ocrReviewFilter = 'all';
function ocrNeedsReview(cue) {
  return !(cue.corrected_text || '').trim() &&
    ((cue.confidence || 0) < 70 || !!cue.ocr_alternatives?.length);
}
function ocrReviewEntries() {
  return (currentCues || []).map((c, i) => ({c, i})).filter(({c}) =>
    ocrReviewFilter === 'pending' ? ocrNeedsReview(c) :
    ocrReviewFilter === 'confirmed' ? !!(c.corrected_text || '').trim() : true);
}
function showOcrReview(filter = 'pending') {
  ocrReviewFilter = filter;
  cuePages.ocr = 0;
  renderCuesTable();
  DOM.cuesList.scrollTop = 0;
  document.getElementById('ocr-review-filters')?.scrollIntoView({block:'center', behavior:'smooth'});
}
document.querySelectorAll('[data-ocr-filter]').forEach(button => {
  button.addEventListener('click', () => showOcrReview(button.dataset.ocrFilter));
});
function cuePage(view, container, render, sourceEntries = null) {
  const size = 50;
  const allEntries = sourceEntries || (currentCues || []).map((c, i) => ({c, i}));
  const count = allEntries.length;
  const pages = Math.max(1, Math.ceil(count / size));
  cuePages[view] = Math.min(cuePages[view], pages - 1);
  let pager = document.getElementById(`cue-pager-${view}`);
  if (!pager) {
    pager = document.createElement("div");
    pager.id = `cue-pager-${view}`;
    pager.className = "controls";
    pager.style.margin = "12px 0";
    (container.closest('.cue-table-wrap') || container).before(pager);
  }
  pager.replaceChildren();
  pager.hidden = count <= size;
  const previous = document.createElement("button");
  previous.className = "btn small";
  previous.textContent = "← Trang trước";
  previous.disabled = cuePages[view] === 0;
  previous.onclick = () => { cuePages[view]--; render(); };
  const label = document.createElement("span");
  label.textContent = `Trang ${cuePages[view] + 1}/${pages} · Hiển thị ${count ? cuePages[view] * size + 1 : 0}–${Math.min(count, (cuePages[view] + 1) * size)} / ${count} câu`;
  const next = document.createElement("button");
  next.className = "btn small";
  next.textContent = "Trang sau →";
  next.disabled = cuePages[view] >= pages - 1;
  next.onclick = () => { cuePages[view]++; render(); };
  pager.append(previous, label, next);
  let bottom = document.getElementById(`cue-pager-bottom-${view}`);
  if (!bottom) {
    bottom = document.createElement("div");
    bottom.id = `cue-pager-bottom-${view}`;
    bottom.className = "controls";
    (container.closest('.cue-table-wrap') || container).after(bottom);
  }
  bottom.hidden = pager.hidden;
  bottom.replaceChildren(...[previous, label, next].map(element => {
    const copy = element.cloneNode(true);
    copy.onclick = element.onclick;
    return copy;
  }));
  const offset = cuePages[view] * size;
  return allEntries.slice(offset, offset + size);
}

async function removeOcrCue(cue, button) {
  const projectId = currentProjectId;
  if (!cue || !projectId) return;
  if (!confirm(`Xóa dòng #${cue.index} khỏi phụ đề?\nDùng cho tiêu đề, bản quyền, watermark hoặc dòng không phải lời thoại. Video nguồn vẫn được giữ nguyên.`)) return;
  button.disabled = true;
  try {
    await API.deleteCue(projectId, cue.index);
    if (currentProjectId === projectId) {
      await loadCues(true);
      await refreshProjectData();
      window.workspaceToast('Đã xóa dòng khỏi phụ đề');
    }
  } catch (err) {
    alert('Không xóa được câu: ' + err.message);
  } finally {
    button.disabled = false;
  }
}

async function confirmOcrCue(cueIndex, text, button) {
  button.disabled = true;
  try {
    await API.updateCue(currentProjectId, cueIndex, { corrected_text: text });
    window.workspaceToast('Đã xác nhận chữ sau khi kiểm tra');
    await loadCues(true);
    await refreshProjectData();
  } catch (err) {
    alert('Không xác nhận được câu OCR: ' + err.message);
  } finally {
    button.disabled = false;
  }
}

function renderCuesTable() {
  const pendingCount = (currentCues || []).filter(ocrNeedsReview).length;
  const confirmedCount = (currentCues || []).filter(c => (c.corrected_text || '').trim()).length;
  const counts = {all:currentCues?.length || 0, pending:pendingCount, confirmed:confirmedCount};
  const labels = {all:'Tất cả', pending:'Cần kiểm tra', confirmed:'Đã xác nhận'};
  document.querySelectorAll('[data-ocr-filter]').forEach(button => {
    const filter = button.dataset.ocrFilter;
    button.textContent = `${labels[filter]} (${counts[filter]})`;
    button.setAttribute('aria-pressed', String(filter === ocrReviewFilter));
    button.classList.toggle('primary', filter === ocrReviewFilter);
  });
  const status = document.getElementById('ocr-review-status');
  if (status) status.textContent = pendingCount
    ? `Còn ${pendingCount} câu cần đối chiếu ảnh trước khi phê duyệt. Bấm “Cần kiểm tra” để xem toàn bộ.`
    : 'Đã xử lý hết các câu bị đánh dấu. Bạn có thể phê duyệt OCR sau khi kiểm tra nội dung.';
  const entries = cuePage("ocr", DOM.cuesList, renderCuesTable, ocrReviewEntries());
  if (!currentCues || currentCues.length === 0) {
    DOM.cuesList.innerHTML = `<p class="muted" style="text-align: center; padding: 32px;">Chưa có dữ liệu câu thoại. Bấm <b>Chạy RapidOCR</b> ở trên để bóc tách.</p>`;
    DOM.ocrSummaryText.textContent = "0 câu thoại";
    return;
  }

  DOM.ocrSummaryText.textContent = `${currentCues.length} câu thoại bóc tách từ video`;
  if (!entries.length) {
    DOM.cuesList.innerHTML = `<p class="muted" style="padding:24px">${ocrReviewFilter === 'pending' ? 'Đã kiểm tra hết các câu bị đánh dấu. Bấm Phê duyệt OCR để tiếp tục.' : 'Chưa có câu đã xác nhận.'}</p>`;
    return;
  }

  DOM.cuesList.innerHTML = entries
    .map(({c, i}) => {
      const conf = Math.max(0, Math.min(100, Math.round(c.confidence || 0)));
      const ambiguous = c.ocr_alternatives?.length && !(c.corrected_text || '').trim();
      const needsReview = ocrNeedsReview(c);
      const confClass = conf >= 80 && !ambiguous ? "color: var(--ok);" : "color: var(--warning);";
      const thumbName = c.image_path ? c.image_path.split("/").pop() : "";
      const thumbSrc = thumbName ? `/api/projects/${currentProjectId}/cue-images/${encodeURIComponent(thumbName)}` : "";

      return `
        <div class="cue-row cue-ocr-grid" data-index="${i}" data-cue-id="${c.index}">
          <div style="font-family: var(--font-mono); font-weight: 700; color: var(--text-dim);">${i + 1}</div>
          <div>
            ${thumbSrc ? `<button class="cue-image-zoom" type="button" title="Phóng lớn ảnh để kiểm tra chữ" style="border:0;background:transparent;padding:0;cursor:zoom-in"><img src="${escapeHtml(thumbSrc)}" class="cue-crop-thumb" alt="Ảnh OCR cue ${i + 1}" loading="lazy" decoding="async" /></button>` : `<div style="font-size: 10px; color: var(--text-dim);">Không có ảnh</div>`}
          </div>
          <div style="font-family: var(--font-mono); font-size: 11.5px; color: var(--primary-light);">
            ${formatDuration(c.start, true)}<br/>${formatDuration(c.end, true)}
          </div>
          <div>
            <input class="cue-input cue-edit-text" data-index="${i}" value="${escapeHtml(c.corrected_text || c.text || "")}" placeholder="Chữ Hán..." />
            ${ambiguous ? `<small style="color:var(--warning)">Cần đối chiếu ảnh: ${escapeHtml(c.ocr_alternatives.map(a => a.text).join(" / "))}</small>` : ""}
            ${needsReview && !ambiguous ? `<small style="color:var(--warning)">Độ tin cậy thấp — kiểm tra ảnh rồi xác nhận chữ.</small>` : ""}
            ${c.corrected_text && c.corrected_text !== c.text ? `<small class="muted">OCR gốc: ${escapeHtml(c.text)}</small>` : ""}
          </div>
          <div style="font-weight: 700; font-family: var(--font-mono); ${confClass}">
            ${conf}%
          </div>
          <div class="cue-action-btns">
            ${needsReview ? `<button class="cue-action-btn btn-confirm-ocr" data-index="${i}" title="Đã đối chiếu ảnh và xác nhận chữ đang hiển thị">✓ Đã kiểm tra chữ</button>` : ""}
            <button class="cue-action-btn btn-play-cue" data-index="${i}" title="Phát đoạn video này">▷</button>
            <button class="cue-action-btn btn-recrop-cue" data-index="${i}" title="Cắt lại và OCR riêng câu này">▣ Cắt / OCR lại</button>
            <button class="cue-action-btn btn-split-cue" data-index="${i}" title="Tách câu thoại thành 2">✂ Tách</button>
            ${i < currentCues.length - 1 ? `<button class="cue-action-btn btn-merge-cue" data-index="${i}" title="Gộp với câu tiếp theo">⤹ Gộp</button>` : ""}
            <button class="cue-action-btn danger btn-delete-cue" data-index="${i}" title="Loại dòng không phải lời thoại khỏi phụ đề">🗑 Xóa câu</button>
          </div>
        </div>
      `;
    })
    .join("");

  if (ocrReviewFilter === 'pending') DOM.cuesList.scrollTop = 0;
  DOM.cuesList.querySelectorAll('.cue-image-zoom').forEach(button => {
    button.onclick = () => {
      const dialog = document.getElementById('ocr-image-preview');
      dialog.querySelector('img').src = button.querySelector('img').src;
      dialog.showModal();
    };
  });
  DOM.cuesList.querySelectorAll(".btn-recrop-cue").forEach(btn => {
    btn.onclick = () => openCueRecrop(currentCues[Number(btn.dataset.index)]);
  });
  DOM.cuesList.querySelectorAll(".btn-play-cue").forEach((btn) => {
    btn.addEventListener("click", () => {
      const idx = parseInt(btn.dataset.index, 10);
      const c = currentCues[idx];
      playVideoSegment(c.start, c.end);
    });
  });

  DOM.cuesList.querySelectorAll(".btn-split-cue").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const idx = parseInt(btn.dataset.index, 10);
      const c = currentCues[idx];
      const mid = ((c.start + c.end) / 2).toFixed(2);
      const promptVal = prompt(`Tách câu #${idx + 1} tại thời điểm giây (${c.start.toFixed(2)}s - ${c.end.toFixed(2)}s):`, mid);
      if (!promptVal) return;
      const splitTime = parseFloat(promptVal);
      if (isNaN(splitTime) || splitTime <= c.start || splitTime >= c.end) {
        alert(`Thời điểm tách không hợp lệ! Phải nằm trong khoảng (${c.start.toFixed(2)}s - ${c.end.toFixed(2)}s).`);
        return;
      }
      try {
        await API.splitCue(currentProjectId, c.index, { split_time: splitTime });
        await loadCues(true);
        alert(`Đã tách câu #${idx + 1} thành công!`);
      } catch (err) {
        alert("Lỗi tách câu: " + err.message);
      }
    });
  });

  DOM.cuesList.querySelectorAll(".btn-merge-cue").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const idx = parseInt(btn.dataset.index, 10);
      const c = currentCues[idx];
      const nextCue = currentCues[idx + 1];
      if (!nextCue) return;
      if (confirm(`Gộp câu #${idx + 1} với câu #${idx + 2}?`)) {
        try {
          await API.mergeCue(currentProjectId, c.index, nextCue.index);
          await loadCues(true);
          alert(`Đã gộp câu #${idx + 1} và #${idx + 2} thành công!`);
        } catch (err) {
          alert("Lỗi gộp câu: " + err.message);
        }
      }
    });
  });

  DOM.cuesList.querySelectorAll(".cue-edit-text").forEach((inp) => {
    inp.addEventListener("change", async (e) => {
      const idx = parseInt(e.target.dataset.index, 10);
      const val = e.target.value;
      try {
        await API.updateCue(currentProjectId, currentCues[idx].index, { corrected_text: val });
        window.workspaceToast('Đã lưu phụ đề gốc');
        await loadCues(true);
        await refreshProjectData();
      } catch (err) {
        alert("Lỗi cập nhật cue: " + err.message);
      }
    });
  });

  DOM.cuesList.querySelectorAll('.btn-confirm-ocr').forEach((button) => {
    button.addEventListener('click', () => {
      const index = Number(button.dataset.index);
      const input = DOM.cuesList.querySelector(`.cue-edit-text[data-index="${index}"]`);
      confirmOcrCue(currentCues[index].index, input.value, button);
    });
  });

  DOM.cuesList.querySelectorAll(".btn-delete-cue").forEach((btn) => {
    const cue = currentCues[Number(btn.dataset.index)];
    btn.addEventListener("click", () => {
      removeOcrCue(cue, btn);
    });
  });
}

DOM.btnStepOcr.addEventListener("click", () => runOCRTask());
DOM.btnOcrRunTab.addEventListener("click", () => runOCRTask());

if (DOM.btnStepOcrCancel) {
  DOM.btnStepOcrCancel.addEventListener("click", async () => {
    const activeJobId = currentProject && currentProject.job ? currentProject.job.id : null;
    if (!activeJobId) {
      if (DOM.btnCancel) DOM.btnCancel.click();
      return;
    }
    DOM.btnStepOcrCancel.disabled = true;
    DOM.btnStepOcrCancel.textContent = "Đang dừng...";
    try {
      await API.cancelJob(activeJobId);
      if (DOM.step4StatusBox) {
        DOM.step4StatusText.textContent = "Đã dừng bóc tách phụ đề theo yêu cầu.";
      }
      await refreshProjectData();
    } catch (err) {
      alert("Lỗi khi dừng tác vụ: " + err.message);
    } finally {
      DOM.btnStepOcrCancel.disabled = false;
      DOM.btnStepOcrCancel.textContent = "⛔ Dừng bóc";
    }
  });
}

if (DOM.btnStepOcrRerun) {
  DOM.btnStepOcrRerun.addEventListener("click", async () => {
    if (confirm("Bạn có muốn bóc tách lại toàn bộ phụ đề với ROI hiện tại không? Các câu cũ sẽ được làm mới.")) {
      await runOCRTask();
    }
  });
}

async function runOCRTask() {
  if (!currentProjectId) return;
  DOM.btnStepOcr.disabled = true;
  DOM.btnStepOcr.textContent = "⏳ Đang kết nối bóc sub...";
  if (DOM.step4StatusBox) {
    DOM.step4StatusBox.style.display = "block";
    DOM.step4StatusText.textContent = "Đang gửi yêu cầu bóc phụ đề...";
    DOM.step4StatusPct.textContent = "0%";
    DOM.step4StatusBar.style.width = "0%";
    DOM.step4StatusDetail.textContent = "";
  }
  if (DOM.btnStepOcrCancel) DOM.btnStepOcrCancel.style.display = "inline-flex";

  try {
    const task = await API.runOCR(currentProjectId, {extraction_mode: document.getElementById("extraction-mode")?.value || "hardsub"});
    await waitForTask(task, (pct, stage) => {
      DOM.detailBar.style.width = `${pct}%`;
      DOM.btnStepOcr.textContent = `⏳ Đang bóc ${pct}%...`;
      if (DOM.step4StatusBox) {
        DOM.step4StatusBox.style.display = "block";
        DOM.step4StatusPct.textContent = `${pct}%`;
        DOM.step4StatusBar.style.width = `${pct}%`;
        DOM.step4StatusText.textContent = stage || "Đang bóc phụ đề...";
        if (stage) DOM.step4StatusDetail.textContent = stage;
      }
      DOM.ocrSummaryText.textContent = `RapidOCR: ${pct}% - ${stage || ""}`;
    });
    DOM.btnStepOcr.textContent = "✔ Đã Bóc Xong";
    DOM.btnStepOcr.classList.remove("primary");
    await refreshProjectData();
    switchStudioTab("ocr");
    alert("Đã bóc xong phụ đề! Mời bạn kiểm tra, sửa câu sai ở tab RapidOCR bên dưới trước khi chuyển sang Bước 5.");
  } catch (err) {
    DOM.btnStepOcr.disabled = false;
    DOM.btnStepOcr.textContent = "⚡ Thử Lại RapidOCR";
    if (DOM.step4StatusBox) {
      DOM.step4StatusText.textContent = `⚠️ ${err.message}`;
    }
    // The worker may have restored the previous completed OCR after cancellation.
    // Reload authoritative state after it has stopped, not only at cancel request time.
    await refreshProjectData();
    await loadCues(true);
    alert("Thông báo bóc phụ đề: " + err.message);
  } finally {
    if (DOM.btnStepOcrCancel) DOM.btnStepOcrCancel.style.display = "none";
  }
}

DOM.btnApproveOcr.addEventListener("click", async () => {
  if (!currentProjectId) return;
  await loadCues(true);
  if ((currentCues || []).some(ocrNeedsReview)) {
    showOcrReview('pending');
    return;
  }
  try {
    await API.approveCues(currentProjectId);
    alert("Đã phê duyệt nguồn OCR thành công! Trạng thái chuyển sang SOURCE_REVIEWED.");
    await refreshProjectData();
  } catch (err) {
    if (err.code === 'TRANSCRIPT_NOT_FOUND') {
      await loadCues(true);
      if ((currentCues || []).some(ocrNeedsReview)) {
        showOcrReview('pending');
        return;
      }
    }
    alert("Lỗi phê duyệt OCR: " + err.message);
  }
});

// =============================================================================
// Step 5: Gemini Translation & Review Table
// =============================================================================
function renderTransTable() {
  const entries = cuePage("trans", DOM.transList, renderTransTable);
  if (!currentCues || currentCues.length === 0) {
    DOM.transList.innerHTML = `<p class="muted" style="text-align: center; padding: 32px;">Chưa có dữ liệu câu thoại để dịch.</p>`;
    DOM.transSummaryText.textContent = "Chưa dịch";
    return;
  }

  const translatedCount = currentCues.filter((c) => c.vi_text && c.vi_text.trim()).length;
  DOM.transSummaryText.textContent = `Đã dịch ${translatedCount}/${currentCues.length} câu thoại`;

  DOM.transList.innerHTML = entries
    .map(({c, i}) => {
      const isSelected = selectedCueIndices.has(c.index);
      return `
        <div class="cue-row cue-trans-grid" data-index="${i}">
          <div>
            <input type="checkbox" class="cb-cue-select" data-cue-index="${c.index}" ${isSelected ? "checked" : ""} />
          </div>
          <div style="font-family: var(--font-mono); font-weight: 700; color: var(--text-dim);">${i + 1}</div>
          <div style="font-family: var(--font-mono); font-size: 11.5px; color: var(--primary-light);">
            ${formatDuration(c.start, true)}<br/>${formatDuration(c.end, true)}
          </div>
          <div style="font-size: 13px; color: var(--text-muted); line-height: 1.4;">
            ${escapeHtml(c.corrected_text || c.text || "")}
          </div>
          <div>
            <input class="cue-input trans-edit-text" data-index="${i}" value="${escapeHtml(c.vi_text || "")}" placeholder="Bản dịch tiếng Việt..." />
          </div>
          <div class="cue-action-btns">
            <button class="cue-action-btn btn-play-cue" data-index="${i}" title="Phát đoạn video này">▷</button>
            <button class="cue-action-btn btn-retranslate-single" data-index="${i}" title="Dịch lại riêng câu này bằng Gemini">🌐 Dịch</button>
            <button class="cue-action-btn btn-cue-tts" data-index="${i}" title="Nghe / Tạo lại giọng đọc câu này">🎙 Voice</button>
          </div>
        </div>
      `;
    })
    .join("");

  DOM.transList.querySelectorAll(".cb-cue-select").forEach((cb) => {
    cb.addEventListener("change", (e) => {
      const cueIdx = parseInt(e.target.dataset.cueIndex, 10);
      if (e.target.checked) {
        selectedCueIndices.add(cueIdx);
      } else {
        selectedCueIndices.delete(cueIdx);
      }
      DOM.selectedCuesCount.textContent = selectedCueIndices.size;
      DOM.btnRetranslateSelected.disabled = selectedCueIndices.size === 0;
    });
  });

  DOM.transList.querySelectorAll(".btn-play-cue").forEach((btn) => {
    btn.addEventListener("click", () => {
      const idx = parseInt(btn.dataset.index, 10);
      const c = currentCues[idx];
      playVideoSegment(c.start, c.end);
    });
  });

  DOM.transList.querySelectorAll(".btn-retranslate-single").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const idx = parseInt(btn.dataset.index, 10);
      const c = currentCues[idx];
      btn.textContent = "⏳...";
      btn.disabled = true;
      try {
        const task = await API.runTranslate(currentProjectId, {
          tone: DOM.transTone.value,
          cues: [c.index],
        });
        await waitForTask(task);
        await loadCues(true);
      } catch (err) {
        alert("Lỗi dịch lại câu #" + (idx + 1) + ": " + err.message);
      } finally {
        btn.textContent = "🌐 Dịch";
        btn.disabled = false;
      }
    });
  });

  DOM.transList.querySelectorAll(".btn-cue-tts").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const idx = parseInt(btn.dataset.index, 10);
      const c = currentCues[idx];
      btn.textContent = "⏳...";
      btn.disabled = true;
      try {
        const rateVal = DOM.voiceRate ? DOM.voiceRate.value : 0;
        const rateStr = (rateVal >= 0 ? "+" + rateVal : rateVal) + "%";
        const voicePick = DOM.voicePick ? DOM.voicePick.value : "vi-VN-NamMinhNeural";
        const res = await API.singleCueTTS(currentProjectId, c.index, {
          voice: withVoiceColor(voicePick),
          rate: rateStr,
        });
        await loadCues(true);
        if (res.audio_url) playCueAudio(res.audio_url);
      } catch (err) {
        alert("Lỗi tạo voice câu #" + (idx + 1) + ": " + err.message);
      } finally {
        btn.textContent = "🎙 Voice";
        btn.disabled = false;
      }
    });
  });

  DOM.transList.querySelectorAll(".trans-edit-text").forEach((inp) => {
    inp.addEventListener("change", async (e) => {
      const idx = parseInt(e.target.dataset.index, 10);
      const val = e.target.value;
      try {
        await API.updateCue(currentProjectId, currentCues[idx].index, { vi_text: val });
        currentCues[idx].vi_text = val;
        window.workspaceToast('Đã lưu bản dịch');
      } catch (err) {
        alert("Lỗi cập nhật bản dịch: " + err.message);
      }
    });
  });
}

DOM.btnStepTranslate.addEventListener("click", () => runTranslateTask());
DOM.btnTransRunTab.addEventListener("click", () => runTranslateTask());

// Find & Replace and Selection Listeners
if (DOM.btnTransFindReplace) {
  DOM.btnTransFindReplace.addEventListener("click", async () => {
    if (!currentProjectId) return;
    const findText = DOM.transFindInput.value;
    const replaceText = DOM.transReplaceInput.value;
    if (!findText) {
      alert("Vui lòng nhập chuỗi cần tìm kiếm.");
      return;
    }
    try {
      const res = await API.findReplace(currentProjectId, {
        find_text: findText,
        replace_text: replaceText,
        match_case: DOM.transMatchCase ? DOM.transMatchCase.checked : false,
      });
      alert(`Tìm & thay thế hoàn tất: Đã sửa ${res.replaced_count || 0} câu thoại!`);
      await loadCues(true);
    } catch (err) {
      alert("Lỗi tìm & thay thế: " + err.message);
    }
  });
}

if (DOM.cbSelectAllCues) {
  DOM.cbSelectAllCues.addEventListener("change", (e) => {
    const checked = e.target.checked;
    if (checked) {
      currentCues.forEach((c) => selectedCueIndices.add(c.index));
    } else {
      selectedCueIndices.clear();
    }
    document.querySelectorAll(".cb-cue-select").forEach((cb) => {
      cb.checked = checked;
    });
    if (DOM.selectedCuesCount) DOM.selectedCuesCount.textContent = selectedCueIndices.size;
    if (DOM.btnRetranslateSelected) DOM.btnRetranslateSelected.disabled = selectedCueIndices.size === 0;
  });
}

if (DOM.btnRetranslateSelected) {
  DOM.btnRetranslateSelected.addEventListener("click", async () => {
    if (!currentProjectId || selectedCueIndices.size === 0) return;
    const count = selectedCueIndices.size;
    if (!confirm(`Dịch lại ${count} câu thoại đã chọn bằng Gemini?`)) return;
    try {
      const task = await API.runTranslate(currentProjectId, {
        tone: DOM.transTone.value,
        cues: Array.from(selectedCueIndices),
      });
      await waitForTask(task);
      selectedCueIndices.clear();
      if (DOM.cbSelectAllCues) DOM.cbSelectAllCues.checked = false;
      if (DOM.selectedCuesCount) DOM.selectedCuesCount.textContent = "0";
      DOM.btnRetranslateSelected.disabled = true;
      await loadCues(true);
      alert(`Đã dịch lại xong ${count} câu thoại!`);
    } catch (err) {
      alert("Lỗi dịch lại: " + err.message);
    }
  });
}

function renderVoiceTable() {
  if (!DOM.voiceCuesList) return;
  const entries = cuePage("voice", DOM.voiceCuesList, renderVoiceTable);
  if (!currentCues || currentCues.length === 0) {
    DOM.voiceCuesList.innerHTML = `<p class="muted" style="text-align: center; padding: 24px;">Chưa có dữ liệu câu thoại. Hãy hoàn thành bóc sub và dịch trước.</p>`;
    if (DOM.voiceSummaryText) DOM.voiceSummaryText.textContent = "0 câu thoại";
    return;
  }
  const voicedCount = currentCues.filter((c) => c.voice_audio_path).length;
  if (DOM.voiceSummaryText) DOM.voiceSummaryText.textContent = `Đã tạo giọng ${voicedCount}/${currentCues.length} câu thoại`;

  DOM.voiceCuesList.innerHTML = entries
    .map(({c, i}) => {
      const slotDur = Math.max(0, c.end - c.start);
      const voiceDur = c.voice_duration ? Number(c.voice_duration).toFixed(2) + "s" : "Chưa tạo";
      const hasAudio = !!c.voice_audio_path;
      return `
        <div class="cue-row cue-voice-grid" data-index="${i}">
          <div style="font-family: var(--font-mono); font-weight: 700; color: var(--text-dim);">${i + 1}</div>
          <div style="font-family: var(--font-mono); font-size: 11.5px; color: var(--primary-light);">
            ${formatDuration(c.start, true)} - ${formatDuration(c.end, true)}
          </div>
          <div style="font-size: 12.5px; color: var(--text-main); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${escapeHtml(c.vi_text || '')}">
            ${escapeHtml(c.vi_text || "(Chưa dịch)")}
          </div>
          <div style="font-size: 11.5px; font-family: var(--font-mono);">
            <span>${voiceDur}</span> / <span class="muted">${slotDur.toFixed(2)}s</span>
            ${c.tts_rate ? `<span class="muted" title="Tốc độ đọc riêng của câu này"> · ${escapeHtml(c.tts_rate)}</span>` : ""}
          </div>
          <div class="cue-action-btns">
            ${hasAudio ? `<button class="cue-action-btn btn-play-voice" data-index="${i}" title="Nghe âm thanh câu này">▷ Nghe</button>` : ""}
            <button class="cue-action-btn btn-regen-voice" data-index="${i}" title="Tạo lại giọng đọc cho câu này">🎙 Tạo lại</button>
          </div>
        </div>
      `;
    })
    .join("");

  DOM.voiceCuesList.querySelectorAll(".btn-play-voice").forEach((btn) => {
    btn.addEventListener("click", () => {
      const idx = parseInt(btn.dataset.index, 10);
      const c = currentCues[idx];
      const audioUrl = `/api/projects/${currentProjectId}/cue-audio/clip_${String(c.index).padStart(4, "0")}.wav`;
      playCueAudio(audioUrl);
    });
  });

  DOM.voiceCuesList.querySelectorAll(".btn-regen-voice").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const idx = parseInt(btn.dataset.index, 10);
      const c = currentCues[idx];
      btn.textContent = "⏳...";
      btn.disabled = true;
      try {
        const rateVal = DOM.voiceRate ? DOM.voiceRate.value : 0;
        const rateStr = (rateVal >= 0 ? "+" + rateVal : rateVal) + "%";
        const voicePick = DOM.voicePick ? DOM.voicePick.value : "vi-VN-NamMinhNeural";
        const res = await API.singleCueTTS(currentProjectId, c.index, {
          voice: withVoiceColor(voicePick),
          rate: rateStr,
        });
        await loadCues(true);
        if (res.audio_url) playCueAudio(res.audio_url);
      } catch (err) {
        alert("Lỗi tạo giọng câu #" + (idx + 1) + ": " + err.message);
      } finally {
        btn.textContent = "🎙 Tạo lại";
        btn.disabled = false;
      }
    });
  });
}

async function runTranslateTask() {
  if (!currentProjectId) return;
  const tone = DOM.transTone.value;
  DOM.transSummaryText.textContent = "Đang gửi batch dịch sang Gemini...";
  try {
    const batch_size = Number(document.getElementById('translation-batch-size')?.value || 100);
    const task = await API.runTranslate(currentProjectId, { tone, batch_size });
    await waitForTask(task, (pct) => {
      DOM.transSummaryText.textContent = `Gemini đang dịch: ${pct}%`;
    });
    DOM.transSummaryText.textContent = "Hoàn tất dịch tiếng Việt!";
    await refreshProjectData();
    switchStudioTab("trans");
  } catch (err) {
    alert("Lỗi dịch Gemini: " + err.message);
  }
}

DOM.btnApproveTrans.addEventListener("click", async () => {
  if (!currentProjectId) return;
  try {
    await API.approveTranslate(currentProjectId);
    alert("Đã phê duyệt bản dịch tiếng Việt! Trạng thái chuyển sang TRANSLATION_REVIEWED.");
    await refreshProjectData();
  } catch (err) {
    alert("Lỗi phê duyệt bản dịch: " + err.message);
  }
});

// =============================================================================
// Step 6: Subtitle Placement & Fast Cover
// =============================================================================
DOM.coverBoxOpacity.addEventListener("input", () => {
  DOM.coverBoxOpacityVal.textContent = `${DOM.coverBoxOpacity.value} (${Math.round(DOM.coverBoxOpacity.value * 100)}%)`;
});

DOM.subFontSize.addEventListener("input", () => {
  DOM.subFontSizeVal.textContent = `${DOM.subFontSize.value} px`;
});

DOM.subMarginV.addEventListener("input", () => {
  DOM.subMarginVVal.textContent = `${DOM.subMarginV.value} px`;
});

DOM.btnSaveSubSettings.addEventListener("click", async () => {
  if (!currentProjectId) return;
  DOM.subSettingsResult.textContent = "Đang lưu cấu hình...";
  DOM.subSettingsResult.className = "result";
  try {
    await API.saveSubtitleSettings(currentProjectId, collectRenderSettings());
    DOM.subSettingsResult.textContent = "Đã lưu. Preview và render sẽ dùng cấu hình này.";
    DOM.subSettingsResult.classList.add("ok");
    await refreshProjectData();
  } catch (err) {
    DOM.subSettingsResult.textContent = err.message;
    DOM.subSettingsResult.classList.add("err");
  }
});

function assColor(hex) {
  return '&H00' + hex.slice(5, 7) + hex.slice(3, 5) + hex.slice(1, 3);
}
function htmlColor(ass) {
  const value = (ass || '&H00FFFFFF').replace('&H', '').padStart(8, '0');
  return '#' + value.slice(6, 8) + value.slice(4, 6) + value.slice(2, 4);
}
const subtitleExtraFields = {
  cover_effect: ['sub-cover-effect', 'value'], blur_strength: ['sub-blur', 'number'],
  font_name: ['sub-font-name', 'value'], outline_width: ['sub-outline', 'number'],
  font_shear: ['sub-shear', 'number'], text_width_percent: ['sub-text-width', 'number'],
  cover_full_width: ['sub-full-width', 'checked'],
  font_scale_x: ['sub-scale-x', 'number'], box_padding: ['sub-padding', 'number'],
  font_bold: ['sub-bold', 'checked'], font_italic: ['sub-italic', 'checked'],
  uppercase: ['sub-uppercase', 'checked'], prefer_single_line: ['sub-single-line', 'checked'],
};
function extraSubtitleSettings() {
  const result = {};
  for (const [key, [id, type]] of Object.entries(subtitleExtraFields)) {
    const el = document.getElementById(id);
    result[key] = type === 'number' ? Number(el.value) : el[type];
  }
  result.font_color = assColor(document.getElementById('sub-font-color').value);
  result.outline_color = assColor(document.getElementById('sub-outline-color').value);
  return result;
}
function loadExtraSubtitleSettings(cfg) {
  for (const [key, [id, type]] of Object.entries(subtitleExtraFields)) {
    if (cfg[key] !== undefined) document.getElementById(id)[type === 'number' ? 'value' : type] = cfg[key];
  }
  document.getElementById('sub-font-color').value = htmlColor(cfg.font_color);
  document.getElementById('sub-outline-color').value = htmlColor(cfg.outline_color || '&H00000000');
}
document.getElementById('sub-reference-preset').addEventListener('click', () => {
  loadExtraSubtitleSettings({cover_effect:'blur', blur_strength:24, font_name:'Anton',
    font_bold:false, font_italic:true, font_shear:0.16, uppercase:true, font_scale_x:98,
    prefer_single_line:true, text_width_percent:84, cover_full_width:true, outline_width:4, box_padding:0,
    font_color:'&H00FFFFFF', outline_color:'&H00000000'});
  DOM.subMode.value = 'fast_cover';
  DOM.coverBoxColor.value = '#55515c';
  DOM.coverBoxOpacity.value = 0.20;
  DOM.subFontSize.value = 72;
  [DOM.coverBoxOpacity, DOM.subFontSize].forEach(el => el.dispatchEvent(new Event('input')));
  DOM.subSettingsResult.textContent = 'Đã nạp mẫu v2: Anton nét dày, xiên nhẹ, viền đen 4 px, dải che ngang khung. Xem thử rồi lưu cấu hình.';
});
function collectRenderSettings() {
  return {
    ...extraSubtitleSettings(),
    style: normalizeRenderStyle(DOM.subMode.value),
      audio_mode: document.getElementById('audio-mode')?.value || 'mix',
    preset: DOM.renderPreset.value,
    box_color: DOM.coverBoxColor.value,
    box_opacity: parseFloat(DOM.coverBoxOpacity.value),
    font_size: parseInt(DOM.subFontSize.value, 10),
    margin_v: parseInt(DOM.subMarginV.value, 10),
    voice_volume: parseFloat(DOM.volVoice.value),
    bg_volume: parseFloat(DOM.volOriginal.value),
    use_nvenc: true,
  };
}

async function loadSubtitleSettings() {
  if (!currentProjectId) return;
  try {
    const cfg = await API.getSubtitleSettings(currentProjectId);
    loadExtraSubtitleSettings(cfg);
    const audioMode = document.getElementById('audio-mode');
    if (audioMode) audioMode.value = cfg.audio_mode || 'mix';
    DOM.subMode.value = ({ cover: "fast_cover", clean: "clean_and_place", bilingual: "bilingual" })[cfg.style] || "fast_cover";
    DOM.coverBoxColor.value = /^#[0-9a-f]{6}$/i.test(cfg.box_color || "") ? cfg.box_color : "#000000";
    DOM.coverBoxOpacity.value = cfg.box_opacity ?? 1;
    const savedFontSize = cfg.font_size ?? 48;
    DOM.subFontSize.min = Math.min(8, savedFontSize);
    DOM.subFontSize.max = Math.max(128, savedFontSize);
    DOM.subFontSize.value = savedFontSize;
    DOM.subMarginV.value = cfg.margin_v ?? 26;
    DOM.renderPreset.value = ({ p4: "p4", p5: "medium", p6: "hq" })[cfg.encoder_preset] || "p4";
    DOM.volVoice.value = cfg.voice_volume ?? 1.7;
    DOM.volOriginal.value = cfg.bg_volume ?? 0.15;
    [DOM.coverBoxOpacity, DOM.subFontSize, DOM.subMarginV, DOM.volVoice, DOM.volOriginal].forEach((el) => el.dispatchEvent(new Event("input")));
  } catch {}
}

DOM.btnPreviewSubStyle.addEventListener("click", () => {
  DOM.btnRenderPreview.click();
  DOM.renderPreviewWrap.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" });
});

DOM.btnSwitchToSub.addEventListener("click", () => switchStudioTab("sub"));
document.getElementById('audio-mode')?.addEventListener('change', () => {
  if (currentProjectId) refreshProjectData();
});

// =============================================================================
// Step 7: Voiceover & Vietnamese Normalizer
// =============================================================================
DOM.voiceRate.addEventListener("input", () => {
  const v = parseInt(DOM.voiceRate.value, 10);
  DOM.voiceRateVal.textContent = v > 0 ? `+${v}%` : `${v}%`;
});

// Normalizer Test
DOM.btnNormTest.addEventListener("click", () => {
  const text = DOM.inputNormTest.value;
  if (!text) return;
  // Simple front normalization test client-side mirror
  let norm = text
    .replace(/\b50k\b/gi, "năm mươi nghìn")
    .replace(/\b(\d+)k\b/gi, "$1 nghìn")
    .replace(/\bAI\b/g, "A-I")
    .replace(/\bAi\b/g, "ai");
  DOM.normTestOutput.textContent = `Kết quả đọc: "${norm}"`;
});

DOM.btnStepVoiceover.addEventListener("click", () => runVoiceoverTask());
DOM.btnVoiceRunTab.addEventListener("click", () => runVoiceoverTask());

function readVoiceSettings() {
  if (DOM.voicePick && (!DOM.voicePick.value || DOM.voicePick.selectedOptions?.[0]?.dataset.unavailable === 'true')) {
    throw new Error('Giọng đã lưu chưa sẵn sàng. Hãy chọn giọng khả dụng trước khi chạy.');
  }
  const value = Number.parseInt(DOM.voiceRate?.value || '0', 10);
  return {
    voice: withVoiceColor(DOM.voicePick?.value || undefined),
    rate: `${value >= 0 ? '+' : ''}${value}%`,
    fit_to_video: document.getElementById('voice-auto-fit')?.checked ?? true,
    shorten_to_fit: document.getElementById('voice-auto-shorten')?.checked ?? true,
    repair_existing: document.getElementById('voice-repair-existing')?.checked ?? false,
  };
}

function withVoiceColor(voice) {
  const control = document.getElementById('voice-color');
  const color = control && !control.disabled && ['review','night','news','mystery'].includes(control.value) ? control.value : 'default';
  return color === 'default' ? voice : `${voice || 'vi-VN-NamMinhNeural'}@color=${color}`;
}

async function runVoiceoverTask(selectedOptions = null) {
  if (!currentProjectId) return;
  if (!DOM.voicePick.value || DOM.voicePick.selectedOptions?.[0]?.dataset.unavailable === 'true') {
    alert('Giọng đã lưu chưa sẵn sàng. Hãy chọn và nghe thử một giọng khả dụng.');
    return;
  }
  const options = selectedOptions || readVoiceSettings();
  window.dispatchEvent(new CustomEvent('workspace-voice-start', {detail: options}));
  DOM.voiceTabResult.textContent = "Đang tạo giọng đã chọn, xử lý màu giọng và căn timing...";
  DOM.voiceTabResult.className = "result";

  try {
    const task = await API.runStep(currentProjectId, "voiceover", options);
    await waitForTask(task, (pct) => {
      DOM.voiceTabResult.textContent = `Đang tạo giọng & fit timeline: ${pct}%`;
    });
    DOM.voiceTabResult.textContent = "Tạo giọng đọc hoàn tất! Bấm nghe từng câu để kiểm tra; render lại để cập nhật giọng vào video thành phẩm.";
    DOM.voiceTabResult.classList.add("ok");
    await refreshProjectData();
    switchStudioTab("voice");
  } catch (err) {
    DOM.voiceTabResult.textContent = err.message;
    DOM.voiceTabResult.classList.add("err");
    await loadVoiceTiming();
  }
}

let voiceTimingGeneration = 0;
async function loadVoiceTiming() {
  const node=document.getElementById('voice-timing-issues');
  if(!currentProjectId || !node)return;
  const projectId=currentProjectId,generation=++voiceTimingGeneration;
  try {
    const result=await API.request('GET',`/api/projects/${projectId}/voiceover/timing`);
    if(projectId!==currentProjectId || generation!==voiceTimingGeneration)return;
    const measured=result.issues.length>0,rows=measured?result.issues:result.risks;
    node.replaceChildren();
    if(!rows.length && !result.stale_report && !result.structure_findings?.length)return;
    const heading=document.createElement('h3');heading.textContent=measured?'Câu cần sửa timing (đã đo audio)':'Câu có nguy cơ quá dài (ước tính từ bản dịch)';node.append(heading);
    const description=document.createElement('p');description.className='muted';
    description.textContent=measured?'Câu tràn có thể làm câu sau bắt đầu muộn. Sửa câu gây tràn trước rồi chạy lại; cache giọng không đổi được giữ.':'Đây là cảnh báo để rà soát trước khi tạo giọng, không phải kết quả QA. Kiểm tra OCR/timing và rút gọn bản dịch nếu phù hợp.';
    if(result.stale_report)description.textContent+=' Báo cáo lần trước chưa khớp dữ liệu hiện tại; cần đo lại để xác nhận.';
    node.append(description);
    if(result.structure_findings?.length){
      const warning=document.createElement('p');warning.className='muted';
      warning.textContent=`${result.structure_findings.length} câu nghi có chú thích, cue vụn hoặc sub hiện dần/lặp. Đây là nghi vấn; hệ thống giữ nguyên nguồn, không tự gộp/xóa thoại.`;
      node.append(warning);
    }
    for(const row of rows){
      const item=document.createElement('div');item.className='timing-issue-row';
      const label=document.createElement('span');label.textContent=`#${row.cue_index} · ${Number(row.start).toFixed(2)}–${Number(row.end).toFixed(2)}s · slot ${Number(row.slot_duration).toFixed(2)}s`+(measured?` · giọng ${Number(row.clip_duration).toFixed(2)}s · lệch ${Number(row.drift).toFixed(2)}s`:'')+` — ${row.text||''}`;
      if(measured && row.cause)label.textContent+=(row.cause==='intrinsic_overflow'?' · Câu gây tràn':row.cause==='delayed_by_predecessor'?' · Bị trễ do câu trước':'');
      const button=document.createElement('button');button.type='button';button.className='btn small';button.textContent='Sửa bản dịch';button.addEventListener('click',()=>{
        const index=currentCues.findIndex(cue=>cue.index===row.cue_index);
        if(index<0)return;
        cuePages.trans=Math.floor(index/50);
        renderTransTable();
        switchStudioTab('trans');
        const input=DOM.transList.querySelector(`.trans-edit-text[data-index="${index}"]`);
        if(input){input.focus({preventScroll:true});input.scrollIntoView({block:'center',behavior:'instant'});}
      });
      item.append(label,button);node.append(item);
    }
  } catch(error) {
    if(projectId===currentProjectId && generation===voiceTimingGeneration)node.textContent='Chưa đọc được chẩn đoán timing: '+error.message;
  }
}

// Sidechain Equalizer Sliders
DOM.volVoice.addEventListener("input", () => {
  DOM.volVoiceVal.textContent = `${parseFloat(DOM.volVoice.value).toFixed(2)}×`;
});

DOM.volOriginal.addEventListener("input", () => {
  DOM.volOriginalVal.textContent = `${parseFloat(DOM.volOriginal.value).toFixed(2)}×`;
});

document.querySelectorAll("[data-preset]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const preset = btn.dataset.preset;
    if (preset === "tu-tien") {
      DOM.volVoice.value = 1.7;
      DOM.volOriginal.value = 0.15;
    } else if (preset === "balanced") {
      DOM.volVoice.value = 1.6;
      DOM.volOriginal.value = 0.12;
    } else if (preset === "voice-only") {
      DOM.volVoice.value = 1.7;
      DOM.volOriginal.value = 0.0;
    }
    DOM.volVoice.dispatchEvent(new Event("input"));
    DOM.volOriginal.dispatchEvent(new Event("input"));
  });
});

DOM.btnRemix.addEventListener("click", async () => {
  if (!currentProjectId) return;
  DOM.remixResult.textContent = "Đang trộn thử audio...";
  DOM.remixResult.className = "result";
  try {
    const voiceVol = parseFloat(DOM.volVoice.value);
    const origVol = parseFloat(DOM.volOriginal.value);
    const task = await API.remix(currentProjectId, { voice_volume: voiceVol, original_volume: origVol });
    await waitForTask(task, (pct) => {
      DOM.remixResult.textContent = `Đang trộn thử audio: ${pct}%`;
    });
    DOM.remixResult.textContent = "Trộn audio thành công!";
    DOM.remixResult.classList.add("ok");
    DOM.audioPreview.src = `/api/projects/${currentProjectId}/files/audio_preview?_r=${Date.now()}`;
    DOM.audioPreview.style.display = "block";
    DOM.audioPreview.play().catch(() => {});
  } catch (err) {
    DOM.remixResult.textContent = err.message;
    DOM.remixResult.classList.add("err");
  }
});

// =============================================================================
// Step 8: One-Pass NVENC Render
// =============================================================================
DOM.btnStepRender.addEventListener("click", () => runRenderTask());
DOM.btnRenderRunTab.addEventListener("click", () => runRenderTask());

async function runRenderTask() {
  if (!currentProjectId) return;
  const mode = DOM.subMode.value;
  const preset = DOM.renderPreset.value;
  const boxColor = DOM.coverBoxColor.value;
  const boxOpacity = parseFloat(DOM.coverBoxOpacity.value);
  const fontSize = parseInt(DOM.subFontSize.value, 10);
  const voiceVolume = parseFloat(DOM.volVoice.value);
  const bgVolume = parseFloat(DOM.volOriginal.value);

  DOM.renderTabResult.textContent = "Đang bắt đầu Render một pass với GPU NVENC...";
  DOM.renderTabResult.className = "result";

  try {
    const renderSettings = collectRenderSettings();
    await API.saveSubtitleSettings(currentProjectId, renderSettings);
    const task = await API.runRender(currentProjectId, renderSettings);
    await waitForTask(task, (pct, stage) => {
      DOM.renderTabResult.textContent = `Render NVENC đang chạy: ${pct}% (${stage})`;
    });
    DOM.renderTabResult.textContent = "Render một pass thành công! Đang tự động kiểm tra QA...";
    DOM.renderTabResult.classList.add("ok");
    await refreshProjectData();
    switchStudioTab("verify");
    await runVerifyTask();
  } catch (err) {
    DOM.renderTabResult.textContent = err.message;
    DOM.renderTabResult.classList.add("err");
  }
}

// =============================================================================
// Step 9: QA Verification & Download Package
// =============================================================================
DOM.btnStepVerify.addEventListener("click", () => {
  switchStudioTab("verify");
  runVerifyTask();
});

DOM.btnVerifyRunTab.addEventListener("click", () => runVerifyTask());

async function runVerifyTask() {
  if (!currentProjectId) return;
  try {
    const rep = await API.getVerify(currentProjectId);
    renderVerifyScorecard(rep);
    if (rep.cloud_task) {
      alert('QA kỹ thuật hoàn tất. Video đã được đưa vào hàng đợi tải Drive; mở Kho cloud để xem tiến độ.');
    }
    updateDownloadLinks();
  } catch (err) {
    alert("Lỗi kiểm tra QA: " + err.message);
  }
}

function renderVerifyScorecard(rep) {
  rep = rep || {};
  // Duration diff
  const durationKnown = Number.isFinite(rep.duration_diff);
  const dDiff = durationKnown ? Math.abs(rep.duration_diff).toFixed(3) : null;
  const durationPass = durationKnown && Math.abs(rep.duration_diff) <= 0.1 && rep.duration_passed !== false;
  DOM.qaMetricDuration.textContent = !durationKnown ? "Chưa có phép đo thời lượng" : durationPass ? `Lệch ${dDiff}s (Đạt)` : `Lệch ${dDiff}s (Không đạt)`;
  DOM.qaMetricDuration.className = !durationKnown ? "qa-warn" : durationPass ? "qa-pass" : "qa-fail";

  // Drift
  const driftKnown = Number.isFinite(rep.max_drift) && Number.isFinite(rep.mean_drift);
  const originalAudio = rep.audio_mode === 'original_only' && rep.voice_required === false;
  const driftPass = driftKnown && rep.max_drift <= 1.5 && rep.mean_drift <= 0.3 && rep.drift_passed !== false;
  DOM.qaMetricDrift.textContent = !driftKnown ? "Chưa có phép đo timing" : `Lệch lớn nhất ${rep.max_drift.toFixed(3)}s; trung bình ${rep.mean_drift.toFixed(3)}s (${driftPass ? "Đạt" : "Không đạt"})`;
  DOM.qaMetricDrift.className = !driftKnown ? "qa-warn" : driftPass ? "qa-pass" : "qa-fail";
  if (originalAudio) {
    DOM.qaMetricDrift.textContent = 'Không áp dụng timing TTS — giữ audio gốc';
    DOM.qaMetricDrift.className = 'qa-pass';
  }

  // Coverage
  const coverageKnown = [rep.total_cues, rep.voiced_cues, rep.translated_cues].every(Number.isInteger) && rep.total_cues > 0;
  const covPass = coverageKnown && (originalAudio || rep.voiced_cues === rep.total_cues) && rep.translated_cues === rep.total_cues;
  DOM.qaMetricCoverage.textContent = !coverageKnown ? "Chưa có số liệu phủ câu" : `Giọng ${rep.voiced_cues}/${rep.total_cues}; bản dịch ${rep.translated_cues}/${rep.total_cues} câu (${covPass ? "Đầy đủ" : "Không đạt"})`;
  DOM.qaMetricCoverage.className = !coverageKnown ? "qa-warn" : covPass ? "qa-pass" : "qa-fail";
  if (originalAudio && coverageKnown) DOM.qaMetricCoverage.textContent = `Giữ audio gốc; bản dịch ${rep.translated_cues}/${rep.total_cues} câu (${covPass ? 'Đầy đủ' : 'Không đạt'})`;

  // Degraded flags
  const degraded = Array.isArray(rep.degraded) ? rep.degraded : null;
  if (!degraded) {
    DOM.qaMetricDegraded.textContent = "Chưa có báo cáo suy hao";
    DOM.qaMetricDegraded.className = "qa-warn";
  } else if (degraded.length === 0) {
    DOM.qaMetricDegraded.textContent = "Không có cảnh báo suy hao trong báo cáo";
    DOM.qaMetricDegraded.className = "qa-pass";
  } else {
    DOM.qaMetricDegraded.textContent = `Cảnh báo: ${degraded.join(", ")}`;
    DOM.qaMetricDegraded.className = "qa-warn";
  }
}

function updateDownloadLinks() {
  if (!currentProjectId) return;
  DOM.linkDownloadVideo.href = `/api/projects/${currentProjectId}/files/dubbed_video`;
  DOM.linkDownloadViSrt.href = `/api/projects/${currentProjectId}/files/vi_dub_srt`;
  DOM.linkDownloadSourceSrt.href = `/api/projects/${currentProjectId}/files/source_srt`;
  DOM.linkDownloadTimeline.href = `/api/projects/${currentProjectId}/files/voice_timeline`;
  DOM.linkDownloadVerify.href = `/api/projects/${currentProjectId}/files/verify_report`;
}

// 1-Click Run-All
document.getElementById("extraction-mode")?.addEventListener("change", () => refreshProjectData());
document.getElementById('btn-no-subtitles')?.addEventListener('click', async () => {
  const mode = document.getElementById('extraction-mode');
  mode.value = 'stt';
  await refreshProjectData();
  document.getElementById('card-step-4')?.scrollIntoView({behavior: 'smooth', block: 'center'});
});
API.request("GET", "/api/translation/providers").then((providers) => {
  const modelLabel = document.getElementById('trans-key-status');
  if (modelLabel && providers.gemini?.model) modelLabel.textContent = providers.gemini.model;
  const localOption = document.querySelector('#translation-provider option[value="local_hymt"]');
  if (localOption && providers.local_hymt?.available) {
    localOption.disabled = false;
    localOption.textContent = "HY-MT cục bộ (cần duyệt bản dịch)";
  }
}).catch(() => {});
DOM.btnRunAll.addEventListener("click", async () => {
  if (!currentProjectId) return;
  if (!confirm("Chạy tự động các công đoạn còn lại (OCR → Dịch → Giọng → Render)?")) return;
  try {
    const task = await API.runAll(currentProjectId, {...readVoiceSettings(), extraction_mode: document.getElementById("extraction-mode")?.value || "hardsub", translation_provider: document.getElementById("translation-provider")?.value || "gemini", translation_batch_size: Number(document.getElementById('translation-batch-size')?.value || 100)});
    await waitForTask(task, (pct, stage) => {
      DOM.detailBar.style.width = `${pct}%`;
      DOM.detailStage.textContent = stage;
    });
    alert("Hoàn tất quy trình 1-Click Fast Hardsub!");
    await refreshProjectData();
  } catch (err) {
    alert("Lỗi chạy tự động: " + err.message);
  }
});

// Step 2 Download
DOM.btnStepSource.addEventListener("click", async () => {
  if (!currentProjectId) return;
  try {
    const task = await API.runStep(currentProjectId, "source");
    await waitForTask(task, (pct) => {
      DOM.detailBar.style.width = `${pct}%`;
    });
    await refreshProjectData();
  } catch (err) {
    DOM.detailError.textContent = `⚠️ ${err.message}`;
  }
});

DOM.btnStepDownload.addEventListener("click", async () => {
  if (!currentProjectId) return;
  DOM.btnStepDownload.disabled = true;
  DOM.btnStepDownload.textContent = "⏳ Đang kết nối tải...";
  if (DOM.step2StatusBox) {
    DOM.step2StatusBox.style.display = "block";
    DOM.step2StatusText.textContent = "Đang gửi yêu cầu tải tới máy chủ...";
    DOM.step2StatusPct.textContent = "0%";
    DOM.step2StatusBar.style.width = "0%";
    DOM.step2StatusDetail.textContent = "";
  }
  try {
    const task = await API.runStep(currentProjectId, "download");
    await waitForTask(task, (pct, stage) => {
      DOM.detailBar.style.width = `${pct}%`;
      DOM.btnStepDownload.textContent = `⏳ Đang tải ${pct}%...`;
      if (DOM.step2StatusBox) {
        DOM.step2StatusBox.style.display = "block";
        DOM.step2StatusPct.textContent = `${pct}%`;
        DOM.step2StatusBar.style.width = `${pct}%`;
        DOM.step2StatusText.textContent = stage || "Đang tải video...";
        if (stage && (stage.includes("MB") || stage.includes("s") || stage.includes("mirror") || stage.includes("mux"))) {
          DOM.step2StatusDetail.textContent = stage;
        }
      }
    });
    DOM.btnStepDownload.textContent = "✔ Đã Tải Xong";
    DOM.btnStepDownload.classList.remove("primary");
    if (DOM.step2StatusBox) {
      DOM.step2StatusText.textContent = "✔ Tải và kiểm tra video hoàn tất!";
      DOM.step2StatusPct.textContent = "100%";
      DOM.step2StatusBar.style.width = "100%";
    }
    await refreshProjectData();
    alert("Đã tải xong video nguồn! Video đã sẵn sàng trên khung xem bên trái. Hãy khoanh vùng phụ đề Trung ở Bước 3.");
    DOM.detailVideo.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "center" });
  } catch (err) {
    DOM.btnStepDownload.disabled = false;
    DOM.btnStepDownload.textContent = "⚡ Thử Tải Lại";
    if (DOM.step2StatusBox) {
      DOM.step2StatusText.textContent = `⚠️ Lỗi: ${err.message}`;
      DOM.step2StatusText.style.color = "var(--danger)";
    }
    alert("Lỗi tải video: " + err.message);
  }
});

DOM.btnFocusRoi.addEventListener("click", () => {
  DOM.detailVideo.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" });
});

// Cancel & Delete
DOM.btnCancel.addEventListener("click", async () => {
  const activeJobId = currentProject && (
    currentProject.active_job_id ||
    (currentProject.job && currentProject.job.id) ||
    (currentProject.task && currentProject.task.job_id)
  );
  if (!activeJobId) {
    alert("Không có tác vụ nào đang chạy");
    return;
  }
  try {
    await API.cancelJob(activeJobId);
    alert("Đã gửi yêu cầu huỷ tác vụ");
    await refreshProjectData();
  } catch (err) {
    alert("Lỗi huỷ tác vụ: " + err.message);
  }
});

DOM.btnDelete.addEventListener("click", async () => {
  if (!currentProjectId) return;
  if (confirm(`Xoá vĩnh viễn dự án ${currentProjectId}?`)) {
    try {
      await API.deleteProject(currentProjectId);
      showView("dashboard");
    } catch (err) {
      alert("Lỗi xoá dự án: " + err.message);
    }
  }
});

function updateLogs(project) {
  DOM.detailMeta.innerHTML = `
    <dt>Mã dự án:</dt><dd><code>${escapeHtml(project.id)}</code></dd>
    <dt>Đường dẫn nguồn:</dt><dd style="word-break: break-all;">${escapeHtml(project.source_url || "")}</dd>
    <dt>Thời lượng:</dt><dd>${project.duration ? formatDuration(project.duration) : "--"}</dd>
    <dt>Độ phân giải:</dt><dd>${project.resolution || "Chưa rõ"}</dd>
  `;

  if (project.logs && project.logs.length > 0) {
    DOM.detailLogs.textContent = project.logs.join("\n");
  } else {
    DOM.detailLogs.textContent = "Chưa có nhật ký ghi nhận.";
  }
}

// =============================================================================
// Gemini API Key Pool Modal
// =============================================================================
DOM.btnOpenKeys.addEventListener("click", () => {
  DOM.modalKeys.style.display = "flex";
  loadKeys();
});

DOM.btnCloseKeys.addEventListener("click", () => {
  DOM.modalKeys.style.display = "none";
});

DOM.modalKeys.addEventListener("click", (e) => {
  if (e.target === DOM.modalKeys) DOM.modalKeys.style.display = "none";
});

async function loadKeys() {
  try {
    const keys = await API.getKeys();
    renderKeysList(keys || []);
  } catch (err) {
    DOM.keysListContainer.innerHTML = `<p class="error" style="text-align: center; padding: 20px;">Lỗi tải keys: ${escapeHtml(err.message)}</p>`;
  }
}

function renderKeysList(keys) {
  if (keys.length === 0) {
    DOM.keysListContainer.innerHTML = `<p class="muted" style="text-align: center; padding: 20px;">Chưa có key nào trong Pool. Hãy thêm key ở trên.</p>`;
    return;
  }

  DOM.keysListContainer.innerHTML = keys
    .map((k) => {
      const isCooldown = ["cooldown", "exhausted"].includes(k.status);
      const modelWait = Object.entries(k.model_cooldowns || {}).map(([model, seconds]) =>
        `${model}: ${Math.ceil((Number(seconds) || 0) / 60)} phút`).join(" · ");
      const statusBadge = isCooldown
        ? `<span class="badge" style="background: rgba(245, 158, 11, 0.15); color: var(--warning);">Làm mát 429</span>`
        : k.status === "healthy"
        ? `<span class="badge badge-success">Sẵn sàng</span>`
        : `<span class="badge badge-danger">Lỗi</span>`;

      return `
        <div class="key-list-item" data-key-id="${escapeHtml(k.id)}">
          <div>
            <div style="font-family: var(--font-mono); font-weight: 700; color: var(--text-main);">
              ${escapeHtml(k.masked_key || k.id)}
            </div>
            <div class="muted" style="font-size: 11px;">
              Requests: ${k.total_calls || 0}${isCooldown ? ` · Còn ${k.cooldown_remaining_s || 0}s` : ""}
            </div>
            ${modelWait ? `<div class="muted" style="font-size:11px;">Đang chờ riêng: ${escapeHtml(modelWait)}</div>` : ""}
          </div>
          <div style="display: flex; align-items: center; gap: 8px;">
            ${statusBadge}
            <button class="btn small btn-test-key" data-key-id="${escapeHtml(k.id)}" title="Thử nghiệm kết nối live">Thử Key</button>
            <button class="btn small btn-project-key" data-key-id="${escapeHtml(k.id)}" data-project-id="${escapeHtml(k.project_id || '')}">Project: ${escapeHtml(k.project_id || 'Chưa gán')}</button>
            <button class="btn link small btn-delete-key" data-key-id="${escapeHtml(k.id)}" style="color: var(--danger);" title="Xoá key">✕</button>
          </div>
        </div>
      `;
    })
    .join("");

  document.querySelectorAll(".btn-test-key").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const kid = btn.dataset.keyId;
      btn.textContent = "Đang test...";
      try {
        const res = await API.testKey(kid);
        btn.textContent = res.healthy ? `OK (${res.latency_ms || 0}ms)` : "Thất bại";
        setTimeout(() => loadKeys(), 1200);
      } catch (err) {
        btn.textContent = "Lỗi";
        alert("Lỗi kiểm tra key: " + err.message);
      }
    });
  });

  document.querySelectorAll('.btn-project-key').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const project_id = prompt('Google Cloud project chứa key này. Các key cùng project phải dùng cùng ID để chia sẻ cooldown/quota. Để trống nếu chưa biết.', btn.dataset.projectId);
      if (project_id === null) return;
      try {
        await API.request('PATCH', `/api/settings/keys/${encodeURIComponent(btn.dataset.keyId)}/project`, {project_id: project_id.trim()});
        await loadKeys();
      } catch (err) {
        alert('Lỗi lưu project: ' + err.message);
      }
    });
  });

  document.querySelectorAll(".btn-delete-key").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const kid = btn.dataset.keyId;
      if (confirm("Bạn có chắc chắn muốn xoá key này khỏi Pool?")) {
        try {
          await API.deleteKey(kid);
          loadKeys();
        } catch (err) {
          alert("Lỗi xoá key: " + err.message);
        }
      }
    });
  });
}

DOM.btnSubmitKey.addEventListener("click", async () => {
  const raw = DOM.inputAddKey.value.trim();
  if (!raw) return;
  DOM.keyAddResult.textContent = "Đang lưu...";

  const result = await API.addKeys(raw);
  DOM.keyAddResult.textContent = `Đã thêm ${result.added || 0} key vào pool!`;
  DOM.inputAddKey.value = "";
  loadKeys();
});

// =============================================================================
// Utilities
// =============================================================================
function escapeHtml(str) {
  if (typeof str !== "string") return str;
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function normalizeRenderStyle(style) {
  return ({ fast_cover: "cover", clean_place: "clean", clean_and_place: "clean" })[style] || style || "cover";
}

function formatDuration(sec, withMs = false) {
  if (!sec || isNaN(sec)) return "00:00";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  const ms = Math.floor((sec % 1) * 100);
  const pad = (n) => String(n).padStart(2, "0");
  if (withMs) {
    return `${pad(m)}:${pad(s)}.${pad(ms)}`;
  }
  return `${pad(m)}:${pad(s)}`;
}

// Initial Boot
restoreWorkspace();


// Persisted usage dashboard. Render all provider/user labels as text.
let usageOffset = 0;
let usageGeneration = 0;
const usageModal = document.getElementById('usage-modal');
const usageValue = (v) => v == null ? 'chưa có' : typeof v === 'number' ? v.toLocaleString('vi-VN', {maximumFractionDigits:1}) : escapeHtml(String(v));
function usageTable(headers, rows) {
  return `<div class="usage-scroll"><table><thead><tr>${headers.map(h=>`<th>${escapeHtml(h)}</th>`).join('')}</tr></thead><tbody>${rows.join('') || `<tr><td colspan="${headers.length}">Chưa có dữ liệu trong phạm vi này.</td></tr>`}</tbody></table></div>`;
}
function renderUsage(data) {
  const t=data.total;
  const card=(label,value,note='')=>`<div class="usage-card">${label}<strong>${usageValue(value)}</strong><small class="muted">${note}</small></div>`;
  let html='<div class="usage-grid">'+card('Request hôm nay',data.today.requests,`${t.requests} tổng cộng · ${t.pending} chưa có kết quả`)+card('Thành công / lỗi',`${t.success} / ${t.failed}`,'Kết quả API, chưa xác nhận nội dung')+card('RPM / TPM (vào + ra)',`${usageValue(data.rpm)} / ${usageValue(data.tpm)}`,'Cửa sổ 60 giây; TPM chưa gồm suy luận')+card('Độ trễ TB / P95 (ms)',`${usageValue(t.mean_ms)} / ${usageValue(t.p95_ms)}`,'Không gồm thời gian chờ pacing')+'</div>';
  html+='<h3>Token từ phản hồi API</h3><div class="usage-grid">';
  for(const [field,label] of [['input_tokens','Đầu vào'],['output_tokens','Đầu ra'],['cached_tokens','Cache'],['reasoning_tokens','Suy luận']]) html+=card(label,t[field],`${t[field+'_reported_requests']}/${t.requests} request có dữ liệu; tổng phần đã biết`);
  html+='</div><h3>Chi phí do lỗi và thử lại</h3>';
  html+=`<p>${t.retries} lượt gọi lại · token đầu vào/ra: ${usageValue(data.retry_usage.input_tokens)} / ${usageValue(data.retry_usage.output_tokens)}. Thời gian API thất bại: ${usageValue(data.failed_time_ms/1000)} giây; token đầu vào/ra: ${usageValue(data.failed_usage.input_tokens)} / ${usageValue(data.failed_usage.output_tokens)}. Hai nhóm có thể giao nhau, không cộng chúng. Thời gian chờ retry thực tế: chưa có.</p>`;
  html+='<h3>Key và nhóm quota</h3>';
  html+=usageTable(['Key','Cloud project','Trạng thái','Chờ / model'],data.keys.map(k=>`<tr><td>${escapeHtml(k.id)} • ${escapeHtml(k.masked_key)}</td><td>${escapeHtml(k.project_id||'Chưa gán')}</td><td>${escapeHtml(k.status)}</td><td>${usageValue(k.cooldown_remaining_s)}s · ${escapeHtml(JSON.stringify(k.model_cooldowns))}</td></tr>`));
  html+='<p class="muted">Key cùng Cloud project/model dùng chung nhóm. Key chưa gán project không được coi là các hạn mức độc lập. Quota còn lại: chưa xác minh.</p><h3>Theo Cloud project / model / video</h3>';
  html+=usageTable(['Cloud project','Model','Video','Request','Thành công / lỗi','Input / output'],data.groups.map(g=>`<tr><td>${escapeHtml(g.cloud_project||'Chưa gán')}</td><td>${escapeHtml(g.model)}</td><td>${usageValue(g.video_id)}</td><td>${g.requests}</td><td>${g.success} / ${g.failed}</td><td>${usageValue(g.input_tokens)} / ${usageValue(g.output_tokens)}</td></tr>`));
  if(data.video) {
    const v=data.video;
    html+='<h3>Video này còn cần gì?</h3><div class="usage-grid">'+card('Chưa dịch',v.missing_translation,'Cue từ database')+card('Chưa có tệp giọng',v.missing_voice_files,'Tệp tồn tại, khác rỗng; chưa kiểm định âm thanh')+card('Chưa duyệt nội dung',v.unreviewed,'Theo cờ duyệt cue trong database')+card('QA kỹ thuật',v.technical_qa,'Trạng thái đã lưu, độc lập với duyệt nội dung')+'</div><p class="muted">Ước tính API còn lại và tỷ lệ cache: chưa có dữ liệu đo đủ tin cậy.</p>';
    html+=usageTable(['Công đoạn','Trạng thái','Tiến độ','Thời gian đã lưu (s)'],Object.entries(v.stages).map(([name,x])=>`<tr><td>${escapeHtml(name)}</td><td>${escapeHtml(x.status)}</td><td>${usageValue(x.progress)}%</td><td>${usageValue(x.started_at ? x.elapsed_seconds : null)}</td></tr>`));
  }
  html+='<h3>Nhật ký theo key</h3>';
  html+=usageTable(['Thời điểm','Key','Model','Kết quả','Độ trễ (ms)','Chi tiết'],data.requests.map(r=>`<tr><td>${escapeHtml(new Date(r.started_at).toLocaleString('vi-VN',{timeZone:'Asia/Bangkok'}))}</td><td>${escapeHtml(r.key_id)} • …${escapeHtml(r.key_suffix)}</td><td>${escapeHtml(r.model)}</td><td>${escapeHtml(r.outcome)} · ${usageValue(r.http_status)}</td><td>${usageValue(r.latency_ms)}</td><td><details><summary>Xem chi tiết</summary>Video: ${usageValue(r.video_id)}<br>Cloud project: ${usageValue(r.cloud_project)}<br>Lượt: ${r.attempt}<br>Retry-After từ API: ${usageValue(r.retry_after_s)}s<br>Token input/output/cache/suy luận: ${usageValue(r.input_tokens)} / ${usageValue(r.output_tokens)} / ${usageValue(r.cached_tokens)} / ${usageValue(r.reasoning_tokens)}</details></td></tr>`));
  document.getElementById('usage-content').innerHTML=html;
  document.getElementById('usage-prev').disabled=usageOffset===0;
  document.getElementById('usage-next').disabled=!data.has_more;
}
async function loadUsage(reset=false) {
  if(reset) usageOffset=0;
  const generation=++usageGeneration;
  const params=new URLSearchParams({offset:String(usageOffset)});
  for(const [name,id] of [['video_id','usage-video'],['cloud_project','usage-cloud'],['model','usage-model']]) {const value=document.getElementById(id).value.trim(); if(value) params.set(name,value);}
  document.getElementById('usage-content').textContent='Đang đọc thống kê từ database…';
  try {
    const data=await API.request('GET','/api/usage?'+params);
    if(generation!==usageGeneration) return;
    const select=document.getElementById('usage-video'), selected=select.value;
    select.innerHTML='<option value="">Tất cả video</option>'+data.videos.map(v=>`<option value="${escapeHtml(v.id)}">${escapeHtml(v.title)} (${escapeHtml(v.id)})</option>`).join(''); select.value=selected;
    renderUsage(data);
  } catch(err) {if(generation===usageGeneration) document.getElementById('usage-content').textContent='Không tải được thống kê: '+err.message;}
}
document.getElementById('btn-open-usage').addEventListener('click',()=>{usageModal.style.display='flex';document.getElementById('usage-close').focus();loadUsage(true);});
document.getElementById('usage-close').addEventListener('click',()=>{usageModal.style.display='none';document.getElementById('btn-open-usage').focus();});
document.getElementById('usage-refresh').addEventListener('click',()=>loadUsage(true));
for(const id of ['usage-video','usage-cloud','usage-model']) document.getElementById(id).addEventListener('change',()=>loadUsage(true));
document.getElementById('usage-prev').addEventListener('click',()=>{usageOffset=Math.max(0,usageOffset-50);loadUsage();});
document.getElementById('usage-next').addEventListener('click',()=>{usageOffset+=50;loadUsage();});
usageModal.addEventListener('keydown',event=>{
  if(event.key==='Escape') document.getElementById('usage-close').click();
  if(event.key==='Tab') {
    const controls=[...usageModal.querySelectorAll('button,input,select,summary')].filter(x=>!x.disabled);
    const first=controls[0],last=controls[controls.length-1];
    if(event.shiftKey && document.activeElement===first) { event.preventDefault();last.focus(); }
    else if(!event.shiftKey && document.activeElement===last) { event.preventDefault();first.focus(); }
  }
});
