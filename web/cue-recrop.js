/* Single-cue review: normalized canvas selection, preview, explicit publication. */
async function openCueRecrop(cue) {
  const projectId = currentProjectId;
  const dialog = document.createElement('dialog');
  dialog.style.cssText = 'width:min(1000px,94vw);max-height:92vh;overflow:auto;background:var(--bg-surface);color:var(--text-main);border:1px solid var(--border-subtle);border-radius:12px;padding:20px';
  dialog.innerHTML = `<h3>Cắt lại / OCR riêng câu ${cue.index}</h3>
    <p>Kéo trên ảnh để chọn vùng chữ. Chỉ câu này được cập nhật khi bạn bấm lưu.</p>
    <label>Thời điểm (giây) <input data-time type="number" step="0.01" style="width:120px"></label>
    <button data-frame class="btn">Xem khung hình</button>
    <button data-expand class="btn">Nới vùng cắt</button>
    <canvas style="width:100%;height:auto;touch-action:none;cursor:crosshair;display:block;margin:12px 0"></canvas>
    <button data-read class="btn primary">OCR riêng câu này</button>
    <p data-status role="status"></p><img data-crop style="max-width:100%;display:none" alt="Ảnh cắt mới">
    <p>Chữ hiện tại: <span data-old></span></p>
    <label>Kết quả mới (có thể sửa trước khi lưu) <input data-text style="width:100%"></label>
    <p>Đổi nội dung sẽ yêu cầu dịch và tạo giọng lại riêng câu này.</p>
    <button data-save class="btn primary" disabled>Dùng kết quả này</button>
    <button data-close class="btn">Đóng</button>`;
  document.body.append(dialog);
  dialog.showModal();
  const el = name => dialog.querySelector(`[data-${name}]`);
  const canvas = dialog.querySelector('canvas'), ctx = canvas.getContext('2d');
  const image = new Image();
  let roi = {x:0, y:0.75, width:1, height:0.25}, anchor, token, busy=true;
  el('read').disabled=true;
  el('old').textContent = cue.corrected_text || cue.text;
  el('time').value = ((cue.start + cue.end) / 2).toFixed(3);
  el('time').min = cue.start;
  el('time').max = Math.max(cue.start, cue.end - 0.001);
  function invalidate() { token=null; el('save').disabled=true; }
  function draw() {
    ctx.drawImage(image,0,0,canvas.width,canvas.height);
    ctx.strokeStyle='#00e0b0'; ctx.lineWidth=Math.max(2,canvas.width/500);
    ctx.strokeRect(roi.x*canvas.width,roi.y*canvas.height,roi.width*canvas.width,roi.height*canvas.height);
  }
  function point(event) {
    const rect=canvas.getBoundingClientRect();
    return {x:Math.max(0,Math.min(1,(event.clientX-rect.left)/rect.width)),
            y:Math.max(0,Math.min(1,(event.clientY-rect.top)/rect.height))};
  }
  canvas.onpointerdown = event => { if(busy || !image.complete)return; anchor=point(event);canvas.setPointerCapture(event.pointerId);invalidate(); };
  canvas.onpointermove = event => {
    if(!anchor)return;
    const end=point(event);
    roi={x:Math.min(anchor.x,end.x),y:Math.min(anchor.y,end.y),width:Math.abs(end.x-anchor.x),height:Math.abs(end.y-anchor.y)};
    draw();
  };
  canvas.onpointerup = canvas.onpointercancel = () => {anchor=null;};
  el('expand').onclick = () => {
    if(busy)return;
    const right=Math.min(1,roi.x+roi.width+0.04), bottom=Math.min(1,roi.y+roi.height+0.02);
    roi.x=Math.max(0,roi.x-0.04);roi.y=Math.max(0,roi.y-0.02);
    roi.width=right-roi.x;roi.height=bottom-roi.y;invalidate();draw();
  };
  async function frame() {
    busy=true;el('read').disabled=true;el('frame').disabled=true;el('time').disabled=true;
    invalidate(); el('status').textContent='Đang lấy khung hình…';
    try {
      const loaded = new Image();
      loaded.src=`/api/projects/${projectId}/frame?t=${Number(el('time').value)}`;
      await loaded.decode();
      image.src=loaded.src; await image.decode();
      canvas.width=image.naturalWidth;canvas.height=image.naturalHeight;draw();
      el('read').disabled=false;
      el('status').textContent='Kéo chọn vùng chứa đủ cả câu, chừa khoảng trống hai bên.';
    } catch(err) {el('status').textContent='Không lấy được khung hình: '+err.message;}
    finally {busy=false;el('frame').disabled=false;el('time').disabled=false;}
  }
  el('time').oninput = () => {invalidate();el('read').disabled=true;};
  el('frame').onclick = async () => {if(busy)return;await frame();};
  el('read').onclick = async () => {
    if(busy)return;
    busy=true;invalidate();el('read').disabled=true;el('frame').disabled=true;el('time').disabled=true;
    el('status').textContent='Đang OCR câu này…';
    try {
      const result=await API.request('POST',`/api/projects/${projectId}/cues/${cue.index}/recrop`,{timestamp:Number(el('time').value),roi});
      token=result.token;el('text').value=result.text;
      el('crop').src=result.image;el('crop').style.display='block';
      el('status').textContent=`Độ tin cậy OCR ${result.confidence}% — kiểm tra ảnh và chữ trước khi lưu.`;
      el('save').disabled=false;
    } catch(err) {el('status').textContent=err.message;}
    finally {busy=false;el('read').disabled=false;el('frame').disabled=false;el('time').disabled=false;}
  };
  el('save').onclick = async () => {
    if(!token || busy)return;
    busy=true;el('save').disabled=true;
    try {
      await API.request('POST',`/api/projects/${projectId}/cues/${cue.index}/recrop/apply`,{token,text:el('text').value});
      dialog.close();
      if(currentProjectId===projectId){await loadCues(true);await refreshProjectData();}
    } catch(err) {el('status').textContent=err.message;el('save').disabled=false;}
    finally {busy=false;}
  };
  el('close').onclick=()=>{if(!busy)dialog.close();};
  dialog.addEventListener('cancel',event=>{if(busy)event.preventDefault();});
  dialog.addEventListener('close',()=>dialog.remove());
  try {
    const rois=await API.request('GET',`/api/projects/${projectId}/roi`);
    const primary=rois.find(r=>r.enabled && r.type==='chinese_subtitle');
    if(primary)roi={x:primary.x,y:primary.y,width:primary.width,height:primary.height};
  } catch(err) {el('status').textContent=err.message;}
  await frame();
}
