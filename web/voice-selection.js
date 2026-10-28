// Per-project choice and explicit defaults; selection never changes an active job.
(() => {
  const pick = document.getElementById('voice-pick');
  if (!pick) return;
  const status = document.getElementById('voice-selection-status');
  const sample = document.getElementById('voice-sample-audio');
  const listen = document.getElementById('voice-listen-sample');
  const switchButton = document.getElementById('voice-switch-running');
  const prefix = 'duanlaylink_voice_choice_v1:';
  let switching = false, sampleGeneration = 0, audioUrl = null, selectionGeneration = 0;
  const storageKey = key => prefix + (window.workspaceServerEnabled ? (window.workspaceServerUser?.username || '') + ':' : '') + key;
  const read = key => { try { return JSON.parse(localStorage.getItem(storageKey(key))); } catch { return null; } };
  const write = (key, value) => {
    try { localStorage.setItem(storageKey(key), JSON.stringify(value)); } catch {}
    if (window.workspaceServerEnabled && key) fetch('/api/server/preferences/'+encodeURIComponent(key), {
      method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(value),
    }).then(response=>{if(!response.ok)throw Error('Không lưu được giọng trên server.');}).catch(error=>{status.textContent=error.message;});
  };
  const choice = () => ({voice: pick.value, rate: document.getElementById('voice-rate').value,
    color: document.getElementById('voice-color').value});
  function describe() {
    const name = pick.selectedOptions[0]?.textContent || 'Chưa chọn giọng';
    status.textContent = `Giọng cho lượt tiếp theo: ${name}. Lựa chọn được nhớ trong trình duyệt cho từng dự án; tác vụ đang chạy giữ giọng lúc bắt đầu.`;
    if (!['QUEUED','RUNNING'].includes(currentProject?.task?.state)) {
      DOM.btnStepVoiceover.textContent = `Tạo giọng: ${name}`;
      DOM.btnVoiceRunTab.textContent = `Tạo giọng: ${name}`;
    }
  }
  function stopSample() {
    sampleGeneration++;
    sample.pause(); sample.removeAttribute('src'); sample.style.display = 'none';
    if (audioUrl) URL.revokeObjectURL(audioUrl);
    audioUrl = null;
    listen.disabled = false;
  }
  function restore() {
    stopSample();
    const saved = (currentProjectId && read(currentProjectId)) || read('default');
    if (saved?.voice) {
      if (![...pick.options].some(o => o.value === saved.voice)) {
        const option = document.createElement('option');
        option.value = saved.voice;
        option.textContent = `Giọng đã lưu chưa sẵn sàng: ${saved.voice}`;
        option.dataset.unavailable = 'true';
        pick.appendChild(option);
      }
      pick.value = saved.voice;
      const rate = document.getElementById('voice-rate');
      const n = Number(saved.rate);
      rate.value = Number.isFinite(n) ? Math.max(-20, Math.min(30, n)) : 0;
      rate.dispatchEvent(new Event('input'));
      const color = document.getElementById('voice-color');
      if ([...color.options].some(o => o.value === saved.color)) color.value = saved.color;
    } else {
      pick.value = 'vi-VN-NamMinhNeural';
      document.getElementById('voice-rate').value = '0';
      document.getElementById('voice-rate').dispatchEvent(new Event('input'));
      document.getElementById('voice-color').value = 'default';
    }
    describe();
  }
  // Replace temporary unavailable entries when provider discovery completes.
  window.addEventListener('workspace-voices-ready', () => {
    for (const option of [...pick.options]) {
      if (option.dataset.unavailable === 'true') option.remove();
    }
    restore();
  });
  window.addEventListener('workspace-project-open', async () => {
    const generation=++selectionGeneration;
    restore();
    const project = currentProjectId;
    if (!window.workspaceServerEnabled || !project) return;
    try {
      const [own, defaults] = await Promise.all([fetch('/api/server/preferences/'+encodeURIComponent(project)),fetch('/api/server/preferences/default')]);
      if (!own.ok || !defaults.ok) throw Error('Không đọc được lựa chọn giọng trên server.');
      const values = await Promise.all([own.json(),defaults.json()]);
      if (project!==currentProjectId || generation!==selectionGeneration) return;
      for (const [key,value] of [[project,values[0]],['default',values[1]]]) {
        try {localStorage.setItem(storageKey(key),JSON.stringify(value.voice ? value : null));} catch {}
      }
      restore();
    } catch(error) {status.textContent=error.message;}
  });
  window.addEventListener('workspace-voice-start', describe);
  for (const control of [pick, document.getElementById('voice-rate'), document.getElementById('voice-color')]) {
    control.addEventListener('change', () => {
      selectionGeneration++;
      stopSample();
      if (currentProjectId) write(currentProjectId, choice());
      describe();
    });
  }
  document.getElementById('voice-save-default').addEventListener('click', () => {
    if (!pick.value || pick.selectedOptions[0]?.dataset.unavailable === 'true') return;
    write('default', choice());
    status.textContent = 'Đã lưu giọng, tốc độ và màu giọng mặc định cho dự án chưa có lựa chọn riêng.';
  });
  listen.addEventListener('click', async () => {
    if (!pick.value || pick.selectedOptions[0]?.dataset.unavailable === 'true') return;
    stopSample();
    const generation = sampleGeneration, project = currentProjectId;
    const selectedName = pick.selectedOptions[0].textContent;
    listen.disabled = true;
    status.textContent = `Đang tạo mẫu: ${selectedName}…`;
    try {
      const previewPath = '/api/tts/preview' + (project ? '?project_id=' + encodeURIComponent(project) : '');
      const response = await fetch(previewPath, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(readVoiceSettings())});
      if (!response.ok) {
        if (response.status === 404 || response.status === 405) {
          throw Error('Máy chủ đang chạy bản cũ, chưa có chức năng nghe thử. Khi hết tác vụ, hãy đóng cửa sổ máy chủ rồi mở lại start_server.bat; sau đó nhấn Ctrl+F5.');
        }
        const error = await response.json().catch(() => ({}));
        throw Error(typeof error.detail === 'string' ? error.detail : error.message || `Không tạo được mẫu (HTTP ${response.status}).`);
      }
      const blob = await response.blob();
      if (generation !== sampleGeneration || project !== currentProjectId) return;
      audioUrl = URL.createObjectURL(blob); sample.src = audioUrl; sample.style.display = 'block';
      status.textContent = `Mẫu đúng giọng đã chọn: ${selectedName}. Bấm phát để nghe và so sánh các giọng.`;
      sample.play().catch(() => {});
    } catch (error) {
      if (generation === sampleGeneration) status.textContent = error.message;
    } finally { if (generation === sampleGeneration) listen.disabled = false; }
  });
  switchButton.addEventListener('click', async () => {
    if (switching || !currentProjectId || !pick.value || pick.selectedOptions[0]?.dataset.unavailable === 'true') return;
    const project = currentProjectId, options = readVoiceSettings();
    const task = currentProject?.task;
    if (!task || !['RUNNING','QUEUED'].includes(task.state)) {
      await runVoiceoverTask(options); return;
    }
    if (task.action !== 'voiceover') {
      status.textContent = 'Tác vụ hiện tại không phải tạo giọng. Hãy chờ tác vụ này kết thúc.'; return;
    }
    if (!confirm(`Dừng lượt tạo giọng hiện tại và tạo lại toàn bộ bằng ${pick.selectedOptions[0].textContent}? Cache giọng cũ vẫn được giữ. Các câu chưa có cache đúng giọng mới sẽ cần tạo lại.`)) return;
    if (!task.job_id) { status.textContent = 'Không xác định được tác vụ cần dừng. Hãy tải lại trang.'; return; }
    switching = true; switchButton.disabled = true;
    try {
      await API.cancelJob(task.job_id);
      status.textContent = 'Đang chờ lượt giọng cũ dừng thực sự…';
      let latest = await API.getTask(task.task_id);
      while (['QUEUED','RUNNING'].includes(latest.state)) {
        await new Promise(resolve => setTimeout(resolve, 500));
        latest = await API.getTask(task.task_id);
      }
      if (project !== currentProjectId) return;
      await refreshProjectData();
      if (project !== currentProjectId) return;
      await runVoiceoverTask(options);
    } catch (error) { status.textContent = 'Chưa đổi giọng: ' + error.message; }
    finally { switching = false; switchButton.disabled = false; }
  });
  restore();
})();
