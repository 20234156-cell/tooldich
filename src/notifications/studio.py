"""Phone control surface using the same Studio APIs, queue and Auto validation.

Callback payloads contain only opaque short tokens. Durable records bind actions
to an account, message, project revision and expiry; mutations are single-use.
"""
import hashlib
import json
import secrets
import time
import io
import re
from pathlib import Path
from contextlib import nullcontext

from config.settings import settings
from src.notifications import assistant

COMMANDS = [('menu', 'Bảng điều khiển Studio'), ('new', 'Tạo dự án và chọn cấu hình'),
            ('projects', 'Dự án của tôi'), ('running', 'Tiến độ đang xử lý'),
            ('review', 'Dự án cần duyệt'), ('storage', 'Kho video và dung lượng'),
            ('status', 'Máy tính và kết nối'), ('help', 'Hướng dẫn')]


class StudioError(ValueError):
    """Only deliberate, credential-free user guidance from this module."""
CHOICES = {
    'product': [('dub', 'Giọng Việt + phụ đề Việt'), ('subtitles', 'Phụ đề Việt · giữ tiếng gốc'),
                ('voice_only', 'Chỉ lồng tiếng · giữ hình gốc')],
    'source': [('screen', 'Đọc chữ trên hình'), ('speech', 'Nghe lời thoại')],
    'style': [('natural_blur', 'Chữ trắng'), ('tiktok_yellow', 'TikTok vàng viền đen'),
              ('modern_white', 'Trắng hiện đại'), ('review_anton', 'Review phim · Anton'),
              ('cinematic_svn', 'Cổ trang · SVN-Times'), ('instagram', 'Reels / Instagram')],
    'audio': [('mix', 'Trộn: giọng Việt 1.7x / gốc 0.15x'), ('voice_only', 'Chỉ giọng Việt'),
              ('original_only', 'Chỉ tiếng gốc')],
    'voice': [(v, label) for v, label in [
        ('piper:ngochuyen', 'Ngọc Huyền · Piper'), ('piper:banmai', 'Ban Mai'),
        ('piper:lacphi', 'Lạc Phi'), ('piper:chieuthanh', 'Chiêu Thanh'),
        ('piper:manhdung', 'Mạnh Dũng'), ('piper:vais1000', 'VAIS 1000'),
        ('vi-VN-NamMinhNeural', 'Nam Minh · Edge online'), ('vi-VN-HoaiMyNeural', 'Hoài My · Edge online'),
        ('vieneu:ngoc_huyen_review_phim', 'Ngọc Huyền · VieNeu GPU')]],
    'rate': [('+0%', '1.0x'), ('+10%', '1.1x'), ('+15%', '1.15x'), ('+20%', '1.2x'), ('+25%', '1.25x')],
    'profile': [('fast', 'Nhanh'), ('balanced', 'Cân bằng'), ('compact', 'Tiết kiệm dung lượng'), ('quality', 'Chất lượng cao')],
    'uncertain': [('review', 'Dừng khi cần duyệt nguồn'), ('skip', 'Auto xử lý câu chưa chắc theo quy tắc Studio')],
}
LABELS = dict(product='Thành phẩm', source='Nguồn để dịch', style='Mẫu phụ đề', audio='Âm thanh',
              voice='Giọng đọc', rate='Tốc độ', profile='Mã hóa', uncertain='Kiểm duyệt nguồn')


def identity():
    return hashlib.sha256((settings.TELEGRAM_BOT_TOKEN+'|'+settings.TELEGRAM_CHAT_ID).encode()).hexdigest()


def transaction(service, change):
    def apply(state):
        studio = state.setdefault('studio', {})
        if studio.get('identity') != identity():
            studio.clear(); studio.update(identity=identity(), actions={}, drafts={})
        return change(studio)
    return service._action_state(apply)


def observe(service, task_id, pid):
    """Terminal notification runs after the shared queue has released the job."""
    from src.api import app as api
    task=api.queue.get(task_id)
    if not task or not task.future:return
    def done(_):
        try:project(service,pid)
        except Exception:pass
    task.future.add_done_callback(done)


def fingerprint(project_id):
    from src.api import app as api
    from src.pipeline import auto_workflow as auto
    info = api.manager.get_project_detail(project_id)
    task = api.queue.get_by_project(project_id)
    value = [{key:info.get(key) for key in ('status','stage','transcript','media')}, auto.read(project_id), task.task_id if task else None,
             task.state if task else None]
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def button(label, action, **values):
    return label, dict(action=action, **values)


def send(service, text, rows):
    now = time.time()
    def register(state):
        actions = state.setdefault('actions', {})
        state['actions'] = actions = {k:v for k,v in actions.items() if v['expires'] > now}
        markup = []
        tokens = []
        for row in rows:
            buttons = []
            for label, record in row:
                token = secrets.token_hex(6)
                actions[token] = dict(record, expires=now+86400, message_id=None)
                tokens.append(token)
                buttons.append(dict(text=label, callback_data='ui:'+token))
            markup.append(buttons)
        while len(actions) > 1500:
            actions.pop(next(iter(actions)))
        return tokens, {'inline_keyboard': markup}
    tokens, markup = transaction(service, register)
    reply = service.message(text, markup)
    if reply and type(reply.get('message_id')) is int:
        def bind(state):
            for token in tokens:
                if token in state['actions']:
                    state['actions'][token]['message_id'] = reply['message_id']
        transaction(service, bind)
    return dict(reply, studio_markup=markup) if isinstance(reply,dict) else reply


def home(service):
    cancel_input(service)
    snapshot = assistant.system_snapshot()
    send(service, '🎬 STUDIO TỪ XA\n'+assistant.status_text(snapshot)+
         '\nChọn Tạo dự án để tải nguồn rồi cấu hình. Dán link ngoài trình tạo vẫn chạy Auto nhanh.', [
        [button('➕ Tạo dự án', 'new'), button('⚙️ Đang xử lý', 'list', kind='running')],
        [button('📂 Dự án', 'list'), button('📝 Cần duyệt / lỗi', 'list', kind='review')],
        [button('☁️ Kho lưu trữ', 'list', kind='storage'), button('💻 Hệ thống', 'status')],
        [button('❓ Hướng dẫn', 'help'), button('🔄 Làm mới', 'home')]])


def projects(service, page=0, kind='all'):
    from src.api import app as api
    rows = api.manager.list_projects(limit=10000)
    if kind == 'running':
        rows = [p for p in rows if (t := api.queue.get_by_project(p['id'])) and t.state in {'QUEUED', 'RUNNING'}]
    elif kind == 'review':
        from src.pipeline import auto_workflow as auto
        rows = [p for p in rows if (auto.read(p['id']) or {}).get('state') in {'needs_review', 'failed', 'paused', 'interrupted'}
                or p.get('error_code') or p.get('stage') in {'OCR_READY', 'TRANSLATED'}]
    elif kind == 'storage':
        rows = [p for p in rows if p.get('stage') == 'VERIFIED' or p.get('status') == 'VERIFIED']
    page = max(0, min(int(page), max(0, (len(rows)-1)//5)))
    buttons = [[button(f"{p.get('title') or p['id']} · {p.get('stage') or p['status']}", 'project', project=p['id'])]
               for p in rows[page*5:page*5+5]]
    nav = []
    if page: nav.append(button('⬅️ Trước', 'list', page=page-1, kind=kind))
    if (page+1)*5 < len(rows): nav.append(button('Sau ➡️', 'list', page=page+1, kind=kind))
    if nav: buttons.append(nav)
    buttons.append([button('🏠 Menu', 'home')])
    send(service, f'📂 {len(rows)} dự án · trang {page+1}\nChọn dự án để xem tiến độ và thao tác.', buttons)


def project(service, pid):
    from src.api import app as api
    from src.pipeline import auto_workflow as auto
    info = assistant.project_info(pid)
    detail = api.manager.get_project_detail(pid)
    task = api.queue.get_by_project(pid)
    active = task and task.state in {'QUEUED', 'RUNNING'}
    profile = auto.read(pid) or {}
    stage = detail.get('stage') or info['status']
    text = f"🎬 {info['title']}\n{pid}\nTrạng thái: {stage}\nMP4 trên máy: {info['mp4_bytes']/1024**2:.1f} MiB"
    text += f"\nBiên nhận YouTube: {'có' if info['youtube_upload'] else 'chưa có'} · Drive: {info.get('drive_archive_mode') or 'chưa có'}"
    text += '\nBiên nhận không thay thế kiểm tra video trên cloud. VERIFIED là QA kỹ thuật.'
    warnings = auto.content_warnings(pid)
    if warnings: text += '\n' + warnings
    rows = []
    if active:
        text += f'\n⚙️ {task.stage} · {task.progress}% · {task.state}'
        if task.started_at: text += f'\nĐã xử lý {max(0,int(time.time()-task.started_at))//60} phút. Phần trăm là mốc công việc, không phải thời gian còn lại.'
        rows.append([button('⏹ Yêu cầu dừng', 'stop', project=pid, task_id=task.task_id)])
    else:
        if task and task.error_code:
            text += '\nTác vụ gần nhất: '+str(task.error_code)
        if profile.get('error_code'):
            text += '\nCần xử lý: '+str(profile['error_code'])
            if profile.get('error_message'):
                from src.notifications.telegram import safe_text
                text += '\n'+safe_text(str(profile['error_message']))[:900]
        if profile and profile.get('state') != 'complete':
            rows.append([button('▶️ Duyệt / tiếp tục Auto', 'resume?', project=pid)])
            if profile.get('error_code') == 'OCR_REVIEW_REQUIRED':
                rows.append([button('⏭ Xử lý câu chưa chắc & tiếp tục', 'resume?', project=pid, skip=True)])
        rows.append([button('⚙️ Cấu hình Auto', 'config', project=pid), button('📝 Xem / sửa câu', 'cues', project=pid, page=0)])
        if not detail.get('media'):
            rows.append([button('⬇️ Thử tải nguồn lại', 'download', project=pid, revision=fingerprint(pid))])
        if info['mp4_bytes']:
            rows.append([button('🎬 Xem Demo', 'legacy', project=pid, command='demo')])
        if stage == 'VERIFIED':
            rows.append([button('📺 YouTube Private', 'legacy', project=pid, command='yt'),
                         button('☁️ Hồ sơ Drive', 'legacy', project=pid, command='drv')])
            if info['mp4_bytes']:
                rows.append([button('☁️ Lưu MP4 đầy đủ Drive', 'drivefull?', project=pid)])
        if info['youtube_upload'] or info['drive_archive']:
            rows.append([button('🔗 Mở bản đã lưu cloud', 'cloudlinks', project=pid)])
        rows.append([button('🧹 Giải phóng media', 'legacy', project=pid, command='del?'),
                     button('🗑 Xóa cả dự án', 'delete?', project=pid)])
    rows.append([button('🔄 Cập nhật', 'project', project=pid), button('🏠 Menu', 'home')])
    reply=send(service, text, rows)
    if active and reply and type(reply.get('message_id')) is int:
        def watch(state):
            watches=state.setdefault('watches',{})
            watches[pid]=dict(task_id=task.task_id,message_id=reply['message_id'],signature=None,
                              title=info['title'],markup=reply['studio_markup'],expires=time.time()+7*86400)
            while len(watches)>30:watches.pop(next(iter(watches)))
        transaction(service,watch)
    elif not active:
        transaction(service,lambda state:state.setdefault('watches',{}).pop(pid,None))


def update_progress(service):
    """Refresh bounded remote cards on the existing background metrics worker."""
    from src.api import app as api
    from src.notifications.telegram import safe_text
    watches=transaction(service,lambda state:dict(state.get('watches',{})))
    for pid,record in list(watches.items())[:4]:
        if service.stopping:return
        def rotate(state):
            items=state.setdefault('watches',{})
            if pid in items:items[pid]=items.pop(pid)
        transaction(service,rotate)
        task=api.queue.get_by_project(pid)
        if record['expires']<time.time() or not task or task.task_id!=record['task_id']:
            transaction(service,lambda state:state.setdefault('watches',{}).pop(pid,None));continue
        elapsed=max(0,int(time.time()-task.started_at)) if task.started_at else 0
        signature=[task.state,task.progress,task.stage,elapsed//60,task.cancel_requested]
        if signature==record.get('signature'):continue
        text=f"🎬 {record['title']}\n{pid}\n⚙️ {task.stage} · {task.progress}% · {task.state}"
        if task.started_at:text+=f'\nĐã xử lý {elapsed//60} phút. % là mốc công việc, không phải ETA.'
        if task.cancel_requested:text+='\nĐã yêu cầu dừng; đang chờ tác vụ kết thúc an toàn.'
        text+='\nBấm Cập nhật để mở thẻ và thao tác theo trạng thái mới.'
        try:
            service.call('editMessageText',dict(chat_id=settings.TELEGRAM_CHAT_ID,message_id=record['message_id'],text=safe_text(text),
                         reply_markup=json.dumps(record['markup'],ensure_ascii=False)),timeout=3)
        except Exception:continue
        def saved(state):
            current=state.setdefault('watches',{}).get(pid)
            if current and current['message_id']==record['message_id']:
                if task.state in {'QUEUED','RUNNING'}:current['signature']=signature
                else:state['watches'].pop(pid,None)
        transaction(service,saved)


def defaults():
    from src.pipeline.media_storage import storage_free_bytes
    return dict(product='dub', source='screen', style='natural_blur', audio='mix', voice='piper:ngochuyen',
                rate='+15%', profile='compact' if storage_free_bytes()<35*1024**3 else 'balanced', uncertain='skip')


def payload(values):
    from src.api.schemas import RunAllRequest
    for key, value in values.items():
        if key not in CHOICES or value not in dict(CHOICES[key]): raise StudioError('Cấu hình không hợp lệ.')
    values = dict(defaults(), **values)
    subtitle_only = values['product'] == 'subtitles'
    voice_only = values['product'] == 'voice_only'
    audio = 'original_only' if subtitle_only else values['audio']
    if voice_only and audio == 'original_only': raise StudioError('Chỉ lồng tiếng cần bật giọng Việt.')
    options = dict(workflow_mode='custom', extraction_mode='hardsub' if values['source']=='screen' else 'stt',
                   audio_mode=audio, voice=values['voice'], rate=values['rate'], processing_profile=values['profile'],
                   voice_volume=1.7, bg_volume=.15, skip_uncertain=values['uncertain']=='skip',
                   fit_to_video=True, shorten_to_fit=True)
    if voice_only: options['subtitle_style']='original'
    elif values['style']=='natural_blur': options['subtitle_preset']='natural_blur'
    else:
        font,color,width,size = {
            'tiktok_yellow': ('Montserrat','&H0000FFFF',3,48), 'modern_white': ('Montserrat','&H00FFFFFF',2,48),
            'review_anton': ('Anton','&H0000FFFF',4,52), 'cinematic_svn': ('SVN-Times New Roman','&H0000D7FF',2,46),
            'instagram': ('Instagram','&H00FFFFFF',3,48)}[values['style']]
        options.update(subtitle_style='cover',font_family=font,font_color=color,outline_color='&H00000000',outline_width=width,font_size=size)
    return RunAllRequest.model_validate(options)


def draft(service, pid):
    return transaction(service, lambda state: dict(state.setdefault('drafts', {}).setdefault(pid, dict(values=defaults(), version=0))))


def config(service, pid):
    from src.api import app as api
    api._require_editable(pid)
    data = draft(service, pid)
    text = '⚙️ CẤU HÌNH AUTO · '+pid+'\n'
    text += '\n'.join(f'{LABELS[k]}: {dict(CHOICES[k])[v]}' for k,v in data['values'].items())
    text += '\nChỉ áp dụng khi bấm Chạy Auto. Chọn lại cấu hình có thể phải xử lý lại các bước liên quan.'
    from src.pipeline.capacity import project_capacity, GIB
    options = payload(data['values'])
    capacity = project_capacity(pid, profile=options.processing_profile, audio_mode=options.audio_mode)
    if capacity:
        text += f"\nDự toán chỗ trống cần thêm (gồm dự phòng): {capacity['required_free_bytes']/GIB:.1f} GiB; đang trống {capacity['free_bytes']/GIB:.1f} GiB. Đây là ước tính."
        if not capacity['sufficient']: text += '\n⚠️ Chưa đủ dung lượng dự toán; chọn compact hoặc dọn media đã sao lưu.'
    if data['values']['product']=='subtitles': text += '\nChỉ phụ đề: dùng tiếng gốc, không tạo giọng.'
    rows = [[button(LABELS[k], 'choose', project=pid, field=k)] for k in CHOICES
            if not (data['values']['product']=='subtitles' and k in {'audio','voice','rate'})
            and not (data['values']['product']=='voice_only' and k=='style')]
    rows += [[button('🔎 Nhận dạng lại nguồn', 'extract?', project=pid)],
             [button('▶️ Kiểm tra & chạy Auto', 'run?', project=pid)], [button('⬅️ Dự án', 'project', project=pid)]]
    send(service, text, rows)


def new(service):
    cancel_input(service)
    transaction(service, lambda state: state.update(input=dict(kind='link',expires=time.time()+1800)))
    send(service, '➕ TẠO DỰ ÁN\nGửi một link Bilibili hoặc Douyin. Tôi sẽ tải và kiểm định nguồn, rồi gửi bảng cấu hình trước khi chạy Auto.',
         [[button('✖️ Hủy nhập link', 'cancel_input'), button('🏠 Menu', 'home')]])


def download(service, url, *, force=False):
    from src.api import app as api
    from src.pipeline.media_storage import auto_disk_guard_if_needed, storage_free_bytes
    auto_disk_guard_if_needed()
    if storage_free_bytes()<settings.AUTO_DISK_GUARD_MIN_FREE_GB*1024**3: raise StudioError('Ổ dữ liệu không đủ dung lượng.')
    recent=transaction(service,lambda state:state.get('links',{}).get(url))
    if recent and not force:
        from src.database import connection
        from src.database.models import Project
        with connection.SessionLocal() as db:exists=db.get(Project,recent) is not None
        if exists:
            send(service,'Link này đã có dự án '+recent+'. Mở bản cũ hay tạo một bản mới?',
                 [[button('📂 Mở dự án cũ','project',project=recent)],
                  [button('➕ Xác nhận tạo bản mới','copy',url=url)]])
            return
    pid = api.manager.create_project(url)['project_id']
    def remember(state):
        links=state.setdefault('links',{});links[url]=pid
        while len(links)>200:links.pop(next(iter(links)))
    transaction(service,remember)
    draft(service, pid)
    transaction(service, lambda state: state.pop('input', None))
    try:task = api.start_download(pid)
    except Exception:
        send(service,'Đã tạo '+pid+' nhưng chưa đưa được tác vụ tải vào hàng đợi. Dự án được giữ để thử lại hoặc xóa.',
             [[button('📋 Mở dự án','project',project=pid)]])
        return
    queued = api.queue.get(task['task_id'])
    send(service, '⬇️ Đang tải và kiểm định nguồn · '+pid+'\nTải xong chọn cấu hình rồi bấm Chạy Auto.',
         [[button('📋 Xem tiến độ', 'project', project=pid)]])
    def completed(_):
        try:
            if queued.state=='DONE': config(service,pid)
            else: project(service,pid)
        except Exception: pass  # Download outcome remains authoritative even if Telegram is unavailable.
    queued.future.add_done_callback(completed)
    return task


def cues(service, pid, page=0):
    from src.api import app as api
    rows = api.get_cues_endpoint(pid)
    page = max(0,min(int(page),max(0,len(rows)-1)))
    if not rows:
        send(service,'Chưa có câu nguồn. Chạy nhận dạng trong Auto trước.',[[button('⬅️ Dự án','project',project=pid)]])
        return
    row=rows[page]
    text=f"📝 {pid} · câu {page+1}/{len(rows)}\n#{row['index']} · {row['start']:.2f}–{row['end']:.2f}s\nNguồn: {row.get('corrected_text') or row.get('text','')}\nViệt: {row.get('vi_text','')}"
    buttons=[[button('✏️ Sửa lời Việt','edit',project=pid,index=row['index'],row=row,field='vi_text'),
              button('✏️ Sửa nguồn','edit',project=pid,index=row['index'],row=row,field='corrected_text')],
             [button('🖼 Xem khung hình','frame',project=pid,index=row['index'])]]
    if row.get('voice_audio_path'):
        buttons[-1].append(button('🔊 Nghe giọng đã tạo','audio',project=pid,index=row['index']))
    nav=[]
    if page:nav.append(button('⬅️ Câu trước','cues',project=pid,page=page-1))
    if page+1<len(rows):nav.append(button('Câu sau ➡️','cues',project=pid,page=page+1))
    if nav:buttons.append(nav)
    buttons.append([button('⬅️ Dự án','project',project=pid)])
    send(service,text,buttons)


def cancel_input(service):
    def cancel(state):
        state.pop('input',None)
        state['actions']={k:v for k,v in state.get('actions',{}).items() if v['action'] not in {'edit!','extract!'}}
    transaction(service,cancel)


def cloud_links(service,pid):
    from src.pipeline.media_storage import project_root
    from src.cloud.archive import list_uploads
    root=project_root(pid)
    links=[]
    try:
        receipt=json.loads((root/'output/youtube_upload.json').read_text(encoding='utf-8'))
        vid=receipt.get('video_id','')
        if receipt.get('privacy_status')=='private' and re.fullmatch(r'[A-Za-z0-9_-]{6,64}',vid):
            links.append('📺 YouTube Private: https://www.youtube.com/watch?v='+vid)
    except (OSError,ValueError,AttributeError,TypeError):pass
    for row in list_uploads(pid):
        url=row.get('web_url') or ''
        if re.fullmatch(r'https://drive\.google\.com/file/d/[A-Za-z0-9_-]+/view',url):
            links.append('☁️ MP4 Drive: '+url)
    try:
        receipt=json.loads((root/'output/drive_metadata_archive.json').read_text(encoding='utf-8'))
        for row in receipt.get('files',[])[:8]:
            identifier=row.get('remote_id','')
            if re.fullmatch(r'[A-Za-z0-9_-]+',identifier):
                links.append('📄 '+Path(row.get('path','Hồ sơ')).name+': https://drive.google.com/file/d/'+identifier+'/view')
    except (OSError,ValueError,AttributeError,TypeError):pass
    send(service,'\n'.join(links) if links else 'Chưa có đường dẫn cloud đã lưu cho dự án.',
         [[button('⬅️ Dự án','project',project=pid)]])


def evidence(service,pid,index,kind):
    from src.api import app as api
    from src.pipeline.media_storage import project_root
    row=next((r for r in api.get_cues_endpoint(pid) if r['index']==index),None)
    if row is None:raise StudioError('Câu không còn tồn tại.')
    if kind=='frame':
        response=api.get_frame_endpoint(pid,(row['start']+row['end'])/2)
        if len(response.body)>9*1024**2:raise StudioError('Ảnh quá lớn để gửi Telegram.')
        with io.BytesIO(response.body) as stream:
            service.call('sendPhoto',dict(chat_id=settings.TELEGRAM_CHAT_ID,caption=f'{pid} · câu #{index}'),
                         files={'photo':('frame.jpg',stream,'image/jpeg')},timeout=30)
    else:
        root=project_root(pid)
        value=row.get('voice_audio_path')
        if not value:raise StudioError('Câu chưa có giọng đã tạo.')
        path=(root/value).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size>=35*1024**2:
            raise StudioError('File giọng không còn khả dụng.')
        with path.open('rb') as stream:
            service.call('sendDocument',dict(chat_id=settings.TELEGRAM_CHAT_ID,caption=f'{pid} · giọng câu #{index}'),
                         files={'document':(path.name,stream)},timeout=60)


def handle_text(text, service):
    command=text.strip().split()[0].split('@')[0].lower() if text.strip() else ''
    if command in {'/menu','/start'}: home(service);return True
    if command=='/new':new(service);return True
    if command in {'/projects','/running','/review','/storage'}:
        projects(service,kind={'/running':'running','/review':'review','/storage':'storage'}.get(command,'all'));return True
    if command=='/cancel':cancel_input(service);home(service);return True
    if command.startswith('/'):return False
    pending=transaction(service,lambda state:state.get('input'))
    if not pending or pending['expires']<time.time():
        url=assistant.extract_video_url(text)
        if url and transaction(service,lambda state:url in state.get('links',{})):
            download(service,url);return True
        return False
    if pending['kind']=='link':
        url=assistant.extract_video_url(text)
        if not url:service.message('Gửi link Bilibili/Douyin hợp lệ hoặc /cancel để thoát.');return True
        download(service,url);return True
    if pending['kind']=='edit':
        if not text.strip() or len(text)>2000:service.message('Lời Việt cần từ 1 đến 2.000 ký tự.');return True
        send(service,'Xác nhận sửa '+('nguồn' if pending.get('field')=='corrected_text' else 'lời Việt')+':\n'+text.strip(),[[button('✅ Lưu bản sửa','edit!',project=pending['project'],index=pending['index'],
             row=pending['row'],field=pending.get('field','vi_text'),text=text.strip(),revision=pending['revision'])],[button('Hủy','cancel_input')]])
        transaction(service,lambda state:state.pop('input',None));return True
    return False


def handle_callback(data, service, message_id):
    if not isinstance(data,str) or not data.startswith('ui:'):return False
    if service.stopping or not service.permitted():return True
    from src.api import app as api
    from src.pipeline import auto_workflow as auto
    token=data[3:]
    def consume(state):
        record=state['actions'].get(token)
        if not record or record['expires']<time.time() or record.get('message_id')!=message_id:
            raise StudioError('Nút đã hết hạn; mở /menu để lấy nút mới.')
        record=dict(record)
        if record['action'] in {'set','run!','extract!','delete!','resume!','edit!','stop','legacy','download','copy','drivefull!'}:
            state['actions'].pop(token)
        return record
    record=transaction(service,consume)
    action=record['action'];pid=record.get('project')
    def mutate(fn):
        # Check state and submit/write under the same lock; release before any
        # Telegram send so slow mobile networking cannot block Auto or cancel.
        with auto.lock, api.queue._lock:
            if record.get('revision') and fingerprint(pid)!=record['revision']:
                raise StudioError('Dự án đã thay đổi; mở lại thẻ dự án.')
            return fn()
    with nullcontext():
        if action=='home':home(service)
        elif action=='new':new(service)
        elif action=='copy':download(service,record['url'],force=True)
        elif action=='cancel_input':cancel_input(service);home(service)
        elif action=='status':service.message(assistant.status_text())
        elif action=='help':service.message('Menu → Tạo dự án → gửi link → tải nguồn → cấu hình → Chạy Auto.\n/projects: dự án · /review: cần duyệt · /cancel: thoát nhập.\nDừng tác vụ trước khi sửa/xóa. Xóa cả dự án mất cả phụ đề và hồ sơ trên máy; giải phóng media giữ hồ sơ. Máy tắt thì bot không nhận lệnh.')
        elif action=='list':projects(service,record.get('page',0),record.get('kind','all'))
        elif action=='project':project(service,pid)
        elif action=='cloudlinks':cloud_links(service,pid)
        elif action=='drivefull?':
            info=assistant.project_info(pid)
            send(service,f"Lưu MP4 đầy đủ lên Drive?\n{pid}\nDung lượng MP4: {info['mp4_bytes']/1024**3:.2f} GiB. Cần đủ dung lượng Drive; không phải gói hồ sơ nhẹ.",
                 [[button('✅ Tải MP4 lên Drive','drivefull!',project=pid,revision=fingerprint(pid))],
                  [button('⬅️ Dự án','project',project=pid)]])
        elif action=='drivefull!':
            from src.cloud.archive import archive_project
            assistant.require_account('drive')
            def upload(check):
                assistant.require_account('drive')
                return archive_project(pid,mode='full_delivery',should_continue=check)
            task=mutate(lambda:assistant._queued(pid,'TELEGRAM_DRIVE_FULL',upload,service))
            project(service,pid);observe(service,task.task_id,pid)
        elif action=='config':
            transaction(service,lambda state:state.update(actions={k:v for k,v in state['actions'].items()
                        if not(v['action']=='extract!' and v.get('project')==pid)}))
            config(service,pid)
        elif action=='extract?':
            api._require_editable(pid)
            d=draft(service,pid); mode=payload(d['values']).extraction_mode
            send(service,'⚠️ Nhận dạng lại bằng '+('nghe thoại' if mode=='stt' else 'OCR')+
                 '?\nDanh sách câu sẽ được tạo lại; bản sửa nguồn/dịch có thể mất và giọng/video phụ thuộc cần làm lại. Chỉ thực hiện khi bạn muốn thay nguồn nhận dạng.',
                 [[button('✅ Xác nhận nhận dạng lại','extract!',project=pid,mode=mode,version=d['version'],revision=fingerprint(pid))],
                  [button('⬅️ Cấu hình','config',project=pid)]])
        elif action=='extract!':
            from src.api.schemas import ExtractionRequest
            if draft(service,pid)['version']!=record['version']:raise StudioError('Cấu hình đã thay đổi; mở lại xác nhận.')
            task=mutate(lambda:api.start_ocr_endpoint(pid,ExtractionRequest(extraction_mode=record['mode'])))
            project(service,pid);observe(service,task['task_id'],pid)
        elif action=='choose':
            api._require_editable(pid);d=draft(service,pid);field=record['field']
            send(service,LABELS[field],[[button(label,'set',project=pid,field=field,value=value,version=d['version'])] for value,label in CHOICES[field]])
        elif action=='set':
            api._require_editable(pid)
            def update(state):
                d=state['drafts'][pid]
                if d['version']!=record['version']:raise StudioError('Cấu hình đã đổi; mở lại bảng cấu hình.')
                if record['value'] not in dict(CHOICES[record['field']]):raise StudioError('Lựa chọn không hợp lệ.')
                d['values'][record['field']]=record['value'];d['version']+=1
                if d['values']['product']=='voice_only' and d['values']['audio']=='original_only':d['values']['audio']='mix'
            transaction(service,update);config(service,pid)
        elif action=='run?':
            api._require_editable(pid);d=draft(service,pid);options=payload(d['values']).model_dump()
            send(service,'▶️ Chạy Auto với cấu hình đã chọn?\n'+pid+'\nCác bước có dữ liệu không còn khớp sẽ được xử lý lại. Không bỏ qua QA.',
                 [[button('✅ Chạy Auto','run!',project=pid,options=options,version=d['version'],revision=fingerprint(pid))],
                  [button('⬅️ Cấu hình','config',project=pid)]])
        elif action=='run!':
            from src.api.schemas import RunAllRequest
            if draft(service,pid)['version']!=record['version']:raise StudioError('Cấu hình đã thay đổi.')
            task=mutate(lambda:api.start_run_all(pid,RunAllRequest.model_validate(record['options'])))
            project(service,pid);observe(service,task['task_id'],pid)
        elif action=='download':
            task=mutate(lambda:api.start_download(pid));project(service,pid);observe(service,task['task_id'],pid)
        elif action=='resume?':
            send(service,'Duyệt bước nguồn/dịch hiện tại (nếu đang chờ) và tiếp tục Auto?\nGiữ cấu hình Auto đã lưu. QA/timing vẫn được kiểm tra.\n'+auto.content_warnings(pid),
                 [[button('✅ Xác nhận tiếp tục','resume!',project=pid,skip=record.get('skip',False),revision=fingerprint(pid))],
                  [button('📝 Xem câu trước','cues',project=pid,page=0)]])
        elif action=='resume!':mutate(lambda:assistant.resume(pid,skip=record.get('skip',False)));project(service,pid)
        elif action=='stop':
            def stop():
                task=api.queue.get_by_project(pid)
                if not task or task.task_id!=record['task_id']:raise StudioError('Tác vụ đã đổi; mở lại thẻ dự án.')
                if task.state in {'QUEUED','RUNNING'}:api.queue.cancel(task.task_id)
            mutate(stop)
            project(service,pid)
        elif action=='legacy':assistant.handle_callback(assistant.callback(record['command'],pid),service,message_id)
        elif action=='delete?':
            api._require_editable(pid)
            info=assistant.project_info(pid)
            send(service,f"⚠️ XÓA VĨNH VIỄN DỰ ÁN\n{info['title']}\n{pid}\nXóa nguồn, giọng, MP4, phụ đề, hồ sơ và bản ghi dự án trên máy. Bản cloud giữ nguyên. Không thể hoàn tác. Nếu chỉ cần dọn MP4, chọn Giải phóng media.",
                 [[button('🗑 Xác nhận xóa cả dự án','delete!',project=pid,revision=fingerprint(pid))],
                  [button('Hủy · giữ dự án','cancel_delete',project=pid)]])
        elif action=='cancel_delete':
            def cancel_delete(state):
                state['actions']={k:v for k,v in state['actions'].items() if not(v['action']=='delete!' and v.get('project')==pid)}
            transaction(service,cancel_delete);project(service,pid)
        elif action=='delete!':
            mutate(lambda:api.delete_project(pid))
            transaction(service,lambda state:state['drafts'].pop(pid,None))
            service.message('Đã xóa dự án '+pid);projects(service)
        elif action=='cues':cues(service,pid,record.get('page',0))
        elif action in {'frame','audio'}:
            assistant._queued(pid,'TELEGRAM_EVIDENCE',lambda _:evidence(service,pid,record['index'],action),service)
        elif action=='edit':
            api._require_editable(pid)
            cancel_input(service)
            transaction(service,lambda state:state.update(input=dict(kind='edit',project=pid,index=record['index'],row=record['row'],field=record.get('field','vi_text'),revision=fingerprint(pid),expires=time.time()+900)))
            service.message('Nhập nội dung '+('nguồn' if record.get('field')=='corrected_text' else 'tiếng Việt')+' thay thế cho câu #'+str(record['index'])+' của '+pid+'. Gửi /cancel để hủy. Sẽ có bước xác nhận trước khi lưu.')
        elif action=='edit!':
            def edit():
                current=next((r for r in api.get_cues_endpoint(pid) if r['index']==record['index']),None)
                if current!=record['row']:raise StudioError('Câu đã thay đổi; mở lại trước khi sửa.')
                field=record.get('field','vi_text')
                if field not in {'vi_text','corrected_text'}:raise StudioError('Trường sửa không hợp lệ.')
                return api.update_cue_endpoint(pid,record['index'],{field:record['text']})
            mutate(edit)
            service.message('Đã lưu câu vào dự án. Chạy tiếp Auto để cập nhật bản dịch/giọng/video cần làm lại.');project(service,pid)
    return True


def register_menu(service):
    scope=json.dumps(dict(type='chat',chat_id=settings.TELEGRAM_CHAT_ID))
    service.call('setMyCommands',dict(scope=scope,commands=json.dumps([dict(command=c,description=d) for c,d in COMMANDS],ensure_ascii=False)))
    service.call('setChatMenuButton',dict(chat_id=settings.TELEGRAM_CHAT_ID,menu_button=json.dumps(dict(type='commands'))))


def fast_action(service,data):
    if not isinstance(data,str) or not data.startswith('ui:'):return False
    action=transaction(service,lambda state:state.get('actions',{}).get(data[3:],{}).get('action'))
    return action in {'stop','resume!','cancel_input','cancel_delete','home','status','list','project'}
