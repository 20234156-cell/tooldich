from pathlib import Path
import sqlite3

import pytest

from config.settings import settings
from src.database.models import Project,Media
from src.server import access
from src.server.backups import create,verify,restore


def test_consistent_backup_restore_remaps_paths_and_preserves_live_data(tmp_path,db_session,monkeypatch):
    import src.api.app as api
    monkeypatch.setattr(api.queue,'_tasks',{})
    app=tmp_path/'application';app.mkdir()
    monkeypatch.setattr(settings,'BASE_DIR',app)
    monkeypatch.setattr(settings,'SERVER_MODE',True)
    data=settings.storage_path/'backup_project';data.mkdir()
    source=data/'source.mp4';source.write_bytes(b'fixture video data')
    db_session.add(Project(id='backup_project'))
    db_session.flush()
    db_session.add(Media(project_id='backup_project',file_path=str(source.resolve()),file_size_bytes=source.stat().st_size))
    db_session.commit()
    from src.server import store
    store.put('invite:restore-test',{'username':'pending_reader','expires':9999999999,'used':False})
    result=create()
    folder=settings.storage_path.parent/'backups'/result['name']
    assert result['verified'] and verify(folder)['complete']
    destination=tmp_path/'restored_data'
    output=restore(folder,destination)
    assert output['verified']
    assert (destination/'projects/backup_project/source.mp4').read_bytes()==source.read_bytes()
    with sqlite3.connect(destination/'app.db') as db:
        path=db.execute('SELECT file_path FROM media WHERE project_id=?',('backup_project',)).fetchone()[0]
        assert db.execute("SELECT COUNT(*) FROM server_records WHERE key LIKE 'invite:%'").fetchone()[0]==0
    assert store.get('invite:restore-test') is not None
    assert Path(path)==destination/'projects/backup_project/source.mp4'
    assert source.read_bytes()==b'fixture video data'
    with pytest.raises(ValueError,match='chưa tồn tại'):restore(folder,destination)
    (folder/'projects/backup_project/source.mp4').write_bytes(b'corrupted')
    with pytest.raises(ValueError,match='thiếu/hỏng'):verify(folder)


def test_backup_rejects_active_writes_and_does_not_leave_maintenance_on(tmp_path,monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(access,'inflight_writes',1)
    with pytest.raises(HTTPException):create()
    assert access.maintenance is False


def test_backup_closes_every_sqlite_handle_before_cleanup(tmp_path,monkeypatch):
    import src.api.app as api
    import src.server.backups as backups
    monkeypatch.setattr(api.queue,'_tasks',{})
    original=sqlite3.connect
    handles=[]
    def connect(*args,**kwargs):
        connection=original(*args,**kwargs)
        handles.append(connection)
        return connection
    monkeypatch.setattr(backups.sqlite3,'connect',connect)
    result=create()
    assert result['verified']
    assert len(handles)>=2
    for connection in handles:
        with pytest.raises(sqlite3.ProgrammingError,match='closed'):
            connection.execute('SELECT 1')
    folder=settings.storage_path.parent/'backups'/result['name']
    # On Windows an unclosed backup or verification handle prevents this rename.
    (folder/'app.db').rename(folder/'database.closed')
    assert not access.maintenance


def test_automatic_backup_failure_backs_off(monkeypatch):
    import asyncio
    import src.server.backups as backups
    from src.server import store
    monkeypatch.setattr(settings,'SERVER_AUTO_BACKUP_HOURS',24)
    calls=[]
    sleeps=[]
    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps)==3:raise asyncio.CancelledError()
    def fail():
        calls.append(1)
        raise PermissionError('locked database')
    monkeypatch.setattr(backups.asyncio,'sleep',sleep)
    monkeypatch.setattr(backups,'create',fail)
    with pytest.raises(asyncio.CancelledError):asyncio.run(backups.automatic_loop())
    assert len(calls)==1
    error=store.get('backup:last_error')
    assert error['attempts']==1 and error['retry_after']-error['time']>=299


def test_failed_verification_removes_partial_backup_and_releases_gate(monkeypatch):
    import src.api.app as api
    import src.server.backups as backups
    monkeypatch.setattr(api.queue,'_tasks',{})
    def fail(folder):raise ValueError('forced integrity failure')
    monkeypatch.setattr(backups,'verify',fail)
    with pytest.raises(ValueError,match='forced integrity failure'):create()
    assert list(backups.backup_root().iterdir())==[]
    assert not access.maintenance


def test_backup_excludes_live_browser_profile_but_keeps_other_private_files(monkeypatch):
    import src.api.app as api
    monkeypatch.setattr(api.queue,'_tasks',{})
    private=settings.storage_path.parent/'private'
    profile=private/'douyin_browser_profile/Default/Service Worker/CacheStorage'
    profile.mkdir(parents=True)
    (profile/'locked-runtime-cache').write_bytes(b'transient')
    (private/'server_login.txt').write_text('private fixture',encoding='utf-8')
    result=create()
    folder=settings.storage_path.parent/'backups'/result['name']
    manifest=verify(folder)
    assert (folder/'private/server_login.txt').read_text()=='private fixture'
    assert not (folder/'private/douyin_browser_profile').exists()
    assert manifest['excluded']
