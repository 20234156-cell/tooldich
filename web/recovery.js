// Recovery is explicit; opening the project never resubmits provider work.
(()=>{
  let generation=0,lastInterrupted=null;
  const pause=document.createElement("button");pause.className="btn small";pause.textContent="Tạm dừng an toàn";pause.hidden=true;document.getElementById("task-activity")?.append(pause);
  window.addEventListener("workspace-task-update",event=>{const task=event.detail;const user=window.workspaceServerUser;pause.textContent=task?.interrupted_reason_code?'Đang dừng ở điểm an toàn…':'Tạm dừng an toàn';pause.hidden=!task||!["QUEUED","RUNNING"].includes(task.state)||!user||(user.role!=="admin"&&task.owner!==user.username);if(task&&["QUEUED","RUNNING"].includes(task.state))lastInterrupted=null;pause.onclick=async()=>{pause.disabled=true;try{const response=await fetch("/api/tasks/"+task.task_id+"/pause",{method:"POST"});if(!response.ok)throw Error("Chưa tạm dừng được tác vụ.");pause.textContent="Đang dừng ở điểm an toàn…";}catch(error){alert(error.message);}finally{pause.disabled=false;}};});
  const card=document.createElement('section');card.className='card recovery-panel';card.hidden=true;card.setAttribute('aria-live','polite');
  document.getElementById('detail-error')?.after(card);
  const labels={SOURCE_MODIFIED:'Video nguồn đã đổi',TRANSCRIPT_MODIFIED:'Nội dung phụ đề đã đổi',ROI_MODIFIED:'Vùng OCR đã đổi',CONFIG_MISMATCH:'Thiết lập chạy đã đổi',VOICE_MODIFIED:'Giọng đã tạo đã đổi',ENGINE_CONFIG_MODIFIED:'Thiết lập bộ xử lý đã đổi',PIPELINE_VERSION_MODIFIED:'Phiên bản xử lý đã đổi',NO_INPUT_CHECKPOINT:'Lượt cũ chưa có chữ ký đầu vào'};
  async function refresh(project){
    const current=++generation;card.hidden=true;card.replaceChildren();
    if(!window.workspaceServerEnabled||window.workspaceServerUser?.role==='viewer')return;
    try{
      const response=await fetch('/api/projects/'+project+'/recovery');if(!response.ok)return;
      const info=await response.json();if(current!==generation||!info.action||info.reason_codes?.includes('NOT_INTERRUPTED'))return;
      const title=document.createElement('h3');title.textContent='Tác vụ bị gián đoạn — '+info.action;
      const summary=document.createElement('p');summary.textContent=(info.interrupted_reason_code||'')+' · '+(info.retained||'');
      const mode=document.createElement('select');mode.setAttribute('aria-label','Thiết lập khi tiếp tục');
      for(const [value,text] of [['current','Dùng thiết lập hiện tại trong Studio'],['previous','Dùng thiết lập của lượt trước']]){const item=document.createElement('option');item.value=value;item.textContent=text;mode.append(item);}
      const detail=document.createElement('p');detail.textContent=(info.reason_codes||[]).map(code=>labels[code]||code).join('; ')||'Đầu vào đã lưu khớp. Các file/cache vẫn được kiểm tra lại khi sử dụng.';
      const button=document.createElement('button');button.className='btn primary';button.textContent='Kiểm tra và tiếp tục';
      button.onclick=async()=>{
        button.disabled=true;
        try{
          const options={...(info.options||{})};
          if(mode.value==='current'){
            if(['voiceover','run-all','run_all'].includes(info.action)){
              const voice=readVoiceSettings();options.voice=voice.voice;options.rate=voice.rate;options.auto_fit=voice.fit_to_video;options.shorten_to_fit=voice.shorten_to_fit;options.repair_existing=voice.repair_existing;
            }
            if(info.action==='render')options.cfg={...(options.cfg||{}),...collectRenderSettings()};
          }
          async function submit(confirm_changed){
            const result=await fetch('/api/projects/'+project+'/resume',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({options,confirm_changed})});
            return {response:result,data:await result.json()};
          }
          let result=await submit(false);
          if(result.response.status===409&&result.data.detail?.reason_codes){
            const reasons=result.data.detail.reason_codes.map(code=>labels[code]||code).join('; ');
            detail.textContent=reasons;
            if(!confirm(reasons+'\nChạy lại công đoạn với thiết lập đã chọn? Cache đúng nội dung vẫn được kiểm tra để dùng lại.'))return;
            result=await submit(true);
          }
          if(!result.response.ok)throw Error(result.data.message||result.data.detail||'Chưa thể tiếp tục.');
          card.hidden=true;renderTaskActivity(result.data);await waitForTask(result.data);await openProject(project);
        }catch(error){detail.textContent=error.message;}finally{button.disabled=false;}
      };
      card.append(title,summary,detail,mode,button);card.hidden=false;
    }catch{} // Existing project error remains visible if recovery cannot be read.
  }
  window.addEventListener('workspace-project-open',event=>{pause.hidden=true;lastInterrupted=null;refresh(event.detail);});
  window.addEventListener('workspace-task-interrupted',event=>{if(lastInterrupted===event.detail)return;lastInterrupted=event.detail;refresh(event.detail);});
})();
