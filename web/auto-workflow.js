// Auto survives a review pause and browser/server reopening. No work on page open.
(()=>{
  let project=null, generation=0, rendered=null, taskIdentity=null, failureNote=null, stageLine=null;
  let pendingResume = null;
  let projectIdentity = null;
  const shortcut = document.querySelector('#btn-resume-auto');
  const card=document.createElement('section');card.className='card';card.hidden=true;
  card.setAttribute('aria-live','polite');card.id='auto-workflow-panel';
  document.getElementById('detail-error')?.after(card);
  async function refresh(id){
    if(project!==id){
      project=id;rendered=null;taskIdentity=null;projectIdentity=null;failureNote=null;stageLine=null;card.hidden=true;card.replaceChildren();
      if (shortcut) shortcut.hidden = true;
    }
    const request=++generation;
    try{
      const info=await API.request('GET',`/api/projects/${id}/auto-workflow`);
      if(request!==generation||project!==id)return;
      if(!info||typeof info!=='object'||Array.isArray(info))throw Error('Trạng thái Auto không hợp lệ');
      failureNote?.remove();failureNote=null;
      const identity=JSON.stringify(info);
      if(identity===rendered)return;
      rendered=identity;
      card.replaceChildren();
      stageLine = null;
      if (shortcut) shortcut.hidden = true;
      if (info.state === 'verified') {
        const title = document.createElement('h3');
        title.textContent = 'Dự án đã qua QA kỹ thuật';
        const verify = document.createElement('button');
        verify.className = 'btn small';
        verify.textContent = 'Mở Nghiệm thu';
        verify.onclick = () => document.querySelector('[data-studio="verify"]')?.click();
        card.append(title, verify);
        card.hidden = false;
        return;
      }
      if(!info.configured&&info.state!=='invalid'){
        const title=document.createElement('h3');title.textContent='Tiếp tục bằng Auto';
        const help=document.createElement('p');help.textContent='Lượt cũ/chế độ từng bước chưa lưu cấu hình Auto. Chọn giọng, bộ dịch và kiểu sub rồi bấm Chạy tự động một lần. Những lần dừng để duyệt OCR sau đó sẽ tự tiếp tục khi bạn phê duyệt.';
        const start=document.createElement('button');start.className='btn primary';start.textContent='Chạy Auto từ bước hiện tại';start.onclick=()=>DOM.btnRunAll.click();
        card.append(title,help,start);card.hidden=false;return;
      }
      const title=document.createElement('h3');
      const labels={running:'Auto đang chạy',queued:'Auto đang chờ',needs_review:'Auto tạm dừng để duyệt OCR',ready_to_resume:'Auto sẵn sàng chạy tiếp',failed:'Auto cần xử lý lỗi',paused:'Auto đã tạm dừng',interrupted:'Auto đã gián đoạn',complete:'Auto đã hoàn tất xử lý',invalid:'Chưa đọc được thiết lập Auto'};
      title.textContent=info.state==='failed' && info.error_code==='TIMING_REVIEW_REQUIRED'
        ? 'Auto dừng để kiểm tra timing giọng Việt'
        : labels[info.state]||'Quy trình Auto';card.append(title);
      if (info.state === 'failed' && info.error_code === 'QA_REVIEW_REQUIRED') {
        title.textContent = 'Video đầu ra cần kiểm tra QA';
      }
      if (info.state === 'complete' && info.completion?.needs_review) {
        title.textContent = 'Auto đã xử lý xong, còn nội dung cần duyệt';
      }
      if (info.last_stage) {
        stageLine = document.createElement('p');
        const prefix = info.state === 'running' ? 'Đang thực hiện' : 'Bước ghi nhận gần nhất';
        stageLine.textContent = `${prefix} · ${info.last_stage}`;
        card.append(stageLine);
      }
      const config=document.createElement('p');config.className='muted';
      config.textContent=`Giữ thiết lập Auto: ${info.options?.voice||'Giọng mặc định'} · ${info.options?.rate||'+0%'} · ${info.options?.translation_model||info.options?.translation_provider||'Gemini'}`;card.append(config);
      if (info.options?.skip_uncertain) {
        const policy = document.createElement('p');
        policy.textContent = 'Đã bật trọng tài nguồn Auto. Xem câu phục hồi và lý do loại trong Lịch sử câu.';
        card.append(policy);
      }
      if (info.state === 'complete' && (info.completion?.needs_review || info.completion?.warnings?.length)) {
        const warnings = document.createElement('ul');
        for (const message of (info.completion.warnings || []).slice(0, 5)) {
          const item = document.createElement('li');
          item.textContent = message;
          warnings.append(item);
        }
        const verify = document.createElement('button');
        verify.className = 'btn small';
        verify.textContent = 'Mở kết quả kiểm tra QA';
        verify.onclick = () => document.querySelector('[data-studio="verify"]')?.click();
        card.append(warnings, verify);
      }
      if(!['running','queued','complete'].includes(info.state)){
        const help=document.createElement('p');help.textContent=info.guidance?.message||'';card.append(help);
        if (info.configured && ['failed', 'needs_review'].includes(info.state)) {
          const continuation = document.createElement('p');
          continuation.textContent = 'Nếu bạn dịch lại để xử lý câu lỗi, ứng dụng sẽ tự nối tiếp toàn bộ Auto sau bước dịch. Bạn cũng có thể bấm Tiếp tục toàn bộ Auto bên dưới.';
          card.append(continuation);
        }
        const ocrBlocked = info.state === 'needs_review' && info.error_code === 'OCR_REVIEW_REQUIRED' && !info.source_auto_recovery_available;
        if (Array.isArray(info.review?.timing_clusters)) {
          for (const cluster of info.review.timing_clusters) {
            const detail=document.createElement('details');
            const label=document.createElement('summary');
            label.textContent=`Cụm ${cluster.cum_id}: ${(cluster.cau||[]).length} câu · ${cluster.trang_thai==='dat'?'đạt':'chưa xếp được'}`;
            detail.append(label);
            for (const conflict of cluster.conflicts||[]) {
              const line=document.createElement('p');
              const missing=Number(conflict.missing_seconds);
              line.textContent=`#${conflict.cue_id}: ${conflict.reason==='window_capacity'?'WAV vượt cửa sổ đã xác minh':conflict.reason} · thiếu ${Number.isFinite(missing)?missing.toFixed(3):'chưa xác định'} giây`;
              detail.append(line);
            }
            card.append(detail);
          }
        }
        if (ocrBlocked && info.review?.cue_indices?.length) {
          help.textContent = `Còn ${info.review.cue_indices.length} câu cần đối chiếu. Sau khi xử lý, bấm Phê duyệt OCR để Auto chạy tiếp.`;
        }
        for (const [field, label] of [
          ['timing_cause_cues', 'Câu gây trễ cần kiểm tra trước'],
          ['timing_cues', 'Câu bị lệch thời lượng'],
          ['cue_indices', 'Mã câu cần kiểm tra'],
          ['creator_credit_cues', 'Câu nghi chứa chữ ghi công cần đối chiếu'],
        ]) {
          const ids = info.review?.[field];
          if (!Array.isArray(ids) || !ids.length) continue;
          const line = document.createElement('p');
          const remaining = ids.length > 12 ? ` và ${ids.length - 12} câu khác` : '';
          line.textContent = `${label} · ${ids.slice(0, 12).map(id => `#${id}`).join(', ')}${remaining}`;
          card.append(line);
        }
        if (info.error_message) {
          const details = document.createElement('details');
          const summary = document.createElement('summary');
          summary.textContent = 'Chi tiết lượt dừng trước';
          const error = document.createElement('p');
          error.textContent = [info.error_code, info.error_message].filter(Boolean).join(' · ');
          details.append(summary, error);
          card.append(details);
        }
        const open=document.createElement('button');open.className='btn small';
        open.textContent=ocrBlocked ? 'Mở danh sách câu cần duyệt' : 'Mở bước cần xử lý';
        open.onclick = () => {
          document.querySelector(`[data-studio="${info.guidance?.tab || 'workflow'}"]`)?.click();
          if (ocrBlocked) showOcrReview('pending');
        };
        card.append(open);
        if(info.configured && !ocrBlocked){
          const resume = document.createElement('button');
          resume.className = 'btn primary';
          resume.textContent = 'Tiếp tục toàn bộ Auto';
          resume.disabled = info.busy || pendingResume === id;
          resume.onclick = async () => {
            if (project !== id || info.busy || pendingResume === id) return;
            pendingResume = id;
            resume.disabled = true;
            if (shortcut) shortcut.disabled = true;
            try {
              const task = await API.request('POST', `/api/projects/${id}/auto-workflow/resume`, {});
              if (project === id) refresh(id);
              await monitorAutoContinuation(task, id);
            } catch (error) {
              help.textContent = error.message;
            } finally {
              if (pendingResume === id) pendingResume = null;
              resume.disabled = false;
              if (shortcut && project === id) shortcut.disabled = false;
            }
          };
          card.append(resume);
          if (shortcut) {
            shortcut.hidden = false;
            shortcut.disabled = resume.disabled;
            shortcut.onclick = resume.onclick;
          }
          const current=document.createElement('button');current.className='btn small';current.textContent='Dùng thiết lập Studio hiện tại';current.disabled=Boolean(info.busy);current.onclick=()=>DOM.btnRunAll.click();card.append(current);
        }
      }
      card.hidden=false;
    }catch(error){
      if(request!==generation||project!==id)return;
      taskIdentity=null;
      if(rendered!==null){
        if(!failureNote){failureNote=document.createElement('p');failureNote.setAttribute('role','status');card.append(failureNote);}
        failureNote.textContent='Chưa cập nhật được trạng thái Auto: '+error.message;
        return;
      }
      card.textContent='Chưa kiểm tra được trạng thái Auto: '+error.message;card.hidden=false;
    }
  }
  window.addEventListener('workspace-project-open',event=>refresh(event.detail));
  if (typeof currentProjectId !== 'undefined' && currentProjectId) refresh(currentProjectId);
  window.addEventListener('workspace-auto-refresh',event=>{if(event.detail===project)refresh(project);});
  window.addEventListener('workspace-project-update', event => {
    const snapshot = event.detail;
    if (snapshot?.project_id !== project) return;
    const identity = JSON.stringify([snapshot.stage, snapshot.updated_at]);
    if (identity === projectIdentity) return;
    projectIdentity = identity;
    refresh(project);
  });
  window.addEventListener('workspace-task-update',event=>{
    const task=event.detail;
    if(task?.project_id!==project)return;
    if (stageLine && task.state === 'RUNNING' && ['run-all', 'run_all'].includes(task.action)) {
      stageLine.textContent = `Đang thực hiện · ${task.stage || 'Auto'} · ${task.progress || 0}%`;
    }
    const identity=JSON.stringify([task.task_id,task.state,task.error_code,task.error_message,task.review_resolved===true,task.review_pending]);
    if(identity===taskIdentity)return;
    taskIdentity=identity;refresh(project);
  });
})();
