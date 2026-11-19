"""Telegram command/control regressions: temporary DB/media and explicit mock transport only."""
import base64
import datetime as dt
import importlib
import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from config.settings import settings
from src.database import connection
from src.database.models import Project, Job, Media, Transcript
from src.notifications import telegram, assistant
from src.notifications.telegram_bot import TelegramBotService
from src.pipeline import media_storage as media
from src.pipeline import auto_workflow as auto
from src.pipeline.state_machine import PipelineState, ProjectState, save_pipeline_state
from src.api import app as api


class Transport:
    def __init__(self):
        self.calls = []
        self.identifier = 100
        self.updates = []

    def __call__(self, method, data, files=None, timeout=10):
        self.identifier += 1
        record = dict(method=method, data=dict(data), timeout=timeout,
                      payload=files['video'][1].read() if files and 'video' in files else None)
        self.calls.append(record)
        if method == 'getUpdates':
            rows, self.updates = self.updates, []
            return rows
        return {'message_id': self.identifier}


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setattr(settings, 'TELEGRAM_NOTIFY_ENABLED', True)
    monkeypatch.setattr(settings, 'TELEGRAM_BOT_TOKEN', 'opaque-test-bot-token')
    monkeypatch.setattr(settings, 'TELEGRAM_CHAT_ID', '7777777')
    monkeypatch.setattr(settings, 'TELEGRAM_POLLING_ENABLED', True)
    monkeypatch.setattr(settings, 'TELEGRAM_POLL_INTERVAL_SECONDS', .5)
    transport = Transport()
    service = TelegramBotService(transport, testing=True)
    yield service, transport
    service.stop()


@pytest.fixture
def local_queue(monkeypatch):
    queue_module = importlib.import_module('src.jobs.queue')
    queue = queue_module.JobQueue(max_workers=1)
    monkeypatch.setattr(queue_module, 'queue', queue)
    monkeypatch.setattr(api, 'queue', queue)
    yield queue
    queue._executor.shutdown(wait=True)


def make_project(identifier='proj_test', *, status='VERIFIED', duration=1000, created=None):
    root = settings.storage_path/identifier
    (root/'output').mkdir(parents=True)
    (root/'source').mkdir()
    (root/'transcript').mkdir()
    (root/'output/video_vi.mp4').write_bytes(b'original-video')
    (root/'transcript/vi.srt').write_text('phụ đề', encoding='utf-8')
    (root/'source/notes.txt').write_text('kịch bản', encoding='utf-8')
    (root/'source/copy.sqlite').write_bytes(b'local-sqlite')
    with connection.SessionLocal() as db:
        db.add(Project(id=identifier, title='Video <script>alert(1)</script>', status=status,
                       created_at=created or dt.datetime(2026, 1, 1)))
        db.add(Job(id='job_'+identifier, project_id=identifier, status='COMPLETED'))
        db.add(Media(project_id=identifier, file_path=str(root/'output/video_vi.mp4'), duration=duration))
        db.add(Transcript(project_id=identifier, language='zh', segments=[dict(index=1, start=0, end=1, text='原文', vi_text='Lời Việt')]))
        db.commit()
    save_pipeline_state(root, PipelineState(current_state=ProjectState.VERIFIED))
    return identifier, root


def update(identifier, *, text=None, data=None, chat='7777777', message_id=1):
    message = dict(chat={'id': chat}, message_id=message_id)
    if data is not None:
        return dict(update_id=identifier, callback_query=dict(id='cb_'+str(identifier), message=message, data=data))
    return dict(update_id=identifier, message=dict(message, text=text))


def wait_until(condition):
    deadline = time.monotonic()+4
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(.01)
    assert condition()


def test_polling_lifecycle_and_offset_persistence(bot, monkeypatch):
    service, transport = bot
    path = assistant.private_path('telegram_offset.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'last_update_id': 40}))
    handled = []
    monkeypatch.setattr(assistant, 'handle_message', lambda text, _: handled.append(text))
    transport.updates = [update(41, text='lạ', chat='not-owner'), update(42, text='hướng dẫn')]
    assert service.start()
    wait_until(lambda: handled == ['hướng dẫn'])
    assert json.loads(path.read_text())['last_update_id'] == 42
    assert next(c for c in transport.calls if c['method']=='getUpdates')['data']['offset'] == 41
    assert not service.process_update(update(42, text='không chạy lại'))
    service.stop()
    assert not service.status()['listening']
    assert service._thread is None and service._command_thread is None
    assert any(c['timeout'] == 2 and 'tạm nghỉ' in c['data'].get('text', '') for c in transport.calls)
    restarted = TelegramBotService(Transport(), testing=True)
    assert not restarted.process_update(update(42, text='cũ'))
    assert handled == ['hướng dẫn']


def test_first_install_discards_backlog_and_single_owner(bot, monkeypatch):
    service, transport = bot
    handled = []
    monkeypatch.setattr(assistant, 'handle_message', lambda text, _: handled.append(text))
    transport.updates = [update(80, text='link cũ')]
    assert service.start()
    wait_until(lambda: service._last_update_id == 80)
    other = TelegramBotService(Transport(), testing=True)
    try:
        assert not other.start()
        assert not handled
        assert next(c for c in transport.calls if c['method']=='getUpdates')['data']['offset'] == -1
    finally:
        other.stop()


def test_status_control_lane_does_not_wait_for_gemini(bot, monkeypatch):
    service, transport = bot
    offset = assistant.private_path('telegram_offset.json')
    offset.parent.mkdir(parents=True, exist_ok=True)
    offset.write_text('{"last_update_id":0}')
    entered, release = threading.Event(), threading.Event()
    def slow(*args):
        entered.set()
        release.wait(4)
        return 'AI đã trả lời'
    monkeypatch.setattr(assistant, 'ask_gemini', slow)
    assert service.start()
    try:
        service.process_update(update(1, text='Tôi nên làm gì tiếp?'), enqueue=True)
        assert entered.wait(2)
        service.process_update(update(2, text='/status'), enqueue=True)
        wait_until(lambda: any('Ổ dữ liệu' in c['data'].get('text', '') for c in transport.calls))
        assert not release.is_set()
    finally:
        release.set()
        service.stop()


def test_offset_write_failure_never_executes_command(bot, monkeypatch):
    service, _ = bot
    called = []
    monkeypatch.setattr(assistant, 'handle_message', lambda *a: called.append(a))
    monkeypatch.setattr('src.notifications.state.durable_json', lambda *a: (_ for _ in ()).throw(OSError('disk')))
    with pytest.raises(OSError):
        service.process_update(update(1, text='link'))
    assert not called


def test_default_pytest_and_offline_never_start_polling_or_call_network(bot, monkeypatch):
    calls = []
    monkeypatch.setattr(telegram, 'network_allowed', lambda: True)
    default = TelegramBotService(lambda *a, **k: calls.append(a))
    assert not default.start()
    assert not default.process_update(update(1, text='/status'))
    monkeypatch.setattr(settings, 'OFFLINE_MODE', True)
    assert not bot[0].start()
    assert not bot[0].process_update(update(1, text='/status'))
    assert not calls


@pytest.mark.parametrize('event', ['OCR_REVIEW_REQUIRED', 'TIMING_REVIEW_REQUIRED', 'QA_REVIEW_REQUIRED', 'VERIFIED', 'delete'])
def test_callback_data_strictly_under_64_bytes(event):
    markup = assistant.keyboard('proj_123456789abc', event)
    for row in markup['inline_keyboard']:
        for button in row:
            assert len(button['callback_data'].encode('utf-8')) < 30 <= 64
    assert assistant.keyboard('x'*60, event)['inline_keyboard'] == []


def test_gate_a_uses_error_code_and_b_has_four_buttons(bot, monkeypatch):
    identifier, root = make_project()
    calls = []
    monkeypatch.setattr(telegram, 'enabled', lambda: True)
    monkeypatch.setattr(telegram, '_send', lambda method, data, files=None: calls.append((method, data)))
    telegram.notify_project_event(identifier, 'OCR_REVIEW_REQUIRED', 'Cần duyệt', details={'cue_indices': [1, 2]})
    first = calls[0][1]
    assert '2' in first['text']
    buttons = json.loads(first['reply_markup'])['inline_keyboard']
    assert buttons[0][0]['callback_data'] == 'skip:'+identifier
    calls.clear()
    telegram.notify_project_event(identifier, 'TIMING_REVIEW_REQUIRED', 'Lệch', details={'timing_cause_cues': [1]})
    assert '1' in calls[0][1]['text'] and 'res:' in calls[0][1]['reply_markup']
    calls.clear()
    telegram.notify_project_event(identifier, 'VERIFIED', 'QA đạt')
    assert 'video dài' in calls[0][1]['text']
    actions = [b['callback_data'].split(':')[0] for row in json.loads(calls[0][1]['reply_markup'])['inline_keyboard'] for b in row]
    assert actions == ['demo', 'yt', 'drv', 'del?', 'open']


def test_two_step_delete_confirmation_flow(bot, local_queue):
    service, transport = bot
    identifier, root = make_project()
    original = (root/'output/video_vi.mp4').read_bytes()
    # A forged confirmation, even from the right chat, has no authority.
    service.process_update(update(1, data='del!:'+identifier, message_id=999))
    assert (root/'output/video_vi.mp4').read_bytes() == original
    service.process_update(update(2, data='del?:'+identifier))
    assert (root/'output/video_vi.mp4').read_bytes() == original
    pending = json.loads(assistant.private_path('telegram_actions.json').read_text())['deletions'][identifier]
    service.process_update(update(3, data='del!:'+identifier, message_id=pending['message_id']))
    task = local_queue.get_by_project(identifier)
    task.future.result(timeout=10)
    assert task.state == 'DONE'
    assert not (root/'output/video_vi.mp4').exists()
    assert (root/'transcript/vi.srt').read_text(encoding='utf-8') == 'phụ đề'
    assert (root/'source/notes.txt').exists() and (root/'source/copy.sqlite').read_bytes() == b'local-sqlite'
    with connection.SessionLocal() as db:
        assert db.get(Project, identifier).status == 'VERIFIED'
        assert db.query(Transcript).filter_by(project_id=identifier).one().segments[0]['text'] == '原文'
    assert any('Đã giải phóng' in c['data'].get('text', '') for c in transport.calls)


def test_delete_cancel_expiry_changed_media_and_busy_project(bot, local_queue):
    service, _ = bot
    identifier, root = make_project()
    service.confirm(identifier, 10, media.media_identity(identifier))
    service.process_update(update(1, data='delx:'+identifier, message_id=10))
    with pytest.raises(ValueError):
        service.take_confirmation(identifier, 10)
    service.confirm(identifier, 11, media.media_identity(identifier))
    pending = service.take_confirmation(identifier, 11)
    (root/'output/video_vi.mp4').write_bytes(b'changed')
    with pytest.raises(ValueError, match='thay đổi'):
        media.release_project_media(identifier, expected_identity=pending['identity'])
    event = threading.Event()
    task = local_queue.submit(identifier, 'block', lambda: event.wait(3), update_pipeline_job=False)
    try:
        with pytest.raises(ValueError, match='đang xử lý'):
            media.release_project_media(identifier)
    finally:
        event.set(); task.future.result(timeout=5)


def test_create_demo_clip_and_cleanup(bot, local_queue):
    from src.pipeline.render import get_ffmpeg_bin, get_ffprobe_bin
    service, transport = bot
    identifier, root = make_project(duration=121)
    target = root/'output/video_vi.mp4'
    subprocess.run([str(get_ffmpeg_bin()), '-nostdin', '-v', 'error', '-y', '-f', 'lavfi',
        '-i', 'color=s=160x90:r=5:d=121', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=121',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', '-shortest', str(target)], check=True, timeout=60)
    original = target.read_bytes()
    demo = media.create_telegram_demo_clip(identifier)
    try:
        assert demo['duration_seconds'] == 120
        assert demo['path'].stat().st_size < 35*1024**2
        measured = json.loads(subprocess.check_output([str(get_ffprobe_bin()), '-v', 'error',
            '-show_entries', 'format=duration', '-of', 'json', str(demo['path'])], timeout=30))
        assert abs(float(measured['format']['duration'])-120) < .1
    finally:
        demo['path'].unlink(missing_ok=True)
    assistant.send_demo(identifier, service)
    assert any(c['method'] == 'sendVideo' and c['payload'] for c in transport.calls)
    assert not (root/'output/demo_preview_tele.mp4').exists()
    assert target.read_bytes() == original


def test_demo_cleanup_on_failed_send_and_size_guard(bot, monkeypatch):
    identifier, root = make_project()
    temporary = root/'output/demo_preview_tele.mp4'
    temporary.write_bytes(b'demo')
    monkeypatch.setattr(media, 'create_telegram_demo_clip', lambda _: dict(path=temporary, start_seconds=1, duration_seconds=120))
    monkeypatch.setattr(bot[0], 'call', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('send failed')))
    with pytest.raises(RuntimeError):
        assistant.send_demo(identifier, bot[0])
    assert not temporary.exists()


@pytest.mark.parametrize('url', ['https://www.bilibili.com/video/BV1234567890/', 'https://b23.tv/abc',
                                 'https://v.douyin.com/abc/', 'https://www.iesdouyin.com/share/video/123/'])
def test_telegram_link_ingestion_starts_auto_workflow(bot, local_queue, monkeypatch, url):
    seen = []
    monkeypatch.setattr(media, 'auto_disk_guard_if_needed', lambda: seen.append('guard'))
    monkeypatch.setattr(media, 'storage_free_bytes', lambda: 20*1024**3)
    def pipeline(project_id, **kwargs):
        seen.append((project_id, kwargs))
        return dict(verification={'passed': False})
    monkeypatch.setattr(api, '_run_fast_hardsub_pipeline', pipeline)
    bot[0].process_update(update(1, text='Làm video '+url))
    with connection.SessionLocal() as db:
        project = db.query(Project).one()
        identifier = project.id
    task = local_queue.get_by_project(identifier)
    task.future.result(timeout=10)
    assert task.action == 'run-all' and task.state == 'DONE'
    assert seen[0] == 'guard'
    saved = auto.read(identifier)['options']
    assert saved['processing_profile'] == 'compact'
    assert saved['voice'] == 'piper:ngochuyen' and saved['rate'] == '+15%'
    assert saved['skip_uncertain'] and saved['audio_mode'] == 'mix'
    assert any('stop:'+identifier in c['data'].get('reply_markup', '') for c in bot[1].calls)


@pytest.mark.parametrize('text', ['https://bilibili.com.evil.test/video/abc', 'https://douyin.com@evil.test/x',
                                  'https://evil.test/douyin.com/a', 'file:///D:/private.txt'])
def test_ingestion_rejects_spoofed_and_local_urls(text):
    assert assistant.extract_video_url(text) is None


def test_natural_language_assistant_and_auto_disk_guard(bot, local_queue, monkeypatch):
    service, transport = bot
    ids = []
    for i, state in enumerate(['VERIFIED', 'FAILED', 'VERIFIED', 'VERIFIED', 'VERIFIED']):
        identifier, root = make_project(f'proj_guard{i}', status=state, created=dt.datetime(2026, 1, i+1))
        with (root/'output/video_vi.mp4').open('wb') as stream:
            stream.truncate(101*1024**2)
        ids.append(identifier)
    monkeypatch.setattr(media, 'storage_free_bytes', lambda: (9 if not (settings.storage_path/ids[0]/'output/video_vi.mp4').exists() else 1)*1024**3)
    monkeypatch.setattr(media, 'backup_status', lambda p: dict(media_backup=p in ids[:3], youtube=True, drive=False, drive_mode=None))
    monkeypatch.setattr(settings, 'AUTO_DISK_GUARD_ENABLED', True)
    monkeypatch.setattr(settings, 'AUTO_DISK_GUARD_MIN_FREE_GB', 8.)
    service.process_update(update(1, text='trạng thái ổ D'))
    assert any('Ổ dữ liệu' in c['data'].get('text', '') for c in transport.calls)
    result = media.auto_disk_guard_if_needed()
    assert result['projects'] == [ids[0]]
    for identifier in ids[1:]:
        assert (settings.storage_path/identifier/'output/video_vi.mp4').exists()
    assert (settings.storage_path/ids[0]/'transcript/vi.srt').exists()
    assert (settings.storage_path/ids[0]/'source/copy.sqlite').exists()


def test_guard_skips_busy_unbacked_and_metadata_only(bot, local_queue, monkeypatch):
    identifier, root = make_project()
    with (root/'output/video_vi.mp4').open('wb') as stream:
        stream.truncate(101*1024**2)
    monkeypatch.setattr(media, 'storage_free_bytes', lambda: 1)
    monkeypatch.setattr(media, 'backup_status', lambda _: dict(media_backup=False, drive=True, drive_mode='metadata_only'))
    assert not media.auto_disk_guard_if_needed()['projects']
    monkeypatch.setattr(media, 'backup_status', lambda _: dict(media_backup=True))
    event = threading.Event()
    task = local_queue.submit(identifier, 'upload', lambda: event.wait(3), update_pipeline_job=False)
    try:
        assert not media.auto_disk_guard_if_needed()['projects']
        assert (root/'output/video_vi.mp4').exists()
    finally:
        event.set(); task.future.result(timeout=5)


def test_ai_snapshot_is_read_only_and_provider_failure_falls_back(bot, monkeypatch):
    make_project()
    captured = []
    def ai(text, snapshot):
        captured.append(snapshot)
        raise RuntimeError(settings.TELEGRAM_BOT_TOKEN+' provider URL')
    monkeypatch.setattr(assistant, 'ask_gemini', ai)
    bot[0].process_update(update(1, text='Tôi nên làm gì tiếp theo?'))
    assert captured[0]['projects'][0]['id'] == 'proj_test'
    texts = '\n'.join(c['data'].get('text', '') for c in bot[1].calls)
    assert 'Ổ dữ liệu' in texts and settings.TELEGRAM_BOT_TOKEN not in texts


def test_stale_approval_keyboard_is_rejected_and_skip_uses_standard_auto(bot, local_queue, monkeypatch):
    identifier, _ = make_project()
    auto.write(identifier, {'version': 1, 'generation': 'old', 'options': {}, 'state': 'failed'})
    reply = bot[0].message('Duyệt', assistant.keyboard(identifier, 'OCR_REVIEW_REQUIRED'))
    profile = auto.read(identifier)
    profile['generation'] = 'new'
    auto.write(identifier, profile)
    called = []
    monkeypatch.setattr(assistant, 'resume', lambda *a, **k: called.append((a, k)))
    bot[0].process_update(update(1, data='res:'+identifier, message_id=reply['message_id']))
    assert not called
    fresh = bot[0].message('Duyệt mới', assistant.keyboard(identifier, 'OCR_REVIEW_REQUIRED'))
    bot[0].process_update(update(2, data='skip:'+identifier, message_id=fresh['message_id']))
    assert called[0][1]['skip'] is True


def test_backup_receipts_are_validated_not_trusted_by_filename(monkeypatch):
    identifier, root = make_project()
    (root/'output/youtube_upload.json').write_text('{"privacy_status":"private","video_id":"forged_video"}')
    (root/'output/drive_archive.json').write_text('{"status":"VERIFIED","mode":"metadata_only"}')
    assert media.backup_status(identifier)['media_backup'] is False


def test_configuration_and_status_are_secret_free_and_account_binding_is_scoped(bot, monkeypatch):
    from src.cloud import drive, youtube_upload
    monkeypatch.setattr(drive, 'seal', lambda value: base64.b64encode(json.dumps(value).encode()).decode())
    monkeypatch.setattr(drive, 'unseal', lambda value: json.loads(base64.b64decode(value)))
    youtube = dict(refresh_token='oauth-refresh-not-public', connection_id='first')
    monkeypatch.setattr(youtube_upload, 'config', lambda: youtube)
    monkeypatch.setattr(drive, 'config', lambda: dict(refresh_token='drive-private', email=assistant.ACCOUNT))
    with TestClient(api.app) as client:
        response = client.post('/api/notifications/telegram/config', json={'TELEGRAM_BOT_TOKEN': 'new-secret-token',
            'TELEGRAM_CHAT_ID': '7777777', 'confirm_youtube_account': True})
        assert response.status_code == 200, response.text
        assert 'new-secret-token' not in response.text and 'oauth-refresh-not-public' not in response.text
        assert '7777777' not in response.text
        assert response.json()['snapshot']['cloud']['youtube']['identity_confirmed']
        assert not response.json()['listening']  # pytest cannot launch the production poller
        wrong_origin = client.post('/api/notifications/telegram/test', json={}, headers={'Origin': 'https://evil.test'})
        assert wrong_origin.status_code == 403
    youtube['connection_id'] = 'reconnected'
    assert not assistant.cloud_status()['youtube']['identity_confirmed']


def test_callbacks_from_other_chat_have_no_side_effects(bot, monkeypatch):
    calls = []
    monkeypatch.setattr(assistant, 'handle_callback', lambda *a: calls.append(a))
    assert not bot[0].process_update(update(1, data='del!:proj_test', chat='attacker'))
    assert not calls and not bot[1].calls


def test_expired_confirmation_and_corrupt_offset_fail_closed(bot):
    identifier, root = make_project()
    service, _ = bot
    service.confirm(identifier, 10, media.media_identity(identifier))
    service._action_state(lambda state: state['deletions'][identifier].update(expires=0))
    with pytest.raises(ValueError):
        service.take_confirmation(identifier, 10)
    path = assistant.private_path('telegram_offset.json')
    path.write_text('{broken')
    with pytest.raises(ValueError):
        service.process_update(update(1, data='del!:'+identifier, message_id=10))
    assert (root/'output/video_vi.mp4').exists()


def test_skip_updates_saved_auto_without_forcing_source_approval(bot, local_queue, monkeypatch):
    identifier, root = make_project()
    save_pipeline_state(root, PipelineState(current_state=ProjectState.OCR_READY))
    auto.write(identifier, {'version': 1, 'generation': 'before', 'options': {'skip_uncertain': False}, 'state': 'needs_review'})
    monkeypatch.setattr(api.orchestrator, 'approve_source_cues', lambda *a: pytest.fail('Do not force approval'))
    monkeypatch.setattr(api, '_run_fast_hardsub_pipeline', lambda *a, **kw: {'verification': {'passed': False}})
    reply = bot[0].message('Nguồn cần duyệt', assistant.keyboard(identifier, 'OCR_REVIEW_REQUIRED'))
    bot[0].process_update(update(1, data='skip:'+identifier, message_id=reply['message_id']))
    task = local_queue.get_by_project(identifier)
    task.future.result(timeout=5)
    assert auto.read(identifier)['options']['skip_uncertain'] is True
    assert api.orchestrator.get_state(identifier).current_state == ProjectState.OCR_READY


def test_upload_queue_rechecks_account_and_notification_failure_keeps_success(bot, local_queue, monkeypatch):
    from src.cloud import youtube_upload
    identifier, _ = make_project()
    calls = []
    checks = []
    monkeypatch.setattr(assistant, 'require_account', lambda kind: checks.append(kind))
    monkeypatch.setattr(youtube_upload, 'upload', lambda project_id, **kw: calls.append((project_id, kw)) or {'video_id': 'private_video'})
    assistant.handle_callback('yt:'+identifier, bot[0], 1)
    task = local_queue.get_by_project(identifier)
    task.future.result(timeout=5)
    assert task.state == 'DONE' and task.job_id is None
    assert len(checks) == 2 and calls[0][1]['force'] is False
    # Cloud tasks must not fail just because their completion message cannot be delivered.
    monkeypatch.setattr(bot[0], 'message', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('offline Telegram')))
    task = assistant._queued(identifier, 'YOUTUBE_UPLOAD', lambda _: {'ok': True}, bot[0])
    task.future.result(timeout=5)
    assert task.state == 'DONE'


def test_queued_upload_does_not_follow_a_reconnected_account(bot, local_queue, monkeypatch):
    identifier, _ = make_project()
    checks = []
    def require(kind):
        checks.append(kind)
        if len(checks) > 1:
            raise ValueError('Connection changed')
    monkeypatch.setattr(assistant, 'require_account', require)
    from src.cloud import youtube_upload
    monkeypatch.setattr(youtube_upload, 'upload', lambda *a, **kw: pytest.fail('Wrong account must not upload'))
    assistant.handle_callback('yt:'+identifier, bot[0], 1)
    task = local_queue.get_by_project(identifier)
    task.future.result(timeout=5)
    assert task.state == 'FAILED'


def test_oversized_demo_is_rejected_and_removed(bot, monkeypatch):
    identifier, root = make_project()
    def run(command, **kw):
        if '-show_entries' in command:
            return SimpleNamespace(stdout='{"format":{"duration":"900"}}')
        path = Path(command[-1])
        with path.open('wb') as stream:
            stream.truncate(35*1024**2)
        return SimpleNamespace()
    monkeypatch.setattr(media.__dict__.get('subprocess', subprocess), 'run', run)
    with pytest.raises(ValueError, match='35 MiB'):
        media.create_telegram_demo_clip(identifier)
    assert not (root/'output/demo_preview_tele.mp4').exists()


def test_demo_selection_uses_db_even_when_intentionally_empty(bot, monkeypatch):
    identifier, root = make_project()
    (root/'voiceover').mkdir()
    (root/'voiceover/timeline.json').write_text(json.dumps([dict(start=900+i/10) for i in range(100)]))
    with connection.SessionLocal() as db:
        transcript = db.query(Transcript).filter_by(project_id=identifier).one()
        transcript.segments = []
        db.commit()
    def run(command, **kw):
        if '-show_entries' in command:
            if str(command[-1]).endswith('demo_preview_tele.mp4'):
                return SimpleNamespace(stdout='{"format":{"duration":"120"},"streams":[{"codec_type":"video","width":1280,"height":720}]}')
            return SimpleNamespace(stdout='{"format":{"duration":"1000"}}')
        Path(command[-1]).write_bytes(b'demo')
        return SimpleNamespace()
    monkeypatch.setattr(subprocess, 'run', run)
    demo = media.create_telegram_demo_clip(identifier)
    try:
        assert demo['start_seconds'] == 250
    finally:
        demo['path'].unlink()
    with connection.SessionLocal() as db:
        transcript = db.query(Transcript).filter_by(project_id=identifier).one()
        transcript.segments = [dict(start=700+i/10) for i in range(20)]
        db.commit()
    demo = media.create_telegram_demo_clip(identifier)
    try:
        assert demo['start_seconds'] == 700
    finally:
        demo['path'].unlink()


def test_status_never_scans_all_project_media_on_critical_path(bot, monkeypatch):
    make_project()
    monkeypatch.setattr(media, 'disk_usage', lambda *a: pytest.fail('Do not scan media during /status'))
    before = time.monotonic()
    assert assistant.intent('trạng thái ổ D') == 'status'
    assert time.monotonic()-before < .1
    bot[0].process_update(update(1, text='/status'))
    assert any('Ổ dữ liệu' in c['data'].get('text', '') for c in bot[1].calls)


def test_verified_auto_archives_light_pack_for_matching_drive(bot, monkeypatch):
    identifier, root = make_project()
    from src.cloud import metadata_archive, youtube_upload
    archived = []
    notifications = []
    monkeypatch.setattr(telegram, 'enabled', lambda: True)
    monkeypatch.setattr(assistant, 'cloud_status', lambda: {'drive': {'identity_confirmed': True}})
    monkeypatch.setattr(metadata_archive, 'receipt_status', lambda _: {'matches_current': False})
    monkeypatch.setattr(metadata_archive, 'archive_project_metadata_pack', lambda p: archived.append(p))
    monkeypatch.setattr(youtube_upload, 'publishing_pack', lambda _: {'youtube_titles': [], 'facebook_post': ''})
    monkeypatch.setattr(telegram, 'notify_project_event', lambda *a, **kw: notifications.append(a))
    class Pipeline:
        def get_project_dir(self, _):
            return root
        @telegram.notify_run_all
        def run_all(self, project_id):
            return {'verification': {'passed': True}}
    Pipeline().run_all(identifier)
    assert archived == [identifier]
    assert notifications[0][1] == 'VERIFIED'
    assert 'không gồm MP4' in notifications[0][2]


def test_config_validation_never_echoes_submitted_secret(bot):
    secret = 'do-not-echo-this-secret'
    with TestClient(api.app) as client:
        response = client.post('/api/notifications/telegram/config', json=[{'TELEGRAM_BOT_TOKEN': secret}])
        assert response.status_code == 400 and secret not in response.text


def test_web_telegram_panel_updates_without_html_injection(tmp_path):
    source = Path('web/app.js').read_text(encoding='utf-8').split('// Telegram control plane shares')[1]
    source = source[source.index('(() =>'):]
    fixture = r'''
const vm=require('vm');
class Element {
 constructor(){this.events={};this.children=[];this.style={display:'flex'};this.value='';this.checked=false;this.textContent='';}
 addEventListener(name,fn){this.events[name]=fn;}
 append(node){this.children.push(node);}
 replaceChildren(...nodes){this.children=nodes;}
 set innerHTML(value){throw new Error('Unsafe HTML');}
}
const nodes={};const get=id=>nodes[id]||(nodes[id]=new Element());
const requests=[];
const result={enabled:true,configured:true,polling_enabled:true,listening:true,isolated:false,
 disk_guard_enabled:true,disk_guard_min_free_gb:8,snapshot:{free_bytes:20*1024**3,tasks:[],cloud:{
 youtube:{connected:true,identity_confirmed:false},drive:{connected:false}},projects:[{
 id:'proj_x',title:'<img src=x onerror=alert(1)>',status:'VERIFIED',mp4_bytes:8,
 heavy_media_bytes:null,youtube_upload:false,drive_archive:true,drive_archive_mode:'metadata_only'}]}};
const sandbox={document:{getElementById:get,createElement:()=>new Element()},
 API:{request:async(method,url,body)=>{requests.push({method,url,body});return result;}},
 setInterval:()=>1,clearInterval:()=>{},window:{open:()=>null}};
vm.runInNewContext(SOURCE,sandbox);
(async()=>{
 get('cloud-modal').style.display='none';
 get('btn-open-cloud').events.click();
 get('cloud-modal').style.display='flex'; // cloud.js opens it in a later listener
 await new Promise(resolve=>setImmediate(resolve));
 if(!get('telegram-assistant-status').textContent.includes('Đang lắng nghe'))throw new Error('Missing status');
 const table=get('telegram-assistant-library').children[0];
 if(!table.children[1].children[0].children[0].textContent.includes('<img'))throw new Error('Missing safe literal title');
 get('telegram-assistant-token').value='secret-to-store';
 get('telegram-assistant-chat').value='777';
 await get('telegram-assistant-save').events.click();
 if(get('telegram-assistant-token').value || get('telegram-assistant-chat').value)throw new Error('Secrets retained in inputs');
 if(!requests.some(r=>r.url.endsWith('/config')&&r.body.TELEGRAM_BOT_TOKEN==='secret-to-store'))throw new Error('Config not sent');
 console.log('DOM OK');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    script = tmp_path/'telegram-dom.cjs'
    script.write_text('const SOURCE='+json.dumps(source)+';\n'+fixture, encoding='utf-8')
    result = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout+result.stderr
    assert 'DOM OK' in result.stdout


@pytest.mark.parametrize('text', ['đừng đẩy youtube', 'không cần demo', 'có nên đẩy youtube?', 'cách giải phóng ổ d'])
def test_questions_and_negated_actions_never_execute_as_commands(text):
    assert assistant.intent(text) == 'ai'


def test_vietnamese_capacity_and_use_are_not_stop_commands():
    assert assistant.intent('dung lượng') == 'status'
    assert assistant.intent('dùng giọng nào hay?') == 'ai'
    assert assistant.intent('dừng') == 'stop'
    assert assistant.intent('dừng proj_first') == 'stop'


def test_disk_guard_does_not_hold_global_queue_lock_while_checking_backup(bot, local_queue, monkeypatch):
    identifier, root = make_project()
    with (root/'output/video_vi.mp4').open('wb') as stream:
        stream.truncate(101*1024**2)
    monkeypatch.setattr(media, 'storage_free_bytes', lambda: 1)
    entered, release, responsive = threading.Event(), threading.Event(), threading.Event()
    def backup(_):
        entered.set(); release.wait(4)
        return {'media_backup': False}
    monkeypatch.setattr(media, 'backup_status', backup)
    guard = threading.Thread(target=media.auto_disk_guard_if_needed)
    guard.start()
    try:
        assert entered.wait(2)
        probe = threading.Thread(target=lambda: (local_queue.get_by_project(identifier), responsive.set()))
        probe.start()
        assert responsive.wait(1), 'Global queue lock held during large media IO'
        probe.join(timeout=2)
    finally:
        release.set(); guard.join(timeout=5)


def test_transport_close_error_cannot_expose_credentials(bot, monkeypatch):
    from src.cloud import drive
    monkeypatch.setattr(drive, 'network_allowed', lambda: True)
    class BrokenResponse:
        status_code = 500
        def close(self):
            raise RuntimeError(settings.TELEGRAM_BOT_TOKEN)
    monkeypatch.setattr(telegram.requests, 'post', lambda *a, **kw: BrokenResponse())
    with pytest.raises(RuntimeError) as exc:
        telegram._request('sendMessage', {'text': 'test'})
    assert settings.TELEGRAM_BOT_TOKEN not in str(exc.value)


@pytest.mark.parametrize('command', ['demo', 'đẩy youtube'])
def test_latest_completed_project_is_not_limited_to_recent_five(bot, monkeypatch, command):
    completed, _ = make_project('proj_completed', created=dt.datetime(2026, 1, 1))
    for i in range(6):
        make_project(f'proj_waiting{i}', status='PENDING', created=dt.datetime(2026, 1, i+2))
    assert completed not in [p['id'] for p in assistant.system_snapshot()['projects']]
    selected = []
    monkeypatch.setattr(assistant, 'handle_callback', lambda data, *a: selected.append(data))
    assistant.handle_message(command, bot[0])
    assert selected == [('demo:' if command == 'demo' else 'yt:')+completed]


def test_stop_keyboard_binds_actual_cloud_task_without_auto_profile(bot, local_queue):
    identifier, _ = make_project()
    event = threading.Event()
    task = local_queue.submit(identifier, 'CLOUD_UPLOAD', lambda: event.wait(5), update_pipeline_job=False)
    assert auto.read(identifier) is None
    reply = bot[0].message('Hủy cloud', {'inline_keyboard': [[
        {'text': 'Hủy', 'callback_data': 'stop:'+identifier}]]})
    try:
        bot[0].process_update(update(1, data='stop:'+identifier, message_id=reply['message_id']))
        assert task.cancel_requested
    finally:
        event.set(); task.future.result(timeout=5)
    second_event = threading.Event()
    next_task = local_queue.submit(identifier, 'CLOUD_UPLOAD', lambda: second_event.wait(5), update_pipeline_job=False)
    try:
        bot[0].process_update(update(2, data='stop:'+identifier, message_id=reply['message_id']))
        assert not next_task.cancel_requested
    finally:
        second_event.set(); next_task.future.result(timeout=5)


def test_explicit_cancel_names_only_the_requested_project(bot, local_queue):
    first, _ = make_project('proj_first')
    second, _ = make_project('proj_second')
    event = threading.Event()
    task1 = local_queue.submit(first, 'hold', lambda: event.wait(5), update_pipeline_job=False)
    task2 = local_queue.submit(second, 'hold', lambda: event.wait(5), update_pipeline_job=False)
    try:
        bot[0].process_update(update(1, text='hủy proj_second'))
        assert task2.cancel_requested and not task1.cancel_requested
    finally:
        event.set()
        task1.future.result(timeout=5)
