import os
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from config.settings import Settings, settings


def test_offline_overrides_legacy_sharing_configuration():
    value = Settings(_env_file=None, OFFLINE_MODE=True, SERVER_MODE=True,
                     SERVER_AUTO_BACKUP_HOURS=24, SERVER_PUBLIC_URL='https://example.com/')
    assert not value.SERVER_MODE
    assert value.SERVER_AUTO_BACKUP_HOURS == 0
    assert not value.SERVER_PUBLIC_URL
    assert value.EDGE_TTS_VOICE.startswith('vieneu:')
    assert value.LOCAL_TRANSLATION_URL.startswith('http://127.0.0.1:')


def test_offline_blocks_remote_sources_before_creating_records(monkeypatch, db_session):
    from src.jobs.system import manager
    from src.database.models import Project
    monkeypatch.setattr(settings, 'OFFLINE_MODE', True)
    for source in ('https://www.bilibili.com/video/BVtest', r'\\server\share\video.mp4', '//server/share/video.mp4'):
        with pytest.raises(Exception, match='offline'):
            manager.create_project(source)
    assert db_session.query(Project).count() == 0


def test_offline_disables_cloud_and_login_and_online_voices(monkeypatch):
    from src.api.app import app
    monkeypatch.setattr(settings, 'OFFLINE_MODE', True)
    monkeypatch.setattr(settings, 'API_TOKEN', None)
    client = TestClient(app)
    assert client.get('/api/server/session').json() == {'enabled': False, 'offline': True}
    assert client.get('/api/cloud/drive').status_code == 403
    assert client.post('/api/server/setup', json={}).status_code == 403
    assert client.get('/api/settings/keys').status_code == 403
    assert 'workspaceOffline=true' in client.get('/workspace-mode.js').text
    # Piper is a local ONNX engine too; offline must exclude online voices,
    # rather than accidentally require one particular local implementation.
    voices = client.get('/api/voices').json()['voices']
    assert all(v['id'].startswith(('vieneu:', 'piper:')) for v in voices)
    assert not any(v['id'].startswith('vi-VN-') for v in voices)


def test_offline_never_reads_cloud_credentials_for_qa(monkeypatch):
    from types import SimpleNamespace
    import src.api.app as api
    import src.cloud.drive as drive
    monkeypatch.setattr(settings, 'OFFLINE_MODE', True)
    monkeypatch.setattr(api, '_require_editable', lambda value: value)
    monkeypatch.setattr(api.orchestrator, 'verify', lambda value: SimpleNamespace(passed=True, to_dict=lambda: {'passed':True}))
    monkeypatch.setattr(drive, 'config', lambda: pytest.fail('Offline QA must not decrypt cloud credentials'))
    assert api.verify_project_endpoint('proj_local') == {'passed':True}


def test_personal_startup_does_not_delete_project_files(monkeypatch, tmp_path):
    from src.jobs.cleanup import cleanup_stale_files
    monkeypatch.setattr(settings, 'OFFLINE_MODE', True)
    project = tmp_path/'proj_not_in_db'
    project.mkdir()
    part = project/'source.part'
    part.write_bytes(b'preserve')
    cleanup_stale_files(tmp_path, max_part_age_seconds=0, min_orphan_age_seconds=0)
    assert part.read_bytes() == b'preserve'


def test_offline_local_video_does_not_require_server_directory_allowlist(monkeypatch, tmp_path):
    from src.security.validators import validate_local_file_path
    monkeypatch.setattr(settings, 'OFFLINE_MODE', True)
    monkeypatch.setattr(settings, 'LOCAL_FILE_ALLOWED_DIRS', '')
    video = tmp_path/'personal.mp4'
    video.write_bytes(b'media validation happens during import')
    assert validate_local_file_path(video) == video.resolve()
    with pytest.raises(Exception):
        validate_local_file_path(r'\\server\share\video.mp4')


def test_offline_socket_guard_in_isolated_process():
    code = '''
import socket
from src.security.offline import install_offline_guard
install_offline_guard()
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    with socket.socket() as client:
        client.connect(listener.getsockname())
    listener.accept()[0].close()
for action in (lambda: socket.getaddrinfo('example.com', 443),
               lambda: socket.socket().connect(('1.1.1.1', 443)),
               lambda: socket.socket().bind(('0.0.0.0', 0))):
    try:
        action()
    except PermissionError:
        continue
    raise AssertionError('Network policy was bypassed')
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).parents[1],
                            env=os.environ.copy(), capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def personal_launcher():
    import importlib.util
    spec = importlib.util.spec_from_file_location('personal_launcher', Path(__file__).parents[1]/'scripts/run_personal.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_personal_launcher_never_stops_unknown_port_owner(monkeypatch):
    module = personal_launcher()
    class OccupiedSocket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def bind(self, address): raise OSError('occupied')
    monkeypatch.setattr(module.sys, 'argv', ['launcher', '--no-browser'])
    monkeypatch.setattr(module.socket, 'socket', OccupiedSocket)
    monkeypatch.setattr(module, 'existing_app', lambda port: False)
    monkeypatch.setattr(module, 'timing_version', lambda url: None)
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **k: pytest.fail('Unknown listener must be preserved'))
    assert module.main() == 1


@pytest.mark.parametrize('version,expected', [
    ('source-grounded-recovery-v2', 1),
    ('cluster-budget-v3', 0),
])
def test_launcher_only_opens_existing_studio_with_current_timing(monkeypatch, version, expected):
    module = personal_launcher()

    class OccupiedSocket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def bind(self, address): raise OSError('occupied')

    opened = []
    monkeypatch.setattr(module.sys, 'argv', ['launcher'])
    monkeypatch.setattr(module.socket, 'socket', OccupiedSocket)
    monkeypatch.setattr(module, 'existing_app', lambda port: True)
    monkeypatch.setattr(module, 'timing_version', lambda url: version)
    monkeypatch.setattr(module.webbrowser, 'open', opened.append)
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **k: pytest.fail('Port is occupied'))
    assert module.main() == expected
    assert opened == (['http://127.0.0.1:9000/'] if expected == 0 else [])


def test_personal_launcher_reuses_only_verified_offline_app(monkeypatch):
    from types import SimpleNamespace
    import psutil
    module = personal_launcher()
    listener = SimpleNamespace(status=psutil.CONN_LISTEN, laddr=SimpleNamespace(port=8088), pid=123)
    monkeypatch.setattr(psutil, 'net_connections', lambda **kwargs: [listener])
    process = SimpleNamespace(cwd=lambda:str(module.BASE), cmdline=lambda:['python','main.py','serve'])
    monkeypatch.setattr(psutil, 'Process', lambda pid: process)
    monkeypatch.setattr(module, 'read_local', lambda url: b'{"enabled":false,"offline":true}')
    assert module.existing_app(8088)
    monkeypatch.setattr(module, 'read_local', lambda url: b'{"enabled":true}')
    assert not module.existing_app(8088)


def test_offline_default_timeline_provider_is_local(monkeypatch):
    from src.tts.timeline import AudioTimelineBuilder
    monkeypatch.setattr(settings, 'OFFLINE_MODE', True)
    monkeypatch.setattr(settings, 'EDGE_TTS_VOICE', 'vieneu:ngoc_huyen_review_phim')
    assert AudioTimelineBuilder().provider.name == 'vieneu'


def test_personal_mode_preserves_ai_configuration_and_disables_sharing():
    value = Settings(_env_file=None, PERSONAL_MODE=True, OFFLINE_MODE=False,
                     SERVER_MODE=True, GEMINI_API_KEY='unchanged-test-key', EDGE_TTS_VOICE='vi-VN-HoaiMyNeural')
    assert not value.SERVER_MODE
    assert value.SERVER_AUTO_BACKUP_HOURS == 0
    assert value.GEMINI_API_KEY == 'unchanged-test-key'
    assert value.EDGE_TTS_VOICE == 'vi-VN-HoaiMyNeural'


def test_personal_mode_keeps_ai_api_and_voices_accessible(monkeypatch):
    from src.api.app import app
    monkeypatch.setattr(settings, 'PERSONAL_MODE', True)
    monkeypatch.setattr(settings, 'OFFLINE_MODE', False)
    monkeypatch.setattr(settings, 'API_TOKEN', None)
    client = TestClient(app)
    assert client.get('/api/settings/keys').status_code == 200
    assert client.get('/api/translation/providers').json()['gemini']['available']
    assert any(v['id']=='vi-VN-NamMinhNeural' for v in client.get('/api/voices').json()['voices'])
    assert client.get('/api/cloud/drive').status_code == 200
    assert client.get('/api/server/session').json()['personal']
