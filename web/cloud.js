/* Google Drive archive; API responses never include credentials/session URLs. */
(() => {
  const $ = id => document.getElementById(id);
  const modal = $('cloud-modal');
  let timer, loading = false, activeTask = null, lastOAuthMessage = null;
  const message = text => { $('cloud-message').textContent = text; };
  const bytes = value => {
    if(value == null) return 'chưa có';
    const number=Number(value),unit=number>=1e9 ? [1e9,'GB'] : number>=1e6 ? [1e6,'MB'] : number>=1e3 ? [1e3,'KB'] : [1,'byte'];
    return (number/unit[0]).toLocaleString('vi-VN',{maximumFractionDigits:2})+' '+unit[1];
  };
  const states = {PENDING:'Chờ tải',UPLOADING:'Đang tải',INTERRUPTED:'Bị gián đoạn — có thể tiếp tục',PAUSED:'Đã dừng',FAILED:'Chưa hoàn tất',VERIFIED:'Đã xác minh tệp'};
  async function load() {
    if(loading || modal.style.display==='none') return;
    loading=true;
    try {
      const [status,library]=await Promise.all([API.request('GET','/api/cloud/drive'),API.request('GET','/api/cloud/uploads')]);
      $('cloud-account').textContent=status.connected ? `Đã kết nối: ${status.account || status.display_name || 'Google Drive'}` : status.connecting ? 'Đang chờ bạn đăng nhập Google…' : 'Chưa kết nối Google Drive.';
      $('cloud-connect').disabled=!status.configured || status.connecting;
      $('cloud-disconnect').disabled=!status.connected && !status.connecting;
      $('cloud-disconnect').textContent=status.connecting ? 'Hủy đăng nhập' : 'Ngắt kết nối trên máy';
      $('cloud-disconnect').dataset.connecting=String(status.connecting);
      $('cloud-upload').disabled=!status.connected;
      $('cloud-auto-upload').disabled=!status.connected;
      $('cloud-auto-upload').checked=status.auto_upload;
      const setup=$('cloud-setup');
      if(!setup.dataset.initialized) {
        setup.open=!status.configured;
        setup.dataset.initialized='true';
      }
      if(status.message && status.message!==lastOAuthMessage) { message(status.message); lastOAuthMessage=status.message; }
      const selected=$('cloud-project').value;
      $('cloud-project').innerHTML='<option value="">Chọn video</option>'+library.projects.map(p=>`<option value="${escapeHtml(p.id)}">${escapeHtml(p.title)} (${escapeHtml(p.id)})</option>`).join('');
      $('cloud-project').value=selected;
      const names=new Map(library.projects.map(p=>[p.id,p.title]));
      $('cloud-library').innerHTML=library.uploads.length ? '<div class="usage-scroll"><table><thead><tr><th>Video</th><th>Trạng thái</th><th>Dung lượng / tiến độ</th><th>Thao tác</th></tr></thead><tbody>'+library.uploads.map(row=>`<tr><td>${escapeHtml(names.get(row.project_id)||row.project_id)}<br><small>${escapeHtml(row.project_id)}</small></td><td>${escapeHtml(states[row.status]||row.status)}<br><small>${escapeHtml(row.error||'')}</small></td><td>${bytes(row.file_size)} · ${Math.floor(row.uploaded_bytes*100/row.file_size)}%<br>${row.active_task ? escapeHtml(row.active_task.stage) : ''}</td><td>${row.web_url ? `<a href="${escapeHtml(row.web_url)}" target="_blank" rel="noopener noreferrer">Mở / tải từ Drive</a>` : ''} ${row.status==='VERIFIED' && !row.local_removed ? `<button class="btn small" data-cloud-remove="${escapeHtml(row.id)}" data-project="${escapeHtml(row.project_id)}">Giải phóng MP4</button>` : row.local_removed ? 'Đã dọn MP4 trên máy' : `<button class="btn small" data-cloud-resume="${escapeHtml(row.project_id)}">Tiếp tục tải</button>`}</td></tr>`).join('')+'</tbody></table></div>' : '<p class="muted">Chưa có video trong kho cloud. Chọn video đã đạt QA để tải lên.</p>';
      if(activeTask) {
        const task=await API.getTask(activeTask);
        $('cloud-task').textContent=`${task.stage} · ${task.progress}%`;
        if(['DONE','FAILED','CANCELLED'].includes(task.state)) {
          message(task.state==='DONE' ? 'Thao tác hoàn tất. Xem trạng thái tệp bên dưới.' : task.error_message||'Tác vụ chưa hoàn tất; giữ nguyên bản trên máy.');
          activeTask=null;
        }
      }
      $('cloud-cancel-task').hidden=!activeTask;
    } catch(err) {
      $('cloud-connect').disabled=true;
      $('cloud-upload').disabled=true;
      $('cloud-account').textContent='Chưa đọc được trạng thái kết nối.';
      message(err.message==='Not Found'
        ? 'Backend đang chạy chưa có API Google Drive. Hãy khởi động lại ứng dụng rồi tải lại trang (Ctrl+F5). Tệp JSON chưa được kiểm tra ở bước này.'
        : err.message);
    }
    finally { loading=false; }
  }
  async function action(fn) { try { await fn(); await load(); } catch(err) { message(err.message); } }
  async function upload(project) {
    if(!project) throw new Error('Hãy chọn video đã đạt QA kỹ thuật.');
    const task=await API.request('POST',`/api/cloud/projects/${encodeURIComponent(project)}/upload`,{});
    activeTask=task.task_id; message('Đã đưa vào hàng đợi. Bạn có thể đóng bảng, tác vụ vẫn chạy.');
  }
  $('btn-open-cloud').addEventListener('click',()=>{modal.style.display='flex';$('cloud-close').focus();load();clearInterval(timer);timer=setInterval(load,5000);});
  $('cloud-close').addEventListener('click',()=>{modal.style.display='none';clearInterval(timer);$('btn-open-cloud').focus();});
  $('cloud-refresh').addEventListener('click',load);
  $('cloud-import').addEventListener('click',()=>action(async()=>{
    const file=$('cloud-config-file').files[0];
    if(!file || file.size>65536) throw new Error('Chọn tệp OAuth JSON dưới 64 KB.');
    let document;
    try { document=JSON.parse(await file.text()); } catch { throw new Error('Không đọc được JSON. Hãy chọn tệp OAuth Desktop app tải từ Google.'); }
    await API.request('POST','/api/cloud/drive/config',{document});
    $('cloud-config-file').value=''; message('Đã lưu cấu hình mã hóa. Bấm Kết nối Google Drive.');
  }));
  $('cloud-connect').addEventListener('click',()=>{
    const popup=window.open('about:blank','_blank');
    if(popup) popup.opener=null;
    action(async()=>{
      try {
        const result=await API.request('POST','/api/cloud/drive/connect',{});
        if(popup) popup.location=result.authorization_url;
        else { message('Trình duyệt chặn tab đăng nhập. Hãy cho phép cửa sổ bật lên, rồi thử lại sau khi phiên chờ kết thúc.'); }
      } catch(err) { if(popup) popup.close(); throw err; }
    });
  });
  $('cloud-disconnect').addEventListener('click',()=>action(async()=>{
    if($('cloud-disconnect').dataset.connecting==='true') {
      await API.request('POST','/api/cloud/drive/cancel',{});
      message('Đã hủy phiên đăng nhập. Bạn có thể đổi JSON hoặc kết nối lại.');
      return;
    }
    await API.request('POST','/api/cloud/drive/disconnect',{});
    message('Đã xóa thông tin đăng nhập trên máy. Tệp Drive được giữ nguyên; có thể thu hồi quyền trong tài khoản Google.');
  }));
  $('cloud-auto-upload').addEventListener('change',()=>action(()=>API.request('POST','/api/cloud/drive/policy',{auto_upload:$('cloud-auto-upload').checked})));
  $('cloud-upload').addEventListener('click',()=>action(()=>upload($('cloud-project').value)));
  $('cloud-cancel-task').addEventListener('click',()=>action(()=>API.request('POST',`/api/cloud/tasks/${encodeURIComponent(activeTask)}/cancel`,{})));
  $('cloud-quota').addEventListener('click',()=>action(async()=>{
    const data=await API.request('GET','/api/cloud/drive/quota'),q=data.storage_quota;
    $('cloud-quota-text').textContent=q ? `Drive báo đã dùng ${bytes(q.usage)} / hạn mức ${bytes(q.limit)}. Đo lúc ${new Date(data.measured_at*1000).toLocaleTimeString('vi-VN')}.` : 'Drive chưa trả dung lượng.';
  }));
  $('cloud-library').addEventListener('click',event=>{
    const button=event.target.closest('button'); if(!button) return;
    if(button.dataset.cloudResume) action(()=>upload(button.dataset.cloudResume));
    if(button.dataset.cloudRemove && confirm('Xóa bản MP4 thành phẩm trên máy sau khi kiểm tra lại checksum và bản Google Drive? Giữ nguồn, phụ đề và cache.')) action(async()=>{
      const task=await API.request('POST',`/api/cloud/projects/${encodeURIComponent(button.dataset.project)}/remove-local/${encodeURIComponent(button.dataset.cloudRemove)}`,{});
      activeTask=task.task_id;
    });
  });
  modal.addEventListener('keydown',event=>{
    if(event.key==='Escape') $('cloud-close').click();
    if(event.key==='Tab') {
      const controls=[...modal.querySelectorAll('button,input,select,a,summary')].filter(x=>!x.disabled && x.getClientRects().length);
      const first=controls[0],last=controls[controls.length-1];
      if(event.shiftKey && document.activeElement===first) {event.preventDefault();last.focus();}
      else if(!event.shiftKey && document.activeElement===last) {event.preventDefault();first.focus();}
    }
  });
})();
