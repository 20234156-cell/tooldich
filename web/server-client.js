// Optional server-mode session, per-tab project ownership and administration.
(() => {
  const originalFetch=window.fetch.bind(window);
  let session={enabled:false},readyResolve,leaseProject=null,heartbeat=null;
  const revisions=new Map(),writes=new Map();
  let clientId;
  try { clientId=sessionStorage.getItem('server_client_id'); } catch {}
  if(!clientId) {
    clientId=Array.from(crypto.getRandomValues(new Uint8Array(12)),v=>v.toString(16).padStart(2,'0')).join('');
    try {sessionStorage.setItem('server_client_id',clientId);}catch{}
  }
  const ready=new Promise(resolve=>readyResolve=resolve);
  window.workspaceServerReady=ready;
  window.workspaceServerEnabled=false;
  function headers(extra={}) {return {...extra,'X-Workspace-Client':clientId,...(session.csrf?{'X-CSRF-Token':session.csrf}:{})};}
  async function control(path,options={}) {
    const response=await originalFetch(path.startsWith('/api/')?path:'/api/server'+path,{...options,headers:headers(options.headers),credentials:'same-origin'});
    const data=await response.json();
    if(!response.ok)throw Object.assign(Error(data.message||data.detail||`HTTP ${response.status}`),{status:response.status});
    return data;
  }
  const banner=document.createElement('p');banner.id='server-access-status';banner.setAttribute('role','status');
  banner.style.cssText='display:none;padding:8px 16px;background:#fff3cd;color:#554000;position:sticky;top:0;z-index:900;';
  document.body.prepend(banner);
  function message(text){banner.textContent=text;banner.style.display=text?'block':'none';}
  async function own(project) {
    const expected=revisions.get(project);
    const data=await control('/lease/'+encodeURIComponent(project),{method:'POST',headers:expected===undefined?{}:{'If-Match':String(expected)}});
    if(data.readonly)throw Error('Tài khoản chỉ xem; không được chỉnh sửa.');
    revisions.set(project,data.revision);
    return data;
  }
  window.fetch=async (input,options={})=>{
    const url=new URL(typeof input==='string'?input:input.url,location.href);
    if(url.origin!==location.origin || !url.pathname.startsWith('/api/'))return originalFetch(input,options);
    await ready;
    if(!session.enabled)return originalFetch(input,options);
    const method=(options.method||(input instanceof Request && input.method)||'GET').toUpperCase();
    const changing=!['GET','HEAD','OPTIONS'].includes(method);
    const match=url.pathname.match(/^\/api\/(?:cloud\/)?projects\/([A-Za-z0-9_-]+)(?:\/|$)/);
    const project=match && match[1]!=='batch'?match[1]:null;
    const send=async()=>{
      const requestHeaders=new Headers(options.headers||{});
      for(const [k,v] of Object.entries(headers()))requestHeaders.set(k,v);
      if(changing && project){
        try {await own(project);} catch(err){message(err.message);return new Response(JSON.stringify({message:err.message,error_code:'PROJECT_EDIT_CONFLICT'}),{status:err.status||503,headers:{'Content-Type':'application/json'}});}
        requestHeaders.set('If-Match',String(revisions.get(project)));
      }
      const response=await originalFetch(input,{...options,headers:requestHeaders,credentials:'same-origin'});
      const version=response.headers.get('X-Project-Revision');
      if(changing && project && version!==null && response.ok)revisions.set(project,Number(version));
      if(response.status===401)message('Phiên đăng nhập đã hết hạn. Tải lại trang để đăng nhập; tác vụ máy chủ vẫn tiếp tục.');
      if(response.status===409 || response.status===428) {
        const data=await response.clone().json().catch(()=>({}));
        if(data.message)message(data.message);
      }
      return response;
    };
    if(changing && project){
      const next=(writes.get(project)||Promise.resolve()).catch(()=>{}).then(send);
      writes.set(project,next);return next;
    }
    return send();
  };
  async function projectOpened(event){
    if(!session.enabled)return;
    const project=event.detail;
    if(heartbeat)clearInterval(heartbeat);
    if(leaseProject && leaseProject!==project)control('/lease/'+leaseProject,{method:'DELETE'}).catch(()=>{});
    leaseProject=project;revisions.delete(project);
    const renew=()=>own(project).then(()=>message(session.user?.role==='viewer'?'Chế độ chỉ xem.':'')).catch(err=>message(err.message));
    await renew();heartbeat=setInterval(renew,20000);
  }
  window.addEventListener('workspace-project-open',projectOpened);

  const overlay=document.createElement('div');overlay.id='server-panel-overlay';overlay.className='server-panel-backdrop';overlay.style.display='none';
  overlay.innerHTML='<section class="server-panel modal-card" role="dialog" aria-modal="true" aria-labelledby="server-panel-title"><header class="server-panel-header"><div><span class="server-eyebrow">QUẢN TRỊ HẠ TẦNG</span><h2 id="server-panel-title">Quản lý máy chủ</h2><p id="server-panel-intro" class="server-panel-intro">Đăng nhập để xem tài khoản, máy khách, tác vụ và sao lưu.</p></div><button id="server-panel-close" class="btn small" type="button" aria-label="Đóng bảng quản lý">Đóng</button></header><div id="server-panel-body" class="server-panel-body"></div><p id="server-panel-message" class="server-panel-message" role="status" aria-live="polite"></p></section>';
  document.body.appendChild(overlay);
  const body=overlay.querySelector('#server-panel-body'),note=overlay.querySelector('#server-panel-message');
  const closePanel=overlay.querySelector('#server-panel-close');
  closePanel.onclick=()=>{if(session.user || !session.enabled)overlay.style.display='none';};
  overlay.addEventListener('click',event=>{if(event.target===overlay&&session.user)overlay.style.display='none';});
  window.addEventListener('keydown',event=>{if(event.key==='Escape'&&overlay.style.display!=='none'&&session.user)overlay.style.display='none';});
  const button=document.createElement('button');button.className='btn small server-admin-launch';button.setAttribute('aria-haspopup','dialog');button.setAttribute('aria-label','Quản lý máy chủ');button.title='Quản lý máy chủ';const buttonLabel=document.createElement('span');buttonLabel.textContent='Quản lý máy chủ';button.append(buttonLabel);button.style.display='none';
  (document.querySelector('.topbar-right')||document.body).prepend(button);
  const el=(tag,text)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;return node;};
  function action(label,callback){const node=el('button',label);node.className='btn small';node.onclick=async()=>{node.disabled=true;try{await callback();}catch(err){note.textContent=err.message;}finally{node.disabled=false;}};return node;}
  function panelSection(id,title,description){const section=el('section');section.className='server-section';section.id='server-section-'+id;const header=el('div');header.className='server-section-heading';header.append(el('h3',title));if(description)header.append(el('p',description));section.append(header);body.append(section);return section;}
  function badge(text,tone='neutral'){const item=el('span',text);item.className='server-badge server-badge-'+tone;return item;}
  const invitationParams=new URLSearchParams(location.hash.slice(1));
  let invitation=invitationParams.has('invite')?{token:invitationParams.get('invite'),username:invitationParams.get('username')||''}:null;
  if(invitation)history.replaceState(null,'',location.pathname+location.search);
  async function copyText(text,input){
    try{await navigator.clipboard.writeText(text);note.textContent='Đã sao chép link.';}
    catch{input?.focus();input?.select();note.textContent='Hãy sao chép link trong ô đang chọn (Ctrl+C).';}
  }
  async function loginPanel(){
    overlay.style.display='block';overlay.classList.add('server-auth-mode');body.replaceChildren();note.textContent='';
    const setup=!!session.setup_required,accept=!!invitation&&!setup;
    overlay.querySelector('.server-eyebrow').textContent='DUANLAYLINK · VIDEO WORKSPACE';
    overlay.querySelector('#server-panel-title').textContent=setup?'Thiết lập máy chủ':accept?'Nhận lời mời tham gia':'Đăng nhập DuanLayLink';
    overlay.querySelector('#server-panel-intro').textContent=setup?'Tạo tài khoản quản trị đầu tiên trên máy chủ.':accept?'Đặt mật khẩu riêng để kích hoạt tài khoản được admin cấp.':'Làm việc với video trên máy chủ bằng tài khoản được cấp.';
    closePanel.hidden=!session.user;
    const server=el('p','Máy chủ: '+location.host);server.className='server-auth-host';body.append(server);
    if(setup&&!session.setup_local){body.append(el('p','Mở trình duyệt trên máy chủ để tạo tài khoản quản trị trước.'));return;}
    const form=el('form');form.className='server-login-form';
    const field=(label,input)=>{const wrapper=el('label',label);wrapper.className='server-auth-field';wrapper.append(input);form.append(wrapper);};
    const username=el('input');username.type='text';username.name='username';username.autocomplete='username';username.required=true;username.minLength=3;username.maxLength=40;username.placeholder='Tên đăng nhập';username.pattern='[A-Za-z0-9_-]{3,40}';
    if(accept){username.value=invitation.username;username.readOnly=true;}
    field('Tên đăng nhập',username);
    const password=el('input');password.name='password';password.type='password';password.placeholder=setup||accept?'Tối thiểu 12 ký tự':'Nhập mật khẩu';password.autocomplete=setup||accept?'new-password':'current-password';password.required=true;password.maxLength=200;if(setup||accept)password.minLength=12;
    field(setup||accept?'Mật khẩu mới':'Mật khẩu',password);
    const reveal=el('button','Hiện mật khẩu');reveal.type='button';reveal.className='btn small server-password-toggle';reveal.setAttribute('aria-pressed','false');reveal.onclick=()=>{const show=password.type==='password';password.type=show?'text':'password';reveal.textContent=show?'Ẩn mật khẩu':'Hiện mật khẩu';reveal.setAttribute('aria-pressed',String(show));};form.append(reveal);
    let confirmPassword=null;
    if(setup||accept){confirmPassword=el('input');confirmPassword.type='password';confirmPassword.autocomplete='new-password';confirmPassword.required=true;confirmPassword.placeholder='Nhập lại mật khẩu';field('Xác nhận mật khẩu',confirmPassword);}
    const submit=el('button',setup?'Tạo quản trị viên':accept?'Kích hoạt và đăng nhập':'Đăng nhập');submit.className='btn primary';submit.type='submit';form.append(submit);
    const helper=el('p',accept?'Link mời dùng một lần và hết hạn sau 24 giờ.':setup?'Admin có quyền cấp tài khoản, quản lý phiên, tác vụ và sao lưu.':'Chưa có tài khoản hoặc quên mật khẩu? Liên hệ admin của máy chủ.');helper.className='server-help-text';body.append(form,helper);
    if(accept){const back=el('button','Dùng tài khoản đã có');back.className='btn small';back.onclick=()=>{invitation=null;loginPanel();};body.append(back);}
    form.onsubmit=async event=>{event.preventDefault();note.textContent='';if(confirmPassword&&confirmPassword.value!==password.value){note.textContent='Hai mật khẩu chưa khớp.';confirmPassword.focus();return;}submit.disabled=true;submit.textContent='Đang xác thực…';try{
      const payload={username:username.value,password:password.value};if(accept)payload.token=invitation.token;
      session=await control(setup?'/setup':accept?'/accept-invitation':'/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      invitation=null;window.workspaceServerEnabled=true;window.workspaceServerUser=session.user;password.value='';if(confirmPassword)confirmPassword.value='';overlay.style.display='none';overlay.classList.remove('server-auth-mode');closePanel.hidden=false;note.textContent='';readyResolve();window.dispatchEvent(new Event('workspace-server-login'));
    }catch(err){note.textContent=[404,405].includes(err.status)&&accept?'Máy chủ cần khởi động lại để nạp chức năng lời mời mới.':err.message;}finally{submit.disabled=false;submit.textContent=setup?'Tạo quản trị viên':accept?'Kích hoạt và đăng nhập':'Đăng nhập';}};
  }
  async function sharingPanel(){
    const [sharing,pending]=await Promise.all([control('/sharing'),control('/invitations')]);
    const section=panelSection('sharing','Chia sẻ truy cập','Gửi link đăng nhập hoặc mời người dùng tự đặt mật khẩu. Máy chủ cần bật trong lúc sử dụng.');
    const select=el('select');select.setAttribute('aria-label','Địa chỉ chia sẻ');
    for(const url of sharing.urls){const option=el('option',url);option.value=url;select.append(option);}
    const link=el('input');link.type='text';link.readOnly=true;link.setAttribute('aria-label','Link truy cập máy chủ');link.value=sharing.urls[0]||'';
    select.onchange=()=>{link.value=select.value;};
    section.append(el('p',sharing.public_url?'Truy cập từ xa: '+sharing.public_url:'Chưa cấu hình địa chỉ HTTPS từ xa. Các link LAN bên dưới chỉ dùng cùng mạng.'),select,link,action('Sao chép link đăng nhập',()=>{if(!link.value)throw Error('Chưa có địa chỉ chia sẻ.');return copyText(link.value,link);}));
    if(!sharing.urls.length){section.append(el('p','Chưa có địa chỉ chia sẻ được cho máy khác. Kiểm tra kết nối LAN và khởi động bằng start_small_server.bat.'));}
    const hint=el('p','Link LAN dùng khi cùng mạng. Địa chỉ chưa chứng minh máy khác kết nối được: còn phụ thuộc Windows Firewall. Muốn dùng khác mạng cần cấu hình HTTPS và tên miền được máy chủ cho phép.');hint.className='server-help-text';section.append(hint);
    const form=el('form');form.className='server-invite-form';const name=el('input');name.placeholder='Tên tài khoản được mời';name.setAttribute('aria-label','Tên tài khoản được mời');name.required=true;name.pattern='[A-Za-z0-9_-]{3,40}';
    const role=el('select');role.setAttribute('aria-label','Quyền của người được mời');for(const [value,label] of [['operator','Người dùng · xử lý video'],['viewer','Người xem · chỉ xem']]){const option=el('option',label);option.value=value;role.append(option);}
    const submit=el('button','Tạo link mời 24 giờ');submit.className='btn primary';submit.type='submit';submit.disabled=!sharing.urls.length;form.append(name,role,submit);section.append(form);
    const result=el('div');result.className='server-invite-result';section.append(result);
    form.onsubmit=async event=>{event.preventDefault();submit.disabled=true;note.textContent='';try{const data=await control('/invitations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:name.value,role:role.value})});const url=new URL(select.value);url.hash=new URLSearchParams({invite:data.token,username:data.username}).toString();const output=el('input');output.readOnly=true;output.value=url.href;output.setAttribute('aria-label','Link mời dùng một lần');result.replaceChildren(el('p','Link mới cho '+data.username+' · chỉ hiện tại đây. Sao chép và gửi riêng cho người được mời.'),output,action('Sao chép link mời',()=>copyText(output.value,output)));const item=el('article');item.className='server-list-row';item.append(el('span',data.username+' · '+new Date(data.expires*1000).toLocaleString()),action('Thu hồi',async()=>{await control('/invitations/'+data.id,{method:'DELETE'});item.remove();result.replaceChildren();note.textContent='Đã thu hồi lời mời.';}));section.append(item);}catch(err){note.textContent=err.message;}finally{submit.disabled=false;}};
    for(const item of pending.invitations){const row=el('article');row.className='server-list-row';row.append(el('span',item.username+' · '+item.role+' · hết hạn '+new Date(item.expires*1000).toLocaleString()),action('Thu hồi lời mời',async()=>{await control('/invitations/'+item.id,{method:'DELETE'});row.remove();}));section.append(row);}
  }
  const sizeLabel=value=>Number.isFinite(value)?(value/1024**3).toLocaleString('vi-VN',{maximumFractionDigits:1})+' GB':'Chưa có số liệu';
  function renderInfrastructure(host,error,backups,data){
    const overview=panelSection('overview','Tổng quan vận hành','Số liệu được lấy tại thời điểm tải. Bấm Làm mới để đo lại; không tự gửi yêu cầu tới dịch vụ AI.');
    if(error)overview.append(el('p',error));
    const measured=host?new Date(host.measured_at*1000).toLocaleString():'Chưa có số liệu hạ tầng';
    overview.append(el('p','Lần đo: '+measured));
    const active=data.tasks.filter(task=>['RUNNING','QUEUED'].includes(task.state));
    const failed=data.tasks.filter(task=>['FAILED','INTERRUPTED'].includes(task.state));
    const latestBackup=backups.backups.filter(item=>item.complete||item.status==='VALIDATED_PASS').sort((a,b)=>b.time-a.time)[0];
    const alerts=[...(host?.alerts||[])];
    if(!latestBackup)alerts.push({section:'backups',message:'Chưa có bản sao lưu hoàn tất trong danh sách. Kiểm tra mục Sao lưu.'});
    else if(Date.now()/1000-latestBackup.time>48*3600)alerts.push({section:'backups',message:'Bản sao hoàn tất gần nhất đã quá 48 giờ. Kiểm tra lịch sao lưu.'});
    if(failed.length)alerts.push({section:'tasks',message:failed.length+' tác vụ gần đây thất bại hoặc gián đoạn. Xem lỗi trước khi chạy lại.'});
    overview.append(badge(error?'Thiếu số liệu hạ tầng':alerts.length?'Có cảnh báo cần xem':'Chưa phát hiện cảnh báo tài nguyên',error||alerts.length?'warning':'success'));
    for(const alert of alerts){const row=el('div');row.className='server-alert';const link=action('Xem mục liên quan',()=>{});link.dataset.adminTarget=alert.section;row.append(el('span',alert.message),link);overview.append(row);}
    const stats=el('div');stats.className='server-stat-grid';
    for(const [label,value,detail] of [['CPU',host?host.cpu_percent+'%':'—',host?host.cpu_threads+' luồng xử lý':'Chưa đo'],['RAM trống',sizeLabel(host?.ram.available_bytes),host?'Tổng '+sizeLabel(host.ram.total_bytes):'Chưa đo'],['Tác vụ đang chạy',active.filter(task=>task.state==='RUNNING').length,active.filter(task=>task.state==='QUEUED').length+' đang chờ · trong 50 tác vụ gần nhất'],['Sao lưu gần nhất',latestBackup?new Date(latestBackup.time*1000).toLocaleDateString():'Chưa có',latestBackup?'Bản hoàn tất; xem mức kiểm chứng ở Sao lưu':'Cần kiểm tra']]){const card=el('article');card.className='server-stat';card.append(el('span',label),el('strong',String(value)),el('small',detail));stats.append(card);}overview.append(stats);
    if(host)overview.append(el('p','Backend hoạt động '+Math.floor(host.uptime_seconds/3600)+' giờ '+Math.floor(host.uptime_seconds%3600/60)+' phút · Giới hạn hiện tại: '+host.heavy_task_limit+' công đoạn nặng cùng lúc.'));
    overview.append(el('p','Trạng thái tài nguyên không thay thế kiểm định chất lượng video hoặc kết nối từ máy khách.'));
    const storage=panelSection('storage','Ổ đĩa & tài nguyên','Phân biệt ổ ứng dụng, ổ dữ liệu và kho sao lưu. Hai ký tự ổ đĩa khác nhau có thể cùng một thiết bị vật lý.');
    if(!host){storage.append(el('p',error||'Chưa đọc được số liệu.'));return;}
    for(const disk of host.disks){const row=el('article');row.className='server-resource-card';row.append(el('h4','Ổ '+disk.root+' · '+disk.purposes.join(', ')));if(disk.error)row.append(el('p',disk.error));else{row.append(el('p','Trống '+sizeLabel(disk.free_bytes)+' / '+sizeLabel(disk.total_bytes)));const meter=el('meter');meter.min=0;meter.max=100;meter.value=disk.used_percent;meter.setAttribute('aria-label','Dung lượng đã dùng ổ '+disk.root);row.append(meter,el('span',disk.used_percent+'% đã dùng'));}storage.append(row);}
    const gpu=el('article');gpu.className='server-resource-card';gpu.append(el('h4','GPU / Bộ nhớ đồ họa'));if(!host.gpu.available)gpu.append(el('p',host.gpu.message));for(const item of host.gpu.devices)gpu.append(el('p',item.name+' · GPU '+item.utilization_percent+'% · VRAM '+item.used_mb+' / '+item.total_mb+' MB'));storage.append(gpu);
    const cleanup=action('Mở công cụ xem trước dọn dữ liệu',()=>{});cleanup.dataset.adminTarget='cleanup';storage.append(cleanup);
  }
  let activeAdminSection='overview';
  async function management(){
    const scrollTop=overlay.querySelector('.server-panel').scrollTop;
    overlay.classList.remove('server-auth-mode');closePanel.hidden=false;overlay.style.display='block';body.replaceChildren();note.textContent='';
    overlay.querySelector('.server-eyebrow').textContent=session.user.role==='admin'?'QUẢN TRỊ HẠ TẦNG':'TÀI KHOẢN MÁY CHỦ';
    overlay.querySelector('#server-panel-title').textContent=session.user.role==='admin'?'Bảng điều khiển quản trị máy chủ':'Tài khoản và tác vụ của tôi';
    overlay.querySelector('#server-panel-intro').textContent=session.user.role==='admin'?'Bạn là quản trị viên máy chủ. Các máy khác đăng nhập bằng tài khoản riêng do bạn cấp.':'Đăng nhập bằng tài khoản người dùng máy chủ; quyền thao tác phụ thuộc vai trò được cấp.';
    const loading=el('div','Đang tải thông tin máy chủ…');loading.className='server-loading';body.append(loading);
    const data=await control('/status');
    let users=null,backups=null,infrastructure=null,infrastructureError='';
    if(session.user.role==='admin'){
      [users,backups]=await Promise.all([control('/users'),control('/backups')]);
      try{infrastructure=await control('/infrastructure');}catch(error){infrastructureError=[404,405].includes(error.status)?'Khởi động lại máy chủ khi rảnh để nạp số liệu hạ tầng.':error.message;}
    }
    body.replaceChildren();
    const roleName={admin:'Quản trị viên máy chủ',operator:'Người dùng',viewer:'Người xem'};
    const roleTone={admin:'admin',operator:'operator',viewer:'viewer'};
    const top=el('div');top.className='server-overview';
    const identity=el('div');identity.className='server-identity';
    identity.append(el('span','ĐANG ĐĂNG NHẬP'),el('strong',session.user.username),badge(roleName[session.user.role]||session.user.role,roleTone[session.user.role]||'neutral'));
    const actions=el('div');actions.className='server-header-actions';actions.append(action('↻ Làm mới',management),action('Đăng xuất',async()=>{await control('/logout',{method:'POST'});location.reload();}));
    top.append(identity,actions);body.append(top);
    const summary=el('div');summary.className='server-stat-grid';
    for(const [label,value,detail] of [['RAM còn trống',`${Math.round(data.available_ram_mb)} MB`,'Tài nguyên máy chủ'],['Dung lượng đĩa',`${data.free_disk_gb} GB`,'Ổ dữ liệu máy chủ'],['Tác vụ hoạt động',String(data.tasks.filter(task=>['RUNNING','QUEUED'].includes(task.state)).length),'Đang chạy hoặc chờ'],['Phiên đăng nhập',String(data.sessions?.length??'—'),'Máy chủ và máy khách']]){const card=el('article');card.className='server-stat';card.append(el('span',label),el('strong',value),el('small',detail));summary.append(card);}if(session.user.role!=='admin')body.append(summary);
    const navigation=el('nav');navigation.className='server-section-nav';navigation.setAttribute('aria-label','Các mục quản lý');
    const sections=session.user.role==='admin'?[['overview','Tổng quan'],['storage','Ổ đĩa & tài nguyên'],['sharing','Chia sẻ truy cập'],['accounts','Tài khoản'],['devices','Phiên & quyền sửa'],['tasks','Tác vụ'],['backups','Sao lưu'],['cleanup','Dọn dữ liệu'],['audit','Nhật ký']]:[['tasks','Tác vụ']];
    for(const [id,label] of sections){const link=el('a',label);link.href='#server-section-'+id;navigation.append(link);}body.append(navigation);
    if(session.user.role==='admin'){
      renderInfrastructure(infrastructure,infrastructureError,backups,data);
      try{await sharingPanel();}catch(err){const section=panelSection('sharing','Chia sẻ truy cập');section.append(el('p',[404,405].includes(err.status)?'Khởi động lại máy chủ để nạp chức năng chia sẻ và lời mời mới.':err.message));}
      const accountSection=panelSection('accounts','Tài khoản','Tạo tài khoản riêng cho từng người và chỉ cấp đúng quyền cần dùng.');
      const userList=el('div');userList.className='server-user-list';
      for(const user of users.users){const row=el('article');row.className='server-user-card';const details=el('div');details.className='server-user-details';details.append(el('strong',user.username),badge(roleName[user.role]||user.role,roleTone[user.role]||'neutral'),badge(user.enabled?'Đang hoạt động':'Đã khóa',user.enabled?'success':'muted'));const edit=action('Chỉnh sửa',()=>{name.value=user.username;role.value=user.role;enabled.checked=user.enabled;password.value='';formTitle.textContent='Cập nhật tài khoản: '+user.username;save.textContent='Lưu thay đổi';cancel.hidden=false;name.focus();form.scrollIntoView({behavior:'smooth',block:'center'});});row.append(details,edit);userList.append(row);}accountSection.append(userList);
      const formTitle=el('h4','Tạo hoặc cập nhật tài khoản');formTitle.className='server-form-title';accountSection.append(formTitle);
      const form=el('form'),name=el('input'),password=el('input'),role=el('select'),enabled=el('input');
      name.placeholder='Tên đăng nhập';name.autocomplete='off';name.required=true;name.maxLength=40;password.type='password';password.placeholder='Mật khẩu (từ 12 ký tự)';password.autocomplete='new-password';password.minLength=12;role.setAttribute('aria-label','Vai trò tài khoản');enabled.type='checkbox';enabled.checked=true;enabled.id='server-user-enabled';
      for(const [value,label] of [['operator','Người dùng · tạo và xử lý video'],['viewer','Người xem · chỉ xem'],['admin','Quản trị viên máy chủ · quản lý toàn bộ server']]){const option=el('option',label);option.value=value;role.append(option);}
      role.value='operator';const enabledLabel=el('label','Đang hoạt động');enabledLabel.htmlFor=enabled.id;const enabledControl=el('div');enabledControl.className='server-enabled-control';enabledControl.append(enabled,enabledLabel);
      const save=el('button','Tạo tài khoản');save.type='submit';save.className='btn primary';
      const cancel=el('button','Hủy chỉnh sửa');cancel.type='button';cancel.className='btn small';cancel.hidden=true;
      cancel.onclick=()=>{name.value='';password.value='';role.value='operator';enabled.checked=true;formTitle.textContent='Tạo tài khoản';save.textContent='Tạo tài khoản';cancel.hidden=true;};
      form.className='server-account-form';form.append(name,password,role,enabledControl,save,cancel);accountSection.append(form);
      const helper=el('p','Mật khẩu mới cần ít nhất 12 ký tự. Khi cập nhật, chọn “Chỉnh sửa” ở tài khoản rồi để trống mật khẩu nếu muốn giữ nguyên.');helper.className='server-help-text';accountSection.append(helper);
      form.onsubmit=async event=>{event.preventDefault();try{await control('/users',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:name.value,password:password.value,role:role.value,enabled:enabled.checked})});await management();}catch(err){note.textContent=err.message;}};
      const deviceSection=panelSection('devices','Máy khách và quyền truy cập','Mỗi người đăng nhập bằng tài khoản riêng. Thu hồi phiên khi cần; các máy khác vẫn có thể xem dự án đang được chỉnh sửa.');
      if(!(data.sessions||[]).length)deviceSection.append(el('p','Chưa có phiên đăng nhập nào khác.'));
      for(const active of data.sessions||[]){const row=el('article');row.className='server-list-row';const info=el('div');info.append(el('strong',active.username),el('span',`${active.ip||'Không rõ IP'} · Hoạt động gần nhất ${new Date(active.seen*1000).toLocaleString()}`));const device=el('details');device.append(el('summary','Chi tiết trình duyệt'),el('p',active.device||'Không có thông tin'));info.append(device);row.append(info,action('Đăng xuất phiên',async()=>{await control('/sessions/'+active.id,{method:'DELETE'});await management();}));deviceSection.append(row);}
      if(data.leases.length)for(const item of data.leases){const row=el('article');row.className='server-list-row';const info=el('div');info.append(el('strong',item.project),el('span',`Đang sửa bởi ${item.username||'người dùng'}`));row.append(info,action('Giải phóng quyền sửa',async()=>{if(confirm('Phiên cũ phải tải lại trước khi sửa. Tiếp tục?')){await control('/locks/'+item.project,{method:'DELETE'});await management();}}));deviceSection.append(row);}else deviceSection.append(el('p','Không có dự án nào đang bị khóa chỉnh sửa.'));
      const backupSection=panelSection('backups','Sao lưu','Sao lưu tự động khi rảnh; kiểm tra tính toàn vẹn trước khi sử dụng.');
      const backupSummary=el('p',`Tự động: ${backups.automatic_hours?backups.automatic_hours+' giờ khi máy rảnh':'đang tắt'} · ${backups.backups.length} bản sao / báo cáo`);backupSummary.className='server-help-text';backupSection.append(backupSummary);
      backupSection.append(action('＋ Tạo bản sao đã kiểm tra',async()=>{note.textContent='Đang sao lưu dữ liệu. Bạn vẫn có thể xem; thao tác ghi sẽ tạm chờ.';const result=await control('/backups',{method:'POST'});note.textContent=`Đã kiểm tra bản sao ${result.name}`;await management();}));
      if(backups.working_root)backupSection.append(el('p','Vùng snapshot và gói làm việc: '+backups.working_root));
      if(backups.replica_directory)backupSection.append(el('p','Kho sao chép: '+backups.replica_directory+(backups.allow_same_disk?' · cho phép bản cục bộ cùng ổ vật lý; không mở quyền xóa gốc.':'')));
      if(!backups.backups.length)backupSection.append(el('p','Chưa có bản sao hoàn tất.'));
      for(const item of backups.backups){const row=el('article');row.className='server-list-row';const info=el('div');info.append(el('strong',new Date(item.time*1000).toLocaleString()),el('span',`${item.name} · ${item.files} tệp`),badge(item.status==='VALIDATED_PASS'?'Đạt · REQUIRED đã đối chiếu':item.complete?'Bản cũ · chưa kiểm tra REQUIRED':item.status||'Chưa hoàn tất',item.status==='VALIDATED_PASS'?'success':'warning'));if(item.status==='VALIDATED_PASS'){info.append(el('span',item.remote_status==='LOCAL_REPLICA_VERIFIED'?(item.drill_at?'Bản sao cục bộ · đã phục hồi thử':'Bản sao cục bộ · chưa phục hồi thử'):item.drill_at?'Kho độc lập · đã phục hồi thử':item.remote_status==='REMOTE_VERIFIED'?'Kho độc lập · chưa phục hồi thử':'Chỉ có bản cục bộ'));row.append(action('Lưu Google Drive',async()=>{note.textContent='Đang truyền gói sao lưu; giữ lại các phiên trước.';await control('/backups/'+item.name+'/export-drive',{method:'POST'});await management();}));if(['REMOTE_VERIFIED','LOCAL_REPLICA_VERIFIED'].includes(item.remote_status))row.append(action('Phục hồi thử',async()=>{note.textContent='Đang đọc lại gói sao lưu và phục hồi vào thư mục riêng.';await control('/backups/'+item.name+'/drill',{method:'POST'});await management();}));}if(item.errors?.length){info.append(el('span',`${item.errors.length} vấn đề`));const details=el('details');details.append(el('summary','Xem danh sách tệp cần xử lý'));const listing=el('pre',item.errors.map(error=>`${error.reason}: ${error.path||error.reference||''}`).join('\n'));listing.style.cssText='max-height:220px;overflow:auto;white-space:pre-wrap;word-break:break-word';details.append(listing);info.append(details);}row.append(info,action('Kiểm tra lại',async()=>{await control('/backups/'+item.name+'/verify',{method:'POST'});note.textContent='Bản sao toàn vẹn.';}),action('Xóa',async()=>{if(confirm('Xóa bản sao này? Cần giữ lại ít nhất một bản hợp lệ.')){await control('/backups/'+item.name,{method:'DELETE'});await management();}}));backupSection.append(row);}
      if(backups.last_error?.message)backupSection.append(el('p','Lần sao lưu gần nhất chưa hoàn tất: '+backups.last_error.message));
      const cleanupSection=panelSection('cleanup','Dọn dữ liệu & đối chiếu công đoạn lỗi','Admin xem trước, chọn mục và xác nhận. Thành phẩm gốc vẫn được bảo vệ.');
      if(window.renderCleanupAdmin){try{await window.renderCleanupAdmin(cleanupSection,(path,options)=>control('/api/admin'+path,options));}catch(error){cleanupSection.append(el('p',[404,405].includes(error.status)?'Khởi động lại server khi rảnh để nạp chức năng dọn dữ liệu mới.':error.message));}}
    }
    const taskSection=panelSection('tasks','Tác vụ','Tối đa 50 tác vụ gần nhất. Lọc để xem lỗi, tiến độ và quản lý các tác vụ bạn có quyền thao tác.');
    const taskItems=data.tasks;
    const filters=el('div');filters.className='server-task-filters';const search=el('input');search.placeholder='Tìm dự án, người gửi hoặc công đoạn';search.setAttribute('aria-label','Tìm tác vụ');const stateFilter=el('select');stateFilter.setAttribute('aria-label','Lọc trạng thái tác vụ');for(const [value,label] of [['all','Tất cả trạng thái'],['RUNNING','Đang chạy'],['QUEUED','Đang chờ'],['FAILED','Thất bại'],['INTERRUPTED','Gián đoạn'],['COMPLETED','Hoàn tất'],['CANCELLED','Đã hủy']]){const option=el('option',label);option.value=value;stateFilter.append(option);}filters.append(search,stateFilter);taskSection.append(filters);const taskRows=[];const emptyFilter=el('p','Không có tác vụ phù hợp.');emptyFilter.hidden=true;
    if(!taskItems.length)taskSection.append(el('p','Chưa có tác vụ gần đây.'));
    for(const task of taskItems){const row=el('article');row.className='server-list-row server-task-row';const info=el('div');const state=task.state==='RUNNING'?'Đang chạy':task.state==='QUEUED'?'Đang chờ':['DONE','COMPLETED'].includes(task.state)?'Hoàn tất':task.state==='INTERRUPTED'?'Bị gián đoạn':task.state==='FAILED'?'Cần xử lý':task.state==='CANCELLED'?'Đã hủy':task.state;info.append(el('strong',task.project_id||'Tác vụ server'),el('span',`${task.owner||'Hệ thống'} · ${task.action} · ${state}${task.stage?' · '+task.stage:''}`));const progress=el('div');progress.className='server-task-progress';const track=el('div');const fill=el('i');fill.style.width=`${Math.max(0,Math.min(100,Number(task.progress)||0))}%`;track.append(fill);progress.append(track,el('span',`${Math.round(Number(task.progress)||0)}%`));info.append(progress);
      const latest=task.progress_events?.at(-1)?.timestamp||task.started_at||task.created_at;
      if(latest)info.append(el('span','Cập nhật tiến độ: '+new Date(latest*1000).toLocaleString()));
      if(task.error_message){const details=el('details');details.append(el('summary','Xem lỗi · '+(task.error_code||task.state)),el('p',task.error_message));info.append(details);}
      row.append(info);if(task.project_id)row.append(action('Mở dự án',async()=>{overlay.style.display='none';await openProject(task.project_id);}));
      if(['RUNNING','QUEUED'].includes(task.state) && task.task_id && (session.user.role==='admin' || task.owner===session.user.username))row.append(action('Tạm dừng an toàn',async()=>{await control('/api/tasks/'+encodeURIComponent(task.task_id)+'/pause',{method:'POST'});await management();}));
      taskRows.push({row,task});
      if(['RUNNING','QUEUED'].includes(task.state) && task.job_id && (session.user.role==='admin' || task.owner===session.user.username))row.append(action('Hủy',async()=>{if(confirm('Hủy tác vụ này?')){const response=await window.fetch('/api/jobs/'+task.job_id+'/cancel',{method:'POST'});if(!response.ok)throw Error('Chưa hủy được tác vụ.');await management();}}));taskSection.append(row);
    }
    taskSection.append(emptyFilter);
    const filterTasks=()=>{let visible=0;for(const {row,task} of taskRows){const matches=JSON.stringify([task.project_id,task.owner,task.action,task.stage]).toLocaleLowerCase().includes(search.value.toLocaleLowerCase())&&(stateFilter.value==='all'||(task.state==='DONE'?'COMPLETED':task.state)===stateFilter.value);row.hidden=!matches;if(matches)visible++;}emptyFilter.hidden=visible>0;};search.oninput=filterTasks;stateFilter.onchange=filterTasks;
    if(data.audit){const auditSection=panelSection('audit','Nhật ký quản lý','Các thao tác quản trị gần đây.');for(const item of data.audit.slice(0,30)){const row=el('p',`${item.username} · ${item.action} · ${item.status??''} · ${new Date(item.time*1000).toLocaleString()}`);row.className='server-audit-row';auditSection.append(row);}}
    const links=[...navigation.querySelectorAll('a')];navigation.setAttribute('role','tablist');
    const showSection=id=>{activeAdminSection=id;for(const section of body.querySelectorAll('.server-section'))section.hidden=section.id!=='server-section-'+id;for(const link of links){const selected=link.dataset.section===id;link.classList.toggle('active',selected);link.setAttribute('aria-selected',String(selected));link.tabIndex=selected?0:-1;}};
    for(const link of links){const id=link.hash.replace('#server-section-','');link.dataset.section=id;link.setAttribute('role','tab');link.id='server-tab-'+id;link.setAttribute('aria-controls','server-section-'+id);const section=body.querySelector('#server-section-'+id);section?.setAttribute('role','tabpanel');section?.setAttribute('aria-labelledby',link.id);link.onclick=event=>{event.preventDefault();showSection(id);};link.onkeydown=event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();let index=links.indexOf(link);index=event.key==='Home'?0:event.key==='End'?links.length-1:(index+(event.key==='ArrowRight'?1:-1)+links.length)%links.length;links[index].click();links[index].focus();};}
    showSection(links.some(link=>link.dataset.section===activeAdminSection)?activeAdminSection:links[0].dataset.section);
    body.querySelectorAll('[data-admin-target]').forEach(link=>link.onclick=()=>showSection(link.dataset.adminTarget));
    overlay.querySelector('.server-panel').scrollTop=scrollTop;
  }
  button.onclick=()=>management().catch(err=>{overlay.style.display='block';note.textContent=err.message;});

  const upload=el('input');upload.type='file';upload.accept='video/*';upload.style.display='none';
  let selectedUpload=null,uploading=false,stopUpload=false;
  const uploadButton=action('Tải video từ máy này lên server',async()=>{selectedUpload=null;upload.click();});uploadButton.style.display='none';
  const uploadPanel=el('section');uploadPanel.className='server-upload-panel';uploadPanel.style.display='none';
  const uploadStatus=el('p');uploadStatus.setAttribute('role','status');
  const pause=el('button','Tạm dừng tải');pause.className='btn small';pause.hidden=true;pause.onclick=()=>{stopUpload=true;uploadStatus.textContent='Đang dừng sau mảnh hiện tại…';};
  const pending=el('div');pending.className='server-pending-uploads';uploadPanel.append(uploadStatus,pause,action('Làm mới danh sách tải lên',refreshUploads),pending);
  const newView=document.getElementById('view-new')||document.body;newView.append(uploadButton,upload,uploadPanel);
  async function refreshUploads(){
    if(!session.user)return;if(session.user.role==='viewer'){uploadButton.style.display='none';uploadPanel.style.display='none';return;}uploadButton.style.display='';uploadPanel.style.display='';pending.replaceChildren();
    try{const response=await window.fetch('/api/uploads');if(!response.ok)return;const data=await response.json();
      for(const item of data.uploads){const row=el('article');row.className='server-list-row';row.append(el('span',`${item.filename} · ${Math.round(item.received_offset/item.total_size*100)}% · ${item.status}${item.error_detail?' · '+item.error_detail:''}`));
        if(item.status==='COMPLETED')row.append(action('Mở dự án',()=>openProject(item.project_id)));
        if(item.status==='VERIFYING')row.append(action('Kiểm tra kết quả',async()=>{const state=await uploadRequest('/'+item.upload_id+'/status');if(state.status==='COMPLETED')await openProject(state.project_id);else{uploadStatus.textContent=state.error_detail||'Trạng thái: '+state.status;await refreshUploads();}}));
        if(item.status==='UPLOADING'&&item.expires_at*1000>Date.now())row.append(action('Chọn lại tệp để tiếp tục',()=>{if(uploading)throw Error('Hãy tạm dừng phiên đang tải trước.');selectedUpload=item;upload.click();}));
        if(!['VERIFYING','COMPLETED'].includes(item.status))row.append(action('Hủy phiên',async()=>{if(confirm('Hủy phiên này? Các byte được giữ lại chờ dọn an toàn.')){const r=await window.fetch('/api/uploads/'+item.upload_id,{method:'DELETE'});if(!r.ok)throw Error('Chưa hủy được phiên.');await refreshUploads();}}));pending.append(row);
      }
    }catch(err){uploadStatus.textContent=err.message;}
  }
  async function uploadRequest(path,options={}){
    const response=await window.fetch('/api/uploads'+path,options),data=await response.json();
    if(!response.ok){if([404,405].includes(response.status))throw Object.assign(Error('Máy chủ chưa nạp chức năng upload tiếp tục. Cần khởi động lại server khi rảnh.'),{status:response.status});const detail=data.message||data.detail||'Upload thất bại';throw Object.assign(Error(typeof detail==='string'?detail:detail.message||JSON.stringify(detail)),{status:response.status});}return data;
  }
  upload.onchange=async()=>{
    const file=upload.files[0];if(!file||uploading)return;
    if(session.upload_limit&&file.size>session.upload_limit){uploadStatus.textContent='Video vượt giới hạn upload của máy chủ.';uploadPanel.style.display='';upload.value='';return;}
    uploading=true;stopUpload=false;uploadButton.disabled=true;pause.hidden=false;uploadPanel.style.display='';
    try{
      const sample=await new Blob([file.slice(0,1024*1024),file.slice(Math.max(0,file.size-1024*1024)),String(file.size)]).arrayBuffer();
      const fingerprint=await window.uploadSHA256(sample);
      if(selectedUpload&&(selectedUpload.total_size!==file.size||selectedUpload.filename!==file.name||selectedUpload.fingerprint!==fingerprint))throw Error('Tệp chọn lại không khớp tên, kích thước hoặc dấu vân tay mẫu của phiên cũ.');
      let state=selectedUpload?await uploadRequest('/'+selectedUpload.upload_id+'/status'):await uploadRequest('/init',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename:file.name,total_size:file.size,fingerprint})});
      while(state.received_offset<file.size&&!stopUpload){
        const offset=state.received_offset,bytes=await file.slice(offset,offset+state.chunk_size).arrayBuffer();const checksum=await window.uploadSHA256(bytes);
        let sent=false;
        for(let retry=0;retry<4&&!sent&&!stopUpload;retry++){
          try{state=await uploadRequest('/'+state.upload_id+'/chunk?expected_offset='+offset,{method:'PATCH',headers:{'X-Chunk-SHA256':checksum,'Content-Type':'application/octet-stream'},body:bytes});sent=true;}
          catch(err){if([401,403,404,410,413,422].includes(err.status)||retry===3)throw err;uploadStatus.textContent='Mất kết nối hoặc offset thay đổi; đang đối chiếu…';await new Promise(resolve=>setTimeout(resolve,Math.min(4000,500*2**retry)));state=await uploadRequest('/'+state.upload_id+'/status');if(state.received_offset!==offset){sent=true;}}
        }
        uploadStatus.textContent=`Đã nhận ${Math.round(state.received_offset/file.size*100)}% · ${file.name}`;
      }
      if(stopUpload){uploadStatus.textContent='Đã tạm dừng. Chọn lại cùng tệp trong danh sách để tiếp tục.';return;}
      pause.hidden=true;state=await uploadRequest('/'+state.upload_id+'/complete',{method:'POST'});
      while(state.status==='VERIFYING'){
        uploadStatus.textContent='Đã tải đủ. Máy chủ đang kiểm định video; bạn có thể quay lại danh sách phiên nếu đóng trang.';
        await new Promise(resolve=>setTimeout(resolve,1500));state=await uploadRequest('/'+state.upload_id+'/status');
      }
      if(state.status!=='COMPLETED')throw Error(state.error_detail||'Video chưa vượt qua kiểm định.');
      uploadStatus.textContent='Video đã được kiểm định và tạo dự án.';await openProject(state.project_id);
    }catch(err){uploadStatus.textContent='Tải video: '+err.message;}finally{uploading=false;uploadButton.disabled=false;pause.hidden=true;upload.value='';selectedUpload=null;await refreshUploads();}
  };
  window.addEventListener('workspace-server-login',refreshUploads);
  (async()=>{try{
    session=await control('/session');window.workspaceServerEnabled=!!session.enabled;window.workspaceServerUser=session.user;
    if(!session.enabled){readyResolve();return;}
    button.style.display='';uploadButton.style.display='';
    if(session.user&&!invitation){readyResolve();refreshUploads();}else await loginPanel();
  }catch(err){message([404,405].includes(err.status)?'Máy chủ chưa nạp bản quản lý nhiều máy. Khi hết tác vụ, mở lại bằng start_small_server.bat.':'Chưa kết nối được máy chủ: '+err.message);readyResolve();}})();
})();
