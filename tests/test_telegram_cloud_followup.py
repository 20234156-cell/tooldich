"""Focused regressions from the live Telegram/Google integration audit."""
import io
import json
import logging
import threading
import time
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest
import requests

from config.settings import settings
from src.cloud import drive, youtube_upload, metadata_archive
from src.database import connection
from src.database.models import CloudConnection
from src.notifications import assistant, telegram
from src.notifications.telegram_bot import FileLease
from src.pipeline import media_storage
from src.pipeline.render import get_ffmpeg_bin, get_ffprobe_bin
from test_telegram_ai_assistant import bot, local_queue, make_project, update, wait_until
from test_cloud_youtube_and_telegram import MetadataDrive


def test_file_upload_streams_bounded_reads_and_closes_response(bot, monkeypatch):
    monkeypatch.setattr(drive, 'network_allowed', lambda: True)
    calls = []
    responses = []

    class BoundedFile(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 65536, 'Multipart upload must not buffer the whole file'
            return super().read(size)

    def handler(request):
        assert request.method == 'POST'
        assert request.headers['content-type'].startswith('multipart/form-data; boundary=')
        body = request.read()
        assert b'name="video"; filename="demo.mp4"' in body
        assert b'Content-Type: video/mp4' in body
        response = httpx.Response(200, json={'ok': True, 'result': {'message_id': 7}})
        responses.append(response)
        return response

    def send(url, **kwargs):
        calls.append(kwargs)
        assert kwargs['follow_redirects'] is False
        timeout = kwargs['timeout']
        assert timeout.connect == 10
        assert timeout.write == timeout.read == 180
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr(telegram.httpx, 'post', send)
    monkeypatch.setattr(telegram.requests, 'post', lambda *a, **k: pytest.fail('Non-streaming multipart'))
    with BoundedFile(b'x'*200000) as stream:
        result = telegram._request('sendVideo', {'chat_id': settings.TELEGRAM_CHAT_ID},
            files={'video': ('demo.mp4', stream, 'video/mp4')}, timeout=180)
    assert result['message_id'] == 7
    assert len(calls) == 1 and responses[0].is_closed


@pytest.mark.parametrize('failure,expected', [
    ('timeout', 'TELEGRAM_TIMEOUT'), ('connection', 'TELEGRAM_CONNECTION_ERROR'),
    ('400', 'TELEGRAM_HTTP_4XX'), ('429', 'TELEGRAM_RATE_LIMITED'),
    ('503', 'TELEGRAM_HTTP_5XX'), ('invalid_json', 'TELEGRAM_INVALID_RESPONSE'),
    ('invalid_payload', 'TELEGRAM_INVALID_RESPONSE'), ('rejected', 'TELEGRAM_REJECTED'),
])
def test_transport_diagnostics_survive_without_credentials_or_retries(bot, monkeypatch, failure, expected):
    monkeypatch.setattr(drive, 'network_allowed', lambda: True)
    secret = settings.TELEGRAM_BOT_TOKEN + ' ' + settings.TELEGRAM_CHAT_ID
    calls = []
    closed = []

    class Response:
        status_code = int(failure) if failure.isdigit() else 200

        def json(self):
            if failure == 'invalid_json':
                raise ValueError(secret)
            if failure == 'invalid_payload':
                return [secret]
            return {'ok': False, 'description': secret, 'parameters': {'retry_after': 9000}}

        def close(self):
            closed.append(True)

    def send(*args, **kwargs):
        calls.append(True)
        if failure == 'timeout':
            raise requests.ReadTimeout(secret)
        if failure == 'connection':
            raise requests.ConnectionError(secret)
        return Response()

    monkeypatch.setattr(telegram.requests, 'post', send)
    with pytest.raises(telegram.TelegramRequestError) as raised:
        telegram._request('sendMessage', {'text': 'test'})
    error = raised.value
    assert error.code == expected and error.details['method'] == 'sendMessage'
    assert len(calls) == 1
    assert bool(closed) == (failure not in {'timeout', 'connection'})
    visible = str(error) + json.dumps(error.details)
    assert settings.TELEGRAM_BOT_TOKEN not in visible
    assert settings.TELEGRAM_CHAT_ID not in visible
    if failure == '429':
        assert error.details['retry_after'] == 3600


def test_demo_failure_keeps_safe_queue_diagnostics_and_removes_temporary(bot, local_queue, monkeypatch):
    identifier, root = make_project()
    temporary = root/'output/demo_preview_tele.mp4'
    temporary.write_bytes(b'preview')
    monkeypatch.setattr('src.pipeline.media_storage.create_telegram_demo_clip', lambda _: {
        'path': temporary, 'start_seconds': 0, 'duration_seconds': 120})
    original = bot[0].call

    def send(method, *args, **kwargs):
        if method == 'sendVideo':
            raise telegram.TelegramRequestError('TELEGRAM_TIMEOUT', method=method, transport_error='timeout')
        return original(method, *args, **kwargs)

    monkeypatch.setattr(bot[0], 'call', send)
    task = assistant._queued(identifier, 'TELEGRAM_DEMO', lambda check: assistant.send_demo(identifier, bot[0], check), bot[0])
    task.future.result(timeout=5)
    assert task.state == 'FAILED' and task.error_code == 'TELEGRAM_TIMEOUT'
    assert task.error_details == {'method': 'sendVideo', 'transport_error': 'timeout'}
    assert not temporary.exists()
    assert (root/'output/video_vi.mp4').read_bytes() == b'original-video'


def test_callback_ack_failure_does_not_discard_authorized_action(bot, monkeypatch):
    identifier, _ = make_project()
    original = bot[0].call
    handled = []

    def call(method, *args, **kwargs):
        if method == 'answerCallbackQuery':
            raise telegram.TelegramRequestError('TELEGRAM_TIMEOUT', method=method)
        return original(method, *args, **kwargs)

    monkeypatch.setattr(bot[0], 'call', call)
    monkeypatch.setattr(assistant, 'handle_callback', lambda data, service, message: handled.append(data))
    bot[0].process_update(update(1, data='demo:'+identifier, message_id=11))
    assert handled == ['demo:'+identifier]


def test_restart_reaps_expired_worker_lease_but_never_overlaps_workers(bot):
    service, _ = bot
    path = assistant.private_path('telegram_poll.lock')
    lease = FileLease(path)
    alive = [True]

    class PreviousWorker:
        def is_alive(self):
            return alive[0]

    service._lease = lease
    service._command_thread = PreviousWorker()
    service._stop_event.set()
    assert not service.start()
    assert service._lease is lease
    alive[0] = False
    assert service.start()
    assert lease.stream is None and service._lease is not lease
    service.stop(notify=False)
    assert service._lease is None and not service.status()['listening']


@pytest.mark.parametrize('text', ['hủy tác vụ là gì?', 'dừng là gì', 'hủy video như thế nào',
    'đừng hủy tác vụ', 'demo là gì', 'đẩy youtube là gì', 'dọn ổ D là gì'])
def test_questions_about_actions_do_not_execute_them(text):
    assert assistant.intent(text) == 'ai'


@pytest.mark.parametrize('text,action', [('hủy', 'stop'), ('dừng', 'stop'),
    ('hủy proj_example', 'stop'), ('demo proj_example', 'demo'),
    ('đẩy youtube proj_example', 'yt'), ('dọn ổ D', 'clean')])
def test_explicit_commands_still_execute(text, action):
    assert assistant.intent(text) == action


def test_malformed_url_does_not_hide_later_valid_link():
    assert assistant.extract_video_url('https://b23.tv:broken/no https://b23.tv/valid') == 'https://b23.tv/valid'
    assert assistant.extract_video_url('https://b23.tv:broken/no') is None


def test_new_desktop_json_invalidates_only_mismatched_youtube_auth(monkeypatch):
    original = {'client_id': 'old.apps.googleusercontent.com', 'client_secret': 'old-secret',
                'refresh_token': uuid.uuid4().hex, 'access_token': uuid.uuid4().hex, 'connection_id': 'previous'}
    youtube_upload.save_config(original)
    drive.configure({'installed': {'client_id': 'new.apps.googleusercontent.com', 'client_secret': 'new-secret'}})
    with connection.SessionLocal() as db:
        encrypted = db.get(CloudConnection, 'youtube').encrypted_data
    cfg = youtube_upload.config()
    assert cfg == {'client_id': 'new.apps.googleusercontent.com', 'client_secret': 'new-secret'}
    with connection.SessionLocal() as db:
        assert db.get(CloudConnection, 'youtube').encrypted_data == encrypted, 'Status read must not mutate credentials'
    monkeypatch.setattr(assistant, 'read_config', lambda: {'youtube_binding': {'account': assistant.ACCOUNT, 'connection_id': 'previous'}})
    assert not assistant.cloud_status()['youtube']['identity_confirmed']


def test_same_client_secret_rotation_preserves_youtube_authorization():
    identifier = 'same.apps.googleusercontent.com'
    cfg = {'client_id': identifier, 'client_secret': 'old-secret', 'refresh_token': uuid.uuid4().hex, 'connection_id': 'current'}
    youtube_upload.save_config(cfg)
    drive.configure({'installed': {'client_id': identifier, 'client_secret': 'new-secret'}})
    value = youtube_upload.config()
    assert value['refresh_token'] == cfg['refresh_token'] and value['connection_id'] == 'current'
    assert value['client_secret'] == 'new-secret'


def test_old_youtube_callback_cannot_restore_previous_client(monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    from src.cloud import youtube_oauth as auth
    drive.configure({'installed': {'client_id': 'old.apps.googleusercontent.com', 'client_secret': 'old-secret'}})
    workers = []
    parameters = {}

    class Server:
        server_port = 19003

        def __init__(self, address, handler):
            self.handler = handler

        def handle_request(self):
            callback = object.__new__(self.handler)
            callback.path = '/callback?state='+parameters['state'][0]+'&code=test-code'
            callback.wfile = io.BytesIO()
            callback.send_response = lambda *a: None
            callback.send_header = lambda *a: None
            callback.end_headers = lambda: None
            callback.do_GET()

        def server_close(self):
            pass

    class Worker:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            workers.append(self.target)

    monkeypatch.setattr(auth, 'HTTPServer', Server)
    monkeypatch.setattr(auth.threading, 'Thread', Worker)
    monkeypatch.setattr(auth, '_pending', False)
    monkeypatch.setattr(auth, '_attempt', None)
    monkeypatch.setattr(auth, 'checked', lambda *a, **k: {
        'access_token': 'test-access', 'refresh_token': 'test-refresh', 'scope': youtube_upload.SCOPE})
    response = auth.start()
    parameters.update(parse_qs(urlsplit(response['authorization_url']).query))
    drive.configure({'installed': {'client_id': 'new.apps.googleusercontent.com', 'client_secret': 'new-secret'}})
    workers[0]()
    assert youtube_upload.config()['client_id'] == 'new.apps.googleusercontent.com'
    assert not auth.status()['connected']
    with connection.SessionLocal() as db:
        assert db.get(CloudConnection, 'youtube') is None


def test_demo_odd_width_source_delivers_measured_player_metadata(bot):
    identifier, root = make_project(duration=3)
    target = root/'output/video_vi.mp4'
    subprocess.run([str(get_ffmpeg_bin()), '-nostdin', '-v', 'error', '-y', '-f', 'lavfi',
        '-i', 'testsrc=size=161x91:rate=5:duration=3', '-f', 'lavfi',
        '-i', 'sine=frequency=440:duration=3', '-c:v', 'libx264', '-pix_fmt', 'yuv444p',
        '-preset', 'ultrafast', '-c:a', 'aac', '-shortest', str(target)], check=True, timeout=30)
    original = target.read_bytes()
    demo = media_storage.create_telegram_demo_clip(identifier)
    try:
        measured = json.loads(subprocess.check_output([str(get_ffprobe_bin()), '-v', 'error',
            '-select_streams', 'v:0', '-show_entries', 'stream=width,height,pix_fmt',
            '-of', 'json', str(demo['path'])], timeout=15))['streams'][0]
        assert measured == {'width': 160, 'height': 90, 'pix_fmt': 'yuv420p'}
        assert demo['width'] == 160 and demo['height'] == 90
    finally:
        demo['path'].unlink(missing_ok=True)
    assistant.send_demo(identifier, bot[0])
    sent = next(call for call in bot[1].calls if call['method'] == 'sendVideo')
    assert sent['data']['width'] == 160 and sent['data']['height'] == 90
    assert sent['data']['duration'] == 3 and sent['timeout'] == 180
    assert not (root/'output/demo_preview_tele.mp4').exists()
    assert target.read_bytes() == original


@pytest.mark.parametrize('failure,code', [
    (httpx.WriteTimeout, 'TELEGRAM_TIMEOUT'), (httpx.ReadTimeout, 'TELEGRAM_TIMEOUT'),
    (httpx.ConnectError, 'TELEGRAM_CONNECTION_ERROR'),
])
def test_file_transport_failure_is_sanitized_once(bot, monkeypatch, failure, code):
    monkeypatch.setattr(drive, 'network_allowed', lambda: True)
    calls = []

    def send(*args, **kwargs):
        calls.append(True)
        raise failure(settings.TELEGRAM_BOT_TOKEN+' '+settings.TELEGRAM_CHAT_ID)

    monkeypatch.setattr(telegram.httpx, 'post', send)
    with pytest.raises(telegram.TelegramRequestError) as raised:
        telegram._request('sendVideo', {}, files={'video': ('demo.mp4', io.BytesIO(b'test'))}, timeout=180)
    assert raised.value.code == code and len(calls) == 1
    assert settings.TELEGRAM_BOT_TOKEN not in str(raised.value)
    assert settings.TELEGRAM_CHAT_ID not in str(raised.value)


def test_isolated_file_transport_cannot_reach_network(bot, monkeypatch):
    monkeypatch.setattr(drive, 'network_allowed', lambda: False)
    monkeypatch.setattr(telegram.httpx, 'post', lambda *a, **k: pytest.fail('Offline upload attempted'))
    with pytest.raises(RuntimeError, match='cô lập'):
        telegram._request('sendVideo', {}, files={'video': ('demo.mp4', io.BytesIO(b'test'))})


def test_httpx_request_log_cannot_expose_bot_token(bot, caplog):
    with caplog.at_level(logging.INFO, logger='httpx'):
        logging.getLogger('httpx').info('HTTP Request: POST %s',
            'https://api.telegram.org/bot'+settings.TELEGRAM_BOT_TOKEN+'/sendVideo')
    assert settings.TELEGRAM_BOT_TOKEN not in caplog.text
    assert '/bot[hidden]/sendVideo' in caplog.text


def test_reconfigure_resumes_after_slow_old_worker_exits(bot):
    service, _ = bot
    alive = [True]
    lease = FileLease(assistant.private_path('telegram_poll.lock'))

    class OldWorker:
        def is_alive(self):
            return alive[0]

    service._lease = lease
    service._command_thread = OldWorker()
    service._stop_event.set()
    assert not service.start()
    alive[0] = False
    wait_until(lambda: service.status()['listening'])
    assert lease.stream is None
    service.stop(notify=False)


def test_explicit_stop_revokes_deferred_restart(bot):
    service, _ = bot
    alive = [True]

    class OldWorker:
        def is_alive(self):
            return alive[0]

        def join(self, timeout=None):
            pass

    service._command_thread = OldWorker()
    service._stop_event.set()
    assert not service.start()
    service.stop(notify=False)
    alive[0] = False
    worker = service._restart_thread
    if worker:
        worker.join(timeout=2)
    assert not service.status()['listening'] and service.stopping


def test_unchanged_drive_light_pack_rechecks_without_duplicate_upload(bot, monkeypatch):
    identifier, root = make_project()
    (root/'output/publishing_pack.json').write_text('{}', encoding='utf-8')
    cfg = {'account_id': 'test-drive-account', 'refresh_token': 'test-refresh'}
    monkeypatch.setattr(drive, 'config', lambda: cfg)
    remote = MetadataDrive()
    monkeypatch.setattr(drive, 'Drive', lambda: remote)
    first = metadata_archive.archive_project_metadata_pack(identifier)
    identifiers = [row['remote_id'] for row in first['files']]
    second = metadata_archive.archive_project_metadata_pack(identifier)
    assert [row['remote_id'] for row in second['files']] == identifiers
    assert len(remote.data) == len(identifiers)
    remote.data[identifiers[0]] = b'changed remotely'
    with pytest.raises(drive.CloudError, match='checksum'):
        metadata_archive.archive_project_metadata_pack(identifier)
    assert (root/'output/video_vi.mp4').exists()


@pytest.mark.parametrize('media_present', [True, False])
def test_light_archive_preserves_existing_full_mp4_receipt(bot, monkeypatch, media_present):
    import hashlib
    from src.cloud import archive
    identifier, root = make_project()
    target = root/'output/video_vi.mp4'
    (root/'output/publishing_pack.json').write_text('{}', encoding='utf-8')
    cfg = {'account_id': 'test-drive-account', 'refresh_token': 'test-refresh'}
    monkeypatch.setattr(drive, 'config', lambda: cfg)
    monkeypatch.setattr(archive, 'delivery', lambda _: target)
    full = {'mode': 'full_delivery', 'status': 'VERIFIED', 'account_id': cfg['account_id'],
            'mp4_sha256': hashlib.sha256(target.read_bytes()).hexdigest()}
    path = root/'output/drive_archive.json'
    path.write_text(json.dumps(full), encoding='utf-8')
    if not media_present:
        target.unlink()
    remote = MetadataDrive()
    monkeypatch.setattr(drive, 'Drive', lambda: remote)
    metadata_archive.archive_project_metadata_pack(identifier)
    assert json.loads(path.read_text(encoding='utf-8')) == full
    assert metadata_archive.receipt_status(identifier)['matches_current'] is media_present
    assert metadata_archive.light_receipt_status(identifier)['matches_current']


def test_default_demo_skips_verified_project_without_local_mp4(bot, monkeypatch):
    older, root = make_project('proj_older')
    newer, missing = make_project('proj_newer')
    (missing/'output/video_vi.mp4').unlink()
    calls = []
    monkeypatch.setattr(assistant, 'handle_callback', lambda data, *args: calls.append(data))
    assistant.handle_message('demo', bot[0])
    assert calls == ['demo:'+older]
    assistant.handle_message('demo '+newer, bot[0])
    assert calls[-1] == 'demo:'+newer
    (root/'output/video_vi.mp4').unlink()
    assert assistant.latest_verified_project(require_media=True) is None


def test_full_drive_receipt_history_survives_local_media_removal(bot, monkeypatch):
    from src.cloud import archive
    identifier, root = make_project()
    receipt = {'mode': 'full_delivery', 'status': 'VERIFIED', 'account_id': 'account', 'mp4_sha256': 'old-sha'}
    (root/'output/drive_archive.json').write_text(json.dumps(receipt), encoding='utf-8')

    def absent(_):
        raise drive.CloudError('No current MP4')

    monkeypatch.setattr(archive, 'delivery', absent)
    status = metadata_archive.receipt_status(identifier)
    assert status['receipt'] == receipt and not status['matches_current']
