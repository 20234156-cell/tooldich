"""Phone Studio integration with isolated DB, real queue and mock Telegram."""
import json
import threading
import time

import pytest
from src.notifications import studio, assistant
from src.api import app as api
from src.pipeline import auto_workflow as auto
from src.pipeline.workflow_options import resolve_workflow
from src.database import connection
from src.database.models import Project
from test_telegram_ai_assistant import bot, local_queue, make_project, update, wait_until


def press(bot, label):
    service, transport = bot
    for call in reversed(transport.calls):
        markup=json.loads(call['data'].get('reply_markup','{}'))
        for row in markup.get('inline_keyboard',[]):
            for b in row:
                if b['text']==label:
                    token=b['callback_data'][3:]
                    record=studio.transaction(service,lambda state:dict(state['actions'][token]))
                    studio.handle_callback(b['callback_data'],service,record['message_id'])
                    return b['callback_data'],record['message_id']
    raise AssertionError('Button absent: '+label)


def test_menu_registration_and_opaque_buttons(bot):
    studio.register_menu(bot[0]);studio.home(bot[0])
    assert {c['method'] for c in bot[1].calls}>={'setMyCommands','setChatMenuButton','sendMessage'}
    for c in bot[1].calls:
        for row in json.loads(c['data'].get('reply_markup','{}')).get('inline_keyboard',[]):
            assert all(len(b['callback_data'].encode())<30 for b in row)


def test_extract_again_requires_confirmation_and_uses_selected_source(bot,local_queue,monkeypatch):
    pid,_=make_project()
    studio.config(bot[0],pid)
    press(bot,'Nguồn để dịch')
    press(bot,'Nghe lời thoại')
    called=[]
    monkeypatch.setattr(api.orchestrator,'run_ocr',lambda *a,**kw:called.append(kw['extraction_mode']))
    press(bot,'🔎 Nhận dạng lại nguồn')
    assert called==[]
    token,mid=press(bot,'✅ Xác nhận nhận dạng lại')
    local_queue.get_by_project(pid).future.result(timeout=10)
    assert called==['stt']
    with pytest.raises(ValueError):studio.handle_callback(token,bot[0],mid)


def test_translation_caveats_survive_technical_approval(bot,monkeypatch):
    from src.pipeline.state_machine import PipelineState,ProjectState,StageMetadata
    pid,_=make_project()
    state=PipelineState(current_state=ProjectState.VERIFIED,stages={'TRANSLATED':StageMetadata(
        degraded=['translation_provider_fallback','ai_source_corrections_review'])})
    monkeypatch.setattr(api.orchestrator,'get_state',lambda _:state)
    studio.project(bot[0],pid)
    text='\n'.join(c['data'].get('text','') for c in bot[1].calls)
    assert 'nguồn dự phòng' in text and 'AI đã sửa chữ nguồn' in text


def test_back_to_configuration_revokes_extract_confirmation(bot):
    pid,_=make_project();studio.config(bot[0],pid)
    press(bot,'🔎 Nhận dạng lại nguồn')
    pending=studio.transaction(bot[0],lambda s:next((k,dict(v)) for k,v in s['actions'].items() if v['action']=='extract!'))
    press(bot,'⬅️ Cấu hình')
    with pytest.raises(ValueError):studio.handle_callback('ui:'+pending[0],bot[0],pending[1]['message_id'])


@pytest.mark.parametrize('product', ['dub','subtitles','voice_only'])
@pytest.mark.parametrize('source', ['screen','speech'])
@pytest.mark.parametrize('audio', ['mix','voice_only','original_only'])
def test_configuration_matches_actual_workflow(product,source,audio):
    if product=='voice_only' and audio=='original_only':
        with pytest.raises(ValueError):studio.payload(dict(product=product,source=source,audio=audio))
        return
    p=studio.payload(dict(product=product,source=source,audio=audio))
    plan=resolve_workflow(p.workflow_mode,p.processing_profile,extraction_mode=p.extraction_mode,audio_mode=p.audio_mode)
    assert plan.extraction_mode==('hardsub' if source=='screen' else 'stt')
    assert plan.audio_mode==('original_only' if product=='subtitles' else audio)
    if product=='voice_only':assert p.subtitle_style=='original' and p.subtitle_preset is None


def test_new_download_waits_for_configuration_before_auto(bot,local_queue,monkeypatch):
    gate=threading.Event()
    monkeypatch.setattr('src.pipeline.media_storage.auto_disk_guard_if_needed',lambda:None)
    monkeypatch.setattr(api.manager,'download_and_validate_project',lambda *a,**k:gate.wait(3) or {})
    started=[]
    monkeypatch.setattr(api,'start_run_all',lambda *a,**k:started.append(a))
    studio.new(bot[0])
    try:
        assert studio.handle_text('https://www.bilibili.com/video/BV1234567890/',bot[0])
        with connection.SessionLocal() as db:pid=db.query(Project).one().id
        assert not started
        gate.set();local_queue.get_by_project(pid).future.result(timeout=10)
        wait_until(lambda:any('CẤU HÌNH AUTO' in c['data'].get('text','') for c in bot[1].calls))
        assert not started
    finally:gate.set()


def test_draft_revision_stale_and_double_run(bot,local_queue,monkeypatch):
    pid,_=make_project()
    studio.config(bot[0],pid)
    press(bot,'Tốc độ')
    stale=next(c for c in reversed(bot[1].calls) if 'reply_markup' in c['data'])
    press(bot,'1.25x')
    old=json.loads(stale['data']['reply_markup'])['inline_keyboard'][0][0]['callback_data']
    record=studio.transaction(bot[0],lambda s:dict(s['actions'][old[3:]]))
    with pytest.raises(ValueError):studio.handle_callback(old,bot[0],record['message_id'])
    monkeypatch.setattr(api,'_run_fast_hardsub_pipeline',lambda *a,**kw:dict(verification={'passed':True}))
    press(bot,'▶️ Kiểm tra & chạy Auto')
    token,message_id=press(bot,'✅ Chạy Auto')
    local_queue.get_by_project(pid).future.result(timeout=10)
    assert auto.read(pid)['options']['rate']=='+25%'
    with pytest.raises(ValueError):studio.handle_callback(token,bot[0],message_id)


def test_delete_requires_bound_confirmation_and_idle_project(bot,local_queue):
    pid,root=make_project()
    studio.project(bot[0],pid);press(bot,'🗑 Xóa cả dự án')
    assert root.is_dir()
    press(bot,'Hủy · giữ dự án');assert root.is_dir()
    press(bot,'🗑 Xóa cả dự án')
    press(bot,'🗑 Xác nhận xóa cả dự án')
    assert not root.exists()
    with connection.SessionLocal() as db:assert db.get(Project,pid) is None


def test_edit_translation_saves_db_only_after_confirmation(bot,local_queue):
    pid,_=make_project()
    studio.cues(bot[0],pid);press(bot,'✏️ Sửa lời Việt')
    studio.handle_text('Lời Việt sửa từ điện thoại',bot[0])
    assert api.get_cues_endpoint(pid)[0]['vi_text']=='Lời Việt'
    press(bot,'✅ Lưu bản sửa')
    assert api.get_cues_endpoint(pid)[0]['vi_text']=='Lời Việt sửa từ điện thoại'


def test_other_chat_cannot_open_remote_menu(bot):
    assert not bot[0].process_update(update(1,text='/menu',chat='unknown'))
    assert not bot[1].calls


def test_message_binding_and_expiry(bot):
    studio.home(bot[0])
    token,record=studio.transaction(bot[0],lambda s:next(iter(s['actions'].items())))
    with pytest.raises(ValueError):studio.handle_callback('ui:'+token,bot[0],record['message_id']+1)
    studio.transaction(bot[0],lambda s:s['actions'][token].update(expires=0))
    with pytest.raises(ValueError):studio.handle_callback('ui:'+token,bot[0],record['message_id'])


def test_cancel_revokes_delete_and_edit_confirmation(bot,local_queue):
    pid,root=make_project()
    studio.project(bot[0],pid);press(bot,'🗑 Xóa cả dự án')
    token,record=studio.transaction(bot[0],lambda s:next((k,dict(v)) for k,v in s['actions'].items() if v['action']=='delete!'))
    press(bot,'Hủy · giữ dự án')
    with pytest.raises(ValueError):studio.handle_callback('ui:'+token,bot[0],record['message_id'])
    assert root.exists()
    studio.cues(bot[0],pid);press(bot,'✏️ Sửa lời Việt');studio.handle_text('Không lưu',bot[0])
    token,record=studio.transaction(bot[0],lambda s:next((k,dict(v)) for k,v in s['actions'].items() if v['action']=='edit!'))
    studio.handle_text('/cancel',bot[0])
    with pytest.raises(ValueError):studio.handle_callback('ui:'+token,bot[0],record['message_id'])
    assert api.get_cues_endpoint(pid)[0]['vi_text']=='Lời Việt'


def test_web_change_invalidates_phone_edit(bot,local_queue):
    pid,_=make_project()
    studio.cues(bot[0],pid);press(bot,'✏️ Sửa lời Việt');studio.handle_text('Phone change',bot[0])
    api.update_cue_endpoint(pid,1,{'vi_text':'Web change'})
    with pytest.raises(ValueError):press(bot,'✅ Lưu bản sửa')
    assert api.get_cues_endpoint(pid)[0]['vi_text']=='Web change'


def test_durable_menu_survives_service_recreation(bot,local_queue):
    from src.notifications.telegram_bot import TelegramBotService
    pid,_=make_project();studio.project(bot[0],pid)
    token,record=studio.transaction(bot[0],lambda s:next((k,dict(v)) for k,v in s['actions'].items() if v['action']=='config'))
    second=TelegramBotService(bot[1],testing=True)
    assert studio.handle_callback('ui:'+token,second,record['message_id'])
    assert 'CẤU HÌNH AUTO' in bot[1].calls[-1]['data']['text']


def test_style_presets_match_web_actual_values():
    import subprocess,shutil
    from pathlib import Path
    node=shutil.which('node')
    if not node:pytest.skip('Node required')
    source=Path('web/app.js').read_text(encoding='utf-8')
    function=source[source.index('function resolveStylePreset('):source.index('const launchTrimMode')]
    names=[v for v,_ in studio.CHOICES['style']]
    out=subprocess.check_output([node,'-e',function+'\nconsole.log(JSON.stringify('+json.dumps(names)+'.map(v=>resolveStylePreset("preset:"+v))))'],text=True,encoding='utf-8')
    for name,web in zip(names,json.loads(out)):
        options=studio.payload({'style':name}).model_dump()
        for key,value in web.items():
            if key!='style':assert options[key]==value


def test_repeated_wizard_link_does_not_create_duplicate(bot,local_queue,monkeypatch):
    monkeypatch.setattr('src.pipeline.media_storage.auto_disk_guard_if_needed',lambda:None)
    monkeypatch.setattr(api.manager,'download_and_validate_project',lambda *a,**kw:{})
    studio.new(bot[0]);url='https://www.bilibili.com/video/BV1234567890/'
    studio.handle_text(url,bot[0])
    with connection.SessionLocal() as db:pid=db.query(Project).one().id
    local_queue.get_by_project(pid).future.result(timeout=10)
    studio.handle_text(url,bot[0])
    with connection.SessionLocal() as db:assert db.query(Project).count()==1
    assert any('Link này đã có dự án' in c['data'].get('text','') for c in bot[1].calls)


def test_progress_edits_card_without_duplicate_or_stale_stop(bot,local_queue):
    pid,_=make_project()
    gate=threading.Event()
    task=local_queue.submit(pid,'test',lambda:gate.wait(5),update_pipeline_job=False)
    try:
        wait_until(lambda:task.state=='RUNNING')
        studio.project(bot[0],pid)
        task.record_progress(35,'Đang thử tiến độ')
        studio.update_progress(bot[0]);studio.update_progress(bot[0])
        edits=[c for c in bot[1].calls if c['method']=='editMessageText']
        assert len(edits)==1 and '35%' in edits[0]['data']['text']
        press(bot,'⏹ Yêu cầu dừng')
        assert task.cancel_requested
    finally:
        gate.set();task.future.result(timeout=10)


def test_busy_project_blocks_delete_confirmation(bot,local_queue):
    pid,root=make_project();studio.project(bot[0],pid);press(bot,'🗑 Xóa cả dự án')
    gate=threading.Event()
    task=local_queue.submit(pid,'test',lambda:gate.wait(5),update_pipeline_job=False)
    try:
        with pytest.raises(ValueError):press(bot,'🗑 Xác nhận xóa cả dự án')
        assert root.is_dir()
    finally:gate.set();task.future.result(timeout=10)


def test_full_drive_confirm_uses_correct_mode(bot,local_queue,monkeypatch):
    pid,_=make_project();calls=[]
    monkeypatch.setattr(assistant,'require_account',lambda kind:None)
    monkeypatch.setattr('src.cloud.archive.archive_project',lambda project_id,**kw:calls.append((project_id,kw['mode'])) or {})
    studio.project(bot[0],pid);press(bot,'☁️ Lưu MP4 đầy đủ Drive')
    assert not calls
    press(bot,'✅ Tải MP4 lên Drive')
    local_queue.get_by_project(pid).future.result(timeout=10)
    assert calls==[(pid,'full_delivery')]


def test_cloud_links_only_known_provider_urls(bot,monkeypatch):
    pid,root=make_project()
    (root/'output/youtube_upload.json').write_text(json.dumps({'privacy_status':'private','video_id':'abc_1234','youtube_url':'https://evil.test/'}))
    monkeypatch.setattr('src.cloud.archive.list_uploads',lambda _: [{'web_url':'https://evil.test/'},{'web_url':'https://drive.google.com/file/d/abc_12/view'}])
    studio.cloud_links(bot[0],pid)
    text=bot[1].calls[-1]['data']['text']
    assert 'evil.test' not in text and 'watch?v=abc_1234' in text and 'drive.google.com/file/d/abc_12/view' in text
