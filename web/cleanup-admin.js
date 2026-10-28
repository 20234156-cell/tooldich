// Admin-only dry-run selections use server IDs; no path is ever submitted.
window.renderCleanupAdmin=async function(section,request){
  const el=(tag,text)=>{const node=document.createElement(tag);if(text)node.textContent=text;return node;};
  const status=el('p');status.setAttribute('role','status');section.append(status);
  const reasons={NO_INDEPENDENT_VERIFIED_BACKUP:'Chưa có backup ở kho độc lập đã phục hồi thử; bản D: cùng ổ không đủ điều kiện.',PROJECT_NOT_CLOSED:'Dự án chưa đóng.',ACTIVE_READER:'Đang có người đọc hoặc phát tệp.',ACTIVE_TASK:'Dự án đang xử lý.',REFERENCED:'Dữ liệu vẫn được hệ thống tham chiếu.',STATE_CHANGED:'Trạng thái đã thay đổi.'};
  function button(text,run,enabled=()=>true){const node=el('button',text);node.className='btn small';node.onclick=async()=>{node.disabled=true;try{await run();}catch(error){status.textContent=error.message;}finally{node.disabled=!enabled();}};return node;}
  const candidates=el('div'),selected=new Set();let batch=null;
  const execute=button('Xóa các mục đã chọn',async()=>{
    if(!batch||!selected.size)return;
    if(!confirm(`Xóa vĩnh viễn ${selected.size} mục đã chọn? Server sẽ kiểm tra lại trạng thái và luồng đọc trước khi xóa.`))return;
    const result=await request('/cleanup/execute',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({batch_id:batch.batch_id,candidate_ids:[...selected]})});
    status.textContent=result.results.map(item=>`${item.candidate_id.slice(0,8)}: ${item.result}${item.reason?' · '+(reasons[item.reason]||item.reason):''}`).join(' | ');
    selected.clear();batch=null;candidates.replaceChildren();execute.disabled=true;await history();
  },()=>!!batch&&selected.size>0);execute.disabled=true;
  section.append(el('p','Xem trước giữ hiệu lực 10 phút. Tệp nháp/hủy cần quá hạn 24 giờ; thành phẩm gốc cần kho độc lập và dự án đã đóng. Không tự chọn hoặc tự xóa.'),button('Xem trước dữ liệu có thể dọn',async()=>{
    batch=await request('/cleanup/preview',{method:'POST'});selected.clear();candidates.replaceChildren();execute.disabled=true;
    status.textContent=`${batch.candidates.length} mục · hết hạn ${new Date(batch.expires_at*1000).toLocaleTimeString()}. Chưa xóa tệp nào.`;
    for(const item of batch.candidates){
      const row=el('article');row.className='server-list-row';const label=el('label'),check=el('input');check.type='checkbox';check.disabled=!!item.blocked_reason;check.dataset.candidateId=item.candidate_id;
      check.onchange=()=>{check.checked?selected.add(item.candidate_id):selected.delete(item.candidate_id);execute.disabled=!selected.size;};
      label.append(check,document.createTextNode(` ${item.kind} · ${item.label} · ${(item.expected_size/1048576).toFixed(2)} MiB`));
      label.style.cssText='overflow-wrap:anywhere;display:block';row.append(label,el('p',item.blocked_reason?(reasons[item.blocked_reason]||item.blocked_reason):'Có thể chọn; sẽ kiểm tra lại lúc thực hiện.'));candidates.append(row);
    }
  }),candidates,execute);
  const historyBox=el('div');section.append(historyBox);
  async function history(){const result=await request('/cleanup/history');historyBox.replaceChildren(el('h4','Lịch sử dọn dữ liệu'));
    for(const item of result.records){const line=el('p',`${item.state} · ${item.actor} · ${item.path}${item.error?' · '+JSON.stringify(item.error):''}`);line.style.overflowWrap='anywhere';historyBox.append(line);}return result;}
  const data=await history();
  for(const item of data.stage_resets||[]){const link=el('a',`Biên bản đặt lại ${item.count} tham chiếu · ${item.actor}`);link.href='/api/admin/stage-reset/audits/'+encodeURIComponent(item.id);link.target='_blank';link.rel='noopener';section.append(link);}
  const reset=el('details');reset.append(el('summary','Đối chiếu và đặt lại tham chiếu giọng của công đoạn lỗi'));
  reset.append(el('p','Chỉ cho công đoạn giọng FAILED chưa từng công bố thành công. Giữ chữ và timing; tạo biên bản trước/sau. Không tự tạo lại giọng hoặc xóa tệp.'));
  const projects=el('select');projects.setAttribute('aria-label','Dự án đối chiếu giọng lỗi');for(const project of data.projects){const option=el('option',project.title);option.value=project.id;projects.append(option);}
  const resetStatus=el('p');resetStatus.setAttribute('role','status');projects.onchange=()=>{resetStatus.textContent='Đã đổi dự án; cần đối chiếu lại trước khi xác nhận.';};reset.append(projects,button('Đối chiếu tham chiếu bị thiếu',async()=>{
    const preview=await request('/stage-reset/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id:projects.value})});
    resetStatus.textContent=`${projects.selectedOptions[0]?.textContent||preview.project_id}: có ${preview.count} tham chiếu âm thanh thiếu. Chỉ voice_audio_path và voice_duration được đặt lại; vi_text và thời gian giữ nguyên.`;
    const confirmButton=button(`Xác nhận đặt lại ${preview.count} tham chiếu`,async()=>{
      if(!confirm(`Đặt lại ${preview.count} tham chiếu bị thiếu, giữ nguyên chữ và thời gian? Thao tác có biên bản trước/sau.`))return;
      const result=await request('/stage-reset/execute',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({preview_id:preview.preview_id,confirmed:true})});
      resetStatus.textContent=`Đã đặt lại ${result.count} tham chiếu. Biên bản ${result.audit_id}. Có thể chạy lại sao lưu; giọng cần tạo lại khi bạn muốn dùng.`;confirmButton.remove();
    });resetStatus.append(confirmButton);
  }),resetStatus);section.append(reset);
};
