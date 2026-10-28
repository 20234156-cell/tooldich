/* Workspace presentation. Existing workflow controls and endpoints stay in app.js. */
(() => {
  const $=id=>document.getElementById(id);
  const number=v=>v==null ? 'chưa có' : Number(v).toLocaleString('vi-VN');
  let generation=0;
  const formatTokens=requests=>{
    if(requests.input_tokens==null || requests.output_tokens==null) return 'chưa có';
    return number(requests.input_tokens+requests.output_tokens);
  };
  function appearance(value) {
    document.documentElement.dataset.theme=value==='mint-dark' ? 'mint-dark':'mint';
    $('theme-toggle').querySelector('span').textContent=value==='mint-dark' ? 'Giao diện sáng':'Giao diện tối';
  }
  try {appearance(localStorage.getItem('duanlaylink_appearance_v2'));} catch {appearance('mint');}
  $('theme-toggle').addEventListener('click',()=>{
    const value=document.documentElement.dataset.theme==='mint' ? 'mint-dark':'mint';
    appearance(value);try {localStorage.setItem('duanlaylink_appearance_v2',value);} catch {}
  });
  function mobile(open) {
    document.body.classList.toggle('sidebar-open',open);
    $('sidebar-scrim').hidden=!open;
    $('sidebar-menu').setAttribute('aria-expanded',String(open));
    if(open) $('app-sidebar').querySelector('button').focus();
  }
  $('sidebar-menu').addEventListener('click',()=>mobile(!document.body.classList.contains('sidebar-open')));
  $('sidebar-scrim').addEventListener('click',()=>mobile(false));
  $('sidebar-collapse').addEventListener('click',()=>{
    const collapsed=document.documentElement.classList.toggle('sidebar-collapsed');
    $('sidebar-collapse').setAttribute('aria-expanded',String(!collapsed));
    $('sidebar-collapse').title=collapsed?'Mở rộng thanh bên':'Thu gọn thanh bên';
    try {localStorage.setItem('duanlaylink_sidebar_collapsed',String(collapsed));} catch {}
  });
  try {if(localStorage.getItem('duanlaylink_sidebar_collapsed')==='true') {document.documentElement.classList.add('sidebar-collapsed');$('sidebar-collapse').setAttribute('aria-expanded','false');}} catch {}
  $('sidebar-studio').addEventListener('click',()=>{if(currentProjectId) showView('detail');});
  window.addEventListener('workspace-view',()=>mobile(false));
  $('app-sidebar').addEventListener('click',event=>{if(event.target.closest('button')?.id!=='theme-toggle') mobile(false);});
  document.addEventListener('keydown',event=>{
    if(event.key==='Escape' && document.body.classList.contains('sidebar-open')) {mobile(false);$('sidebar-menu').focus();}
    if(event.key==='Tab' && document.body.classList.contains('sidebar-open')) {
      const controls=[...$('app-sidebar').querySelectorAll('button')].filter(x=>!x.disabled && x.getClientRects().length);
      const first=controls[0],last=controls[controls.length-1];
      if(event.shiftKey && document.activeElement===first) {event.preventDefault();last.focus();}
      else if(!event.shiftKey && document.activeElement===last) {event.preventDefault();first.focus();}
    }
  });
  $('btn-open-guide').addEventListener('click',()=>{$('guide-modal').style.display='flex';$('guide-close').focus();});
  $('guide-close').addEventListener('click',()=>{$('guide-modal').style.display='none';$('btn-open-guide').focus();});
  $('guide-modal').addEventListener('keydown',event=>{if(event.key==='Escape') $('guide-close').click();if(event.key==='Tab'){event.preventDefault();$('guide-close').focus();}});
  const dialogs=[...document.querySelectorAll('.modal-backdrop')];
  let restoreFocus=null;
  document.addEventListener('click',event=>{
    const button=event.target.closest('button');
    if(button && ['btn-open-keys','btn-open-cloud','btn-open-usage','btn-open-guide'].includes(button.id)) restoreFocus=button;
  },true);
  const dialogState=()=>{
    const opened=dialogs.some(dialog=>dialog.style.display!=='none');
    for(const target of [$('app-sidebar'),document.querySelector('header.topbar'),$('main-content')]) target.inert=opened;
    document.body.style.overflow=opened?'hidden':'';
    if(!opened && restoreFocus) { (matchMedia('(max-width:800px)').matches?$('sidebar-menu'):restoreFocus).focus();restoreFocus=null; }
  };
  for(const dialog of dialogs) {
    new MutationObserver(dialogState).observe(dialog,{attributes:true,attributeFilter:['style']});
    dialog.addEventListener('keydown',event=>{
      if(event.key!=='Tab')return;
      const focusable=[...dialog.querySelectorAll('button,input,select,textarea,a[href],summary')].filter(x=>!x.disabled && x.getClientRects().length);
      if(!focusable.length)return;
      const first=focusable[0],last=focusable[focusable.length-1];
      if(event.shiftKey && document.activeElement===first){event.preventDefault();last.focus();}
      else if(!event.shiftKey && document.activeElement===last){event.preventDefault();first.focus();}
    });
  }
  $('search-input').addEventListener('input',()=>{
    const value=$('search-input').value.trim().toLocaleLowerCase('vi');
    if(value && !DOM.views.dashboard.classList.contains('active')) showView('dashboard');
    document.querySelectorAll('#project-rows tr[data-id]').forEach(row=>{row.hidden=!row.textContent.toLocaleLowerCase('vi').includes(value);});
  });
  const statIds=['stat-total','stat-verified','stat-processing','dash-keys','dash-requests','dash-tokens','dash-latency','dash-cloud'];
  let hydrated=false;
  const previous=new Map();
  async function overview() {
    const requestId=++generation;
    $('dashboard-stats').setAttribute('aria-busy','true');
    if(!hydrated) statIds.forEach(id=>$(id).classList.add('is-skeleton'));
    try {
      const data=await API.request('GET','/api/dashboard');
      if(requestId!==generation)return;
      const p=data.projects,r=data.requests;
      for(const [id,value] of [['stat-total',number(p.total)],['stat-verified',number(p.technical_verified)],['stat-processing',number(p.active)],['dash-keys',number(data.keys.total)],['dash-requests',number(r.today)],['dash-tokens',formatTokens(r)],['dash-latency',r.mean_latency_ms==null?'chưa có':(r.mean_latency_ms/1000).toLocaleString('vi-VN',{maximumFractionDigits:2})+'s'],['dash-cloud',number(data.cloud.verified_versions)]]) {
        const node=$(id),old=previous.get(id);
        node.textContent=value;
        if(old!==undefined && old!==value && ['dash-requests','dash-tokens','dash-latency'].includes(id)) window.workspaceFlash(node);
        if(id==='stat-processing') {
          node.classList.toggle('has-active-tasks',p.active>0);
          if(old==='0' && p.active>0) window.workspaceFlash(node.closest('.stat-card'));
        }
        previous.set(id,value);
      }
      $('dashboard-summary-status').textContent=`Số liệu toàn bộ không gian · cập nhật ${new Date(data.measured_at).toLocaleTimeString('vi-VN',{timeZone:'Asia/Bangkok'})} (UTC+7). Token là tổng phần API đã báo, chưa gồm suy luận.`;
      const groups=[['Chuẩn bị',['CREATED','SOURCE_READY','MEDIA_READY','ROI_READY']],['Phụ đề & dịch',['OCR_READY','SOURCE_REVIEWED','TRANSLATED','TRANSLATION_REVIEWED']],['Giọng & render',['VOICE_READY','RENDERED']],['QA kỹ thuật',['VERIFIED']]];
      const stageMarkup=groups.map(([label,stages])=>{
        const count=stages.reduce((sum,state)=>sum+(p.stages[state]||0),0);
        return `<div class="stage-summary"><span>${label}</span><strong>${number(count)}</strong><div class="stage-track"><i style="width:${p.total?count/p.total*100:0}%"></i></div></div>`;
      }).join('');
      const stageNodes=$('dashboard-stages').querySelectorAll('.stage-summary');
      if(stageNodes.length===groups.length) groups.forEach(([,states],index)=>{
        const count=states.reduce((sum,state)=>sum+(p.stages[state]||0),0);
        stageNodes[index].querySelector('strong').textContent=number(count);
        stageNodes[index].querySelector('i').style.width=`${p.total?count/p.total*100:0}%`;
      }); else $('dashboard-stages').innerHTML=stageMarkup;
      $('dashboard-attention').innerHTML=[['Cue chưa có bản dịch',number(p.missing_translation)],['Cue chưa duyệt nội dung',number(p.unreviewed_cues)],['Dự án chưa có transcript DB',number(p.missing_transcripts)],['Kết nối Google Drive',data.cloud.connected?'Đã kết nối':'Chưa kết nối']].map(([label,value])=>`<div class="attention-row"><span>${label}</span><strong>${value}</strong></div>`).join('');
      $('dash-keys').nextElementSibling.textContent=`${data.keys.healthy} khỏe · ${data.keys.waiting} chờ · ${data.keys.invalid} vô hiệu`;
      $('dash-tokens').nextElementSibling.textContent=`Input ${number(r.input_tokens)} / output ${number(r.output_tokens)} · dữ liệu ${r.input_tokens_reported_requests}/${r.today} lượt`;
      hydrated=true;
      return true;
    } catch(error) {
      if(requestId!==generation)return;
      for(const id of ['stat-total','stat-verified','stat-processing','dash-keys','dash-requests','dash-tokens','dash-latency','dash-cloud']) $(id).textContent='chưa có';
      $('dashboard-summary-status').textContent='Chưa đọc được tổng quan từ backend. Nếu vừa cập nhật ứng dụng, hãy khởi động lại server. '+error.message;
      $('dashboard-stages').innerHTML='<p class="muted">Chưa đọc được trạng thái dự án.</p>';
      $('dashboard-attention').innerHTML='<p class="muted">Chưa đọc được dữ liệu cần duyệt.</p>';
      previous.clear();
      $('stat-processing').classList.remove('has-active-tasks');
      return false;
    } finally {
      if(requestId===generation) {
        $('dashboard-stats').removeAttribute('aria-busy');
        statIds.forEach(id=>$(id).classList.remove('is-skeleton'));
      }
    }
  }
  window.addEventListener('workspace-projects-loaded',()=>{
    $('search-input').dispatchEvent(new Event('input'));
    if(!$('btn-refresh').disabled) overview();
  });
  window.refreshWorkspaceOverview=overview;
  overview();
})();
