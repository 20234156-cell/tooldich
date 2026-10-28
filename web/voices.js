// Provider-backed choices: unavailable local models are never advertised as ready.
(async () => {
  const select = document.getElementById('voice-pick');
  if (!select) return;
  try {
    const response = await fetch('/api/voices');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    for (const voice of data.voices || []) {
      const existing = Array.from(select.options).find(option => option.value === voice.id);
      if (existing && existing.dataset?.unavailable !== 'true') continue;
      if (existing) existing.remove();
      const option = document.createElement('option');
      option.value = voice.id;
      option.textContent = voice.name + (voice.provider === 'vieneu' ? ' — VieNeu tại máy' : '');
      select.appendChild(option);
    }
  } catch (error) {
    console.error('Không tải được danh sách giọng bổ sung:', error.message);
  }
  if (typeof window !== 'undefined') window.dispatchEvent(new Event('workspace-voices-ready'));
})();
