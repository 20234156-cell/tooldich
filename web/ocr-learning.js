/* Explicit source review. Edits elsewhere remain proposals, never training truth. */
(() => {
  const esc=escapeHtml;
  const signalLabels={low_confidence:'OCR điểm thấp',ocr_disagreement:'Các lượt OCR khác nhau',scene_layout:'Nghi chữ trong cảnh / nhiều vùng chữ',short_text:'Câu ngắn — có thể hợp lệ',confusable_characters:'Có ký tự dễ nhầm',temporal_disagreement:'Chữ đổi theo thời gian',invalid_timing:'Chữ trống / mốc thời gian không hợp lệ'};
  function modal(title) {
    const dialog=document.createElement('dialog');dialog.className='learning-dialog';
    const heading=document.createElement('h2');heading.textContent=title;dialog.append(heading);
    document.body.append(dialog);dialog.addEventListener('close',()=>dialog.remove());
    return dialog;
  }
  const base=id=>`/api/projects/${encodeURIComponent(id)}`;
  window.addEventListener('ocr-learning-warning',()=>window.workspaceToast?.('Câu đã lưu trong DB, nhưng nhật ký học chưa xuất đủ. Mở Đề xuất học để xem lỗi/thử lại.',true));
  window.openOcrLearningReview=async(cue,projectId=currentProjectId,relatedCueId=null)=>{
    if(currentProjectId!==projectId)return;
    const d=modal(`Đối chiếu nguồn câu #${cue.index}`);
    const position=currentCues.findIndex(c=>c.index===cue.index);
    const neighbors=currentCues.filter((c,i)=>Math.abs(i-position)===1);
    d.insertAdjacentHTML('beforeend',`<p>Kiểm tra chữ, vai trò phụ đề và nội dung/thời gian riêng biệt. Kết quả OCR/AI chưa phải nhãn đúng. Ảnh đầy đủ giúp nhận ra chữ trong cảnh.</p>
      <div class="learning-fields">
        <label>Hành động<select data-action><option>CẦN NGƯỜI</option><option>GIỮ</option><option>SỬA</option><option>LOẠI</option><option>CẮT</option><option>GỘP</option></select></label>
        <label data-target-box hidden>Gộp với câu<select data-target>${neighbors.map(c=>`<option value="${c.index}">#${c.index} · ${esc(c.effective_source_text||c.corrected_text||c.text)}</option>`).join('')}</select></label>
        <label data-split-box hidden>Mốc tách (giây)<input data-split type="number" step="0.001" value="${((cue.start+cue.end)/2).toFixed(3)}"></label>
      </div>
      <button type="button" class="btn" data-load>Lấy / làm mới ảnh bằng chứng</button>
      <button type="button" class="btn" data-extra>Thêm ảnh quanh điểm chuyển / fade</button>
      <button type="button" class="btn" data-play>Nghe / xem đoạn nguồn</button>
      <video data-player controls playsinline preload="metadata" hidden style="width:100%;max-height:330px"></video>
      <p data-status role="status"></p><div class="learning-evidence" data-images></div>
      <div class="learning-fields">
        <label>A. Chữ đọc đúng chưa?<select data-text-answer><option>chưa rõ</option><option>đúng</option><option>sai</option></select></label>
        <label>B. Vai trò của chữ?<select data-role-answer><option>chưa rõ</option><option>phụ đề</option><option>chữ cảnh</option><option>trùng</option></select></label>
        <label>C. Nghĩa/người nói/thời gian với âm thanh?<select data-alignment><option>chưa rõ</option><option>khớp</option><option>khác</option><option>không áp dụng</option></select></label>
      </div>
      <p class="muted">Nghe nguồn khi đánh giá C; phụ đề có thể rút gọn, không cần giống nguyên văn ASR. Chưa chắc thì chọn CẦN NGƯỜI.</p>
      <label data-text-box>Chữ đúng / chữ câu thứ nhất<input data-text value="${esc(cue.corrected_text||cue.text||'')}"></label>
      <label data-text2-box hidden>Chữ câu thứ hai<input data-text2></label>
      <label data-merged-box hidden>Chữ đúng của câu sau khi gộp
        <input data-merged placeholder="Nhập một lần nếu hai đoạn là cùng một phụ đề. Để trống để nối hai đoạn chữ.">
      </label>
      <label>Theo luật (nếu đang đối chiếu đề xuất)<select data-rule><option value="">Không gắn luật</option></select></label>
      <label>Lý do quyết định (bắt buộc)<input data-reason placeholder="Ví dụ: đối chiếu ba ảnh, chữ đúng nhưng điểm OCR thấp"></label>
      <label>Nguyên nhân gốc<select data-cause><option>Chưa xác minh nguyên nhân gốc.</option><option>Tiền xử lý làm mất nét chữ</option><option>Nhầm chữ trong cảnh với phụ đề</option><option>OCR nhầm ký tự</option><option>Sai điểm chuyển / gộp tách câu</option><option>Chữ đúng nhưng confidence thấp</option></select></label>
      <label class="learning-confirm"><input type="checkbox" data-crop-match> Chữ chốt xuất hiện đúng trong ảnh crop đang xem (điều kiện riêng để dùng crop làm nhãn nhận dạng; chưa rõ thì bỏ trống).</label>
      <label class="learning-confirm"><input type="checkbox" data-confirm disabled> Tôi đã xem bằng chứng và xác nhận quyết định này. Chỉ xác nhận này mới đủ điều kiện làm nhãn học.</label>
      <div class="project-dialog-actions"><button type="button" class="btn" data-close>Đóng</button><button type="button" class="btn primary" data-save>Ghi quyết định</button></div>`);
    const el=name=>d.querySelector(`[data-${name}]`);
    let evidence=null,busy=false,generation=0;
    function invalidate(){generation++;evidence=null;el('confirm').checked=false;el('confirm').disabled=true;el('crop-match').checked=false;}
    function layout(){
      const action=el('action').value;
      el('target-box').hidden=action!=='GỘP';el('split-box').hidden=action!=='CẮT';
      el('text2-box').hidden=action!=='CẮT';el('text-box').hidden=!['SỬA','CẮT'].includes(action);
      el('merged-box').hidden=action!=='GỘP';
      invalidate();el('status').textContent='Lấy lại bằng chứng cho lựa chọn hiện tại trước khi xác nhận.';
    }
    for(const name of ['action','target','split'])el(name).addEventListener('change',layout);
    if(neighbors.some(neighbor=>neighbor.index===relatedCueId)){
      el('target').value=String(relatedCueId);
      el('action').value='GỘP';
      layout();
    }
    async function load(extra=false){
      if(busy)return;
      invalidate();const request=++generation;busy=true;
      el('load').disabled=el('extra').disabled=true;el('status').textContent='Đang đọc khung hình nguồn…';
      try{
        if(currentProjectId!==projectId)throw Error('Dự án đang mở đã thay đổi.');
        const params=new URLSearchParams({extra:String(extra)}),action=el('action').value;
        if(action==='GỘP')params.set('target_id',el('target').value);
        if(action==='CẮT')params.set('transition',el('split').value);
        const result=await API.request('GET',`${base(projectId)}/cues/${cue.index}/review-evidence?${params}`);
        if(request!==generation||!d.open)return;
        const groups=[result,...(result.target?[result.target]:[])];
        el('images').innerHTML=groups.map(group=>group.frames.map(frame=>{
          const prefix=`${base(projectId)}/review-evidence/${group.token}/`;
          return `<figure><figcaption>Câu #${group.cue_id} · ${frame.seconds.toFixed(3)}s · frame ${frame.frame_index}</figcaption><a href="${prefix+frame.name}" target="_blank" rel="noopener"><img src="${prefix+frame.name}" alt="Khung đầy đủ câu ${group.cue_id}"></a>${frame.crop_name?`<img src="${prefix+frame.crop_name}" alt="Vùng chữ cùng khung">`:''}</figure>`;
        }).join('')).join('');
        await Promise.all([...el('images').querySelectorAll('img')].map(img=>img.decode()));
        if(request!==generation||currentProjectId!==projectId)return;
        evidence=result;el('confirm').disabled=!result.complete;
        const matching=(result.matching_rules||[]).flatMap(x=>x.proposed_actions||[]);
        el('rule').innerHTML='<option value="">Không gắn luật</option>'+matching.map(r=>`<option value="${esc(r.rule_id)}">${esc(r.rule_id)} · ${esc(r.action)}</option>`).join('');
        el('status').textContent=result.complete?'Đã tải ảnh thật. Xem ảnh đầy đủ, crop và nghe đoạn trước khi quyết định.':'Đoạn quá ngắn/chưa đủ ba khung khác nhau: chỉ ghi CẦN NGƯỜI.';
      }catch(error){el('status').textContent=error.message;}
      finally{busy=false;el('load').disabled=el('extra').disabled=false;}
    }
    el('load').onclick=()=>load(false);el('extra').onclick=()=>load(true);
    const player=el('player');
    let stopAt=cue.end;
    player.addEventListener('timeupdate',()=>{if(player.currentTime>=stopAt)player.pause();});
    el('play').onclick=async()=>{
      if(currentProjectId!==projectId)return;
      const target=neighbors.find(c=>c.index===Number(el('target').value));
      const merge=el('action').value==='GỘP' && target;
      const start=merge?Math.min(cue.start,target.start):cue.start;
      stopAt=merge?Math.max(cue.end,target.end):cue.end;
      try {
        if(!player.getAttribute('src')) player.src=`${base(projectId)}/preview?source=original`;
        player.hidden=false;
        if(player.readyState<1) await new Promise((resolve,reject)=>{
          player.addEventListener('loadedmetadata',resolve,{once:true});
          player.addEventListener('error',()=>reject(Error('Không phát được video nguồn.')),{once:true});
        });
        if(!d.open)return;
        player.currentTime=start;
        await player.play();
      } catch(error) {el('status').textContent=error.message;}
    };
    d.addEventListener('close',()=>player.pause());
    el('save').onclick=async()=>{
      if(busy)return;
      if(currentProjectId!==projectId){el('status').textContent='Dự án đã thay đổi.';return;}
      if(!evidence){el('status').textContent='Cần lấy lại ảnh bằng chứng.';return;}
      const action=el('action').value;
      if(action!=='CẦN NGƯỜI'&&!el('confirm').checked){el('status').textContent='Chưa có xác nhận của bạn; có thể ghi CẦN NGƯỜI.';return;}
      busy=true;el('save').disabled=true;
      try{
        await API.request('POST',`${base(projectId)}/cues/${cue.index}/review-decision`,{
          token:evidence.token,action,confirmed:el('confirm').checked,crop_matches_text:el('crop-match').checked,reason:el('reason').value,
          cause:el('cause').value,rule_id:el('rule').value||null,text:el('text').value,text1:el('text').value,text2:el('text2').value,
          merged_text:el('merged').value,
          split_time:Number(el('split').value),target_cue_id:Number(el('target').value),
          answers:{text:el('text-answer').value,role:el('role-answer').value,alignment:el('alignment').value}});
        if(currentProjectId===projectId){await loadCues(true);await refreshProjectData();}
        d.close();window.workspaceToast('Đã ghi quyết định và lịch sử DB.');
      }catch(error){el('status').textContent=error.message;}
      finally{busy=false;el('save').disabled=false;}
    };
    el('close').onclick=()=>{if(!busy)d.close();};
    d.addEventListener('cancel',e=>{if(busy)e.preventDefault();});
    layout();d.showModal();await load();
  };
  document.getElementById('btn-ocr-learning-report').onclick=async()=>{
    const projectId=currentProjectId;if(!projectId)return;
    const d=modal('Đề xuất học — chỉ đề xuất');d.showModal();
    const content=document.createElement('div');content.textContent='Đang đọc lịch sử và luật…';d.append(content);
    const close=document.createElement('button');close.className='btn';close.textContent='Đóng';close.onclick=()=>d.close();d.append(close);
    try{
      const r=await API.request('GET',`${base(projectId)}/learning-review`);
      content.innerHTML=`<p>${r.no_learning_history?'Chưa có lịch sử học. ':''}${r.total_cues} câu · ${r.confirmed_labels} nhãn đã xác nhận · ${r.unconfirmed_proposals} đề xuất · ${r.actual_changes} câu tự đổi.</p><p>Luật đang dùng đã khớp: ${esc(r.active_rule_ids.join(', ')||'chưa có')}. Luật thử nghiệm: ${esc(r.experimental_rule_ids.join(', ')||'chưa có')}.</p>${r.write_errors.map(x=>`<p class="error">${esc(x)}</p>`).join('')}<p>Chưa tự đối chiếu ASR/người nói trong lượt phân tích này. Dùng màn hình bằng chứng để xác nhận riêng.</p><ul>${r.proposals.map(x=>`<li>Câu #${x.cue_id}: ${esc(x.signals.map(s=>signalLabels[s]||s).join(', '))}${x.conflict?' · Luật mâu thuẫn: CẦN NGƯỜI':''} <button type="button" class="btn small" data-review-cue="${x.cue_id}">Mở đối chiếu #${x.cue_id}</button></li>`).join('')}</ul>`;
      content.querySelectorAll('[data-review-cue]').forEach(button=>button.onclick=async()=>{if(currentProjectId!==projectId)return;await loadCues(true);const cue=currentCues.find(c=>c.index===Number(button.dataset.reviewCue));if(cue){d.close();window.openOcrLearningReview(cue,projectId);}});
    }catch(error){content.textContent=error.message;}
  };
  document.getElementById('btn-ocr-history').onclick=async()=>{
    const projectId=currentProjectId;if(!projectId)return;
    const d=modal('Lịch sử sửa nguồn / hoàn tác');d.showModal();
    const content=document.createElement('div');d.append(content);
    const close=document.createElement('button');close.className='btn';close.textContent='Đóng';close.onclick=()=>d.close();d.append(close);
    async function reload(){
      try{
        const r=await API.request('GET',`${base(projectId)}/cue-history`);
        content.innerHTML=r.revisions.length?r.revisions.map(x=>`<article class="learning-history"><strong>${esc(x.action)} · ${esc(x.cue_ids.map(i=>'#'+i).join(', '))}</strong><p>${esc(new Date(x.created_at*1000).toLocaleString('vi-VN'))} · ${esc(x.decided_by)} · ${esc(x.review_status)} · ${x.exported?'đã xuất nhật ký':'nhật ký đang chờ xuất'}</p><p>${esc(x.reason)}</p><p>Trước: ${esc(x.before.map(c=>c.corrected_text||c.text).join(' / '))}</p><p>Sau: ${esc(x.after.map(c=>c.corrected_text||c.text).join(' / ')||'(đã loại)')}</p><button type="button" class="btn small" data-undo="${esc(x.id)}" ${x.can_undo?'':'disabled'}>Hoàn tác nội dung</button></article>`).join(''):'Chưa có lịch sử sửa nguồn được ghi bởi phiên bản này.';
        content.querySelectorAll('[data-undo]').forEach(button=>button.onclick=async()=>{
          if(!confirm('Hoàn tác nội dung nguồn? Cần duyệt/dịch/tạo giọng lại; không khôi phục nhãn QA cũ.'))return;
          button.disabled=true;
          try{await API.request('POST',`${base(projectId)}/cue-history/${button.dataset.undo}/undo`);if(currentProjectId===projectId){await loadCues(true);await refreshProjectData();}await reload();}
          catch(error){window.workspaceToast(error.message,true);button.disabled=false;}
        });
      }catch(error){content.textContent=error.message;}
    }
    await reload();
  };
})();
