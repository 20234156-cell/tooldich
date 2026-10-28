(async () => {
  const editor = document.getElementById('voice-dictionary');
  const button = document.getElementById('voice-dictionary-save');
  const status = document.getElementById('voice-dictionary-status');
  if (!editor || !button) return;
  button.disabled = true;
  try {
    const response = await fetch('/api/tts/dictionary');
    if (!response.ok) throw new Error('Không tải được từ điển; kiểm tra phiên bản backend.');
    const data = await response.json();
    editor.value = Object.entries(data.entries).map(([key,value])=>`${key} = ${value}`).join('\n');
    const color = document.getElementById('voice-color');
    if (color) color.disabled = false;
    button.disabled = false;
  } catch (error) { status.textContent = error.message; }
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const entries = Object.create(null);
      for (const line of editor.value.split('\n')) {
        if (!line.trim()) continue;
        const separator = line.indexOf('=');
        if (separator < 1 || !line.slice(separator+1).trim()) throw new Error('Mỗi dòng phải có dạng: từ gốc = cách đọc');
        const key = line.slice(0,separator).trim();
        if (Object.keys(entries).some(k=>k.toLowerCase()===key.toLowerCase())) throw new Error(`Trùng từ: ${key}`);
        entries[key] = line.slice(separator+1).trim();
      }
      const response = await fetch('/api/tts/dictionary',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(entries)});
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail==='string'?result.detail:'Không lưu được từ điển.');
      status.textContent = 'Đã lưu. Tạo lại giọng để áp dụng; chỉ câu có cách đọc thay đổi cần tổng hợp lại.';
    } catch (error) { status.textContent = error.message; }
    finally { button.disabled = false; }
  });
  document.getElementById('voice-color')?.addEventListener('change', event => {
    const rate = document.getElementById('voice-rate');
    if (event.target.value==='news' && rate && rate.value==='0') {
      rate.value='10';rate.dispatchEvent(new Event('input',{bubbles:true}));
    }
  });
})();
