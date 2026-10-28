import copy
import errno
import json
import os
from pathlib import Path
import threading
import time

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from config.settings import settings
from src.database.models import (Project,UploadSession,ProcessingAttempt,ArtifactPointer,Transcript,
                                 FileLifecycle,StageResetAudit,CleanupCandidate)
from src.server import cleanup,readers,remediation,store,backups


@pytest.fixture(autouse=True)
def mode(monkeypatch):
    monkeypatch.setattr(settings,'SERVER_MODE',True)
    monkeypatch.setattr(settings,'BASE_DIR',settings.storage_path.parent/'application')
    from src.jobs.queue import queue
    monkeypatch.setattr(queue,'_tasks',{})
    with readers.lock:readers.active.clear();cleanup.inflight.clear()
    yield
    with readers.lock:assert not readers.active


@pytest.fixture
def abandoned(db_session):
    from src.server.uploads import root
    identifier='a'*32;path=root(identifier);path.mkdir(parents=True);(path/'payload.part').write_bytes(b'keep exact bytes')
    db_session.add(UploadSession(upload_id=identifier,owner_username='owner',filename='fixture.mp4',total_size=16,chunk_size=8,status='ABORTED',created_at=time.time()-200000,updated_at=time.time()-200000,expires_at=time.time()-100000))
    db_session.commit();return identifier,path


def selected():
    result=cleanup.preview('admin')
    item=next(item for item in result['candidates'] if item['kind']=='upload')
    return result,item


def run(batch,item):return cleanup.execute(batch['batch_id'],[item['candidate_id']],'admin')['results'][0]


def test_upload_reactivated_after_preview_is_not_deleted(abandoned,db_session):
    identifier,path=abandoned;batch,item=selected()
    db_session.get(UploadSession,identifier).status='UPLOADING';db_session.commit()
    assert run(batch,item)['result']=='SKIPPED_STATE_CHANGED'
    assert (path/'payload.part').read_bytes()==b'keep exact bytes'


def test_attempt_reattached_after_preview_is_not_deleted(db_session):
    project=settings.storage_path/'p';path=project/'attempts/old';path.mkdir(parents=True);(path/'data.wav').write_bytes(b'old voice')
    db_session.add(Project(id='p'));db_session.add(ProcessingAttempt(id='old',project_id='p',job_id='j',kind='voiceover',number=1,fence=1,fingerprint='f',inputs={},options={},directory='attempts/old',state='UNCOMMITTED_ATTEMPT',created_at=time.time()-200000));db_session.commit()
    batch=cleanup.preview('admin');item=next(c for c in batch['candidates'] if c['kind']=='attempt')
    db_session.add(ArtifactPointer(project_id='p',kind='voiceover',attempt_id='old',directory='attempts/old/voiceover'));db_session.commit()
    assert run(batch,item)['result']=='SKIPPED_STATE_CHANGED' and path.exists()


def test_reader_blocks_delete_and_is_released(abandoned):
    identifier,path=abandoned;batch,item=selected()
    with readers.read_guard(path/'payload.part'):
        assert run(batch,item)['result']=='SKIPPED_ACTIVE_READER'
    batch,item=selected();assert run(batch,item)['result']=='DELETED'
    assert not path.exists()
    with pytest.raises(HTTPException) as error:
        with readers.read_guard(path/'payload.part'):pass
    assert error.value.status_code==404


def test_deleting_commit_blocks_new_reader_and_upload_reuse(abandoned,monkeypatch):
    identifier,path=abandoned;batch,item=selected();marked=threading.Event();release=threading.Event();errors=[]
    def barrier(_):marked.set();assert release.wait(10)
    monkeypatch.setattr(cleanup,'after_mark',barrier)
    def worker():
        try:run(batch,item)
        except Exception as error:errors.append(error)
    thread=threading.Thread(target=worker);thread.start();assert marked.wait(10)
    try:
        assert path.exists()
        with pytest.raises(HTTPException) as error:
            with readers.read_guard(path):pass
        assert error.value.status_code==409
        from src.server.uploads import load
        with store.transaction() as db:
            with pytest.raises(HTTPException) as error:load(db,identifier,'owner')
            assert error.value.status_code==409
        cleanup.recover();assert path.exists()  # A live delete must not be stolen by recovery.
    finally:release.set();thread.join(10)
    assert not errors and not path.exists()


def test_stat_change_after_preview_preserves_bytes(abandoned):
    _,path=abandoned;batch,item=selected();(path/'new.tmp').write_bytes(b'new work')
    assert run(batch,item)['result']=='SKIPPED_STATE_CHANGED' and path.exists()


def test_spoofed_candidate_selection_is_validated_before_any_delete(abandoned):
    _,path=abandoned;batch,item=selected()
    with pytest.raises(HTTPException):cleanup.execute(batch['batch_id'],[item['candidate_id'],'made-up'],'admin')
    with pytest.raises(HTTPException):cleanup.execute(batch['batch_id'],[item['candidate_id']],'another-admin')
    assert path.exists()


def test_crash_after_mark_recovery_and_permission_failure(abandoned,monkeypatch):
    _,path=abandoned;batch,item=selected()
    def crash(_):raise RuntimeError('process interrupted after durable mark')
    monkeypatch.setattr(cleanup,'after_mark',crash)
    with pytest.raises(RuntimeError):run(batch,item)
    with store.transaction() as db:assert db.get(FileLifecycle,str(path.resolve())).state=='DELETING'
    def denied(_):raise PermissionError(errno.EACCES,'locked file')
    monkeypatch.setattr(cleanup,'unlink',denied);cleanup.recover()
    with store.transaction() as db:
        row=db.get(FileLifecycle,str(path.resolve()));assert row.state=='DELETE_FAILED' and row.error['errno']==errno.EACCES
    assert path.exists()


def test_crash_after_unlink_recovery_finishes_deleted(abandoned,monkeypatch):
    _,path=abandoned;batch,item=selected()
    def crash(_):
        cleanup.unlink(path);raise RuntimeError('crash before final db commit')
    monkeypatch.setattr(cleanup,'after_mark',crash)
    with pytest.raises(RuntimeError):run(batch,item)
    assert not path.exists();cleanup.recover()
    with store.transaction() as db:assert db.get(FileLifecycle,str(path.resolve())).state=='DELETED'


def test_committed_delivery_blocked_without_independent_package(db_session):
    root=settings.storage_path/'p';(root/'output').mkdir(parents=True);path=root/'output/video_vi.mp4';path.write_bytes(b'committed delivery')
    db_session.add(Project(id='p',status='CLOSED'));db_session.commit()
    batch=cleanup.preview('admin');item=next(c for c in batch['candidates'] if c['kind']=='deliverable')
    assert item['blocked_reason']=='NO_INDEPENDENT_VERIFIED_BACKUP'
    assert run(batch,item)['result']=='SKIPPED_STATE_CHANGED' and path.exists()


@pytest.fixture
def failed_voice(db_session):
    project='failed_voice';root=settings.storage_path/project;root.mkdir()
    (root/'pipeline_state.json').write_text(json.dumps(dict(current_state='TRANSLATION_REVIEWED',stages={'VOICE_READY':dict(status='FAILED')})))
    db_session.add(Project(id=project,status='FAILED'))
    db_session.add(Transcript(project_id=project,segments=[dict(index=7,start=1.2,end=2.5,vi_text='Giữ nguyên chữ Việt',text='source',voice_audio_path='voiceover/clips/missing.wav',voice_duration=.8),dict(index=9,start=3,end=4,vi_text='',voice_audio_path=None)]))
    db_session.commit();return project


def test_explicit_failed_stage_reset_preserves_text_timing_and_audits(failed_voice):
    project=failed_voice
    with store.transaction() as db:before=copy.deepcopy(db.query(Transcript).filter_by(project_id=project).one().segments)
    planned=remediation.preview(project,'admin')
    with pytest.raises(HTTPException):remediation.execute(planned['preview_id'],'admin')
    result=remediation.execute(planned['preview_id'],'admin',True)
    assert result['count']==1
    with store.transaction() as db:
        after=db.query(Transcript).filter_by(project_id=project).one().segments
        audit=db.get(StageResetAudit,result['audit_id']);assert backups.checksum(Path(audit.path))==audit.sha256
    expected=copy.deepcopy(before);expected[0]['voice_audio_path']=None;expected[0]['voice_duration']=None
    assert after==expected
    data=json.loads(Path(result['audit_path']).read_text(encoding='utf-8'));assert data['before'][0]['segments']==before and data['after'][0]['segments']==after
    backup=backups.create();manifest=backups.verify(backups.backup_root()/backup['name'])
    assert any(item['references']==['stage_reset:'+result['audit_id']] for item in manifest['required'])
    with pytest.raises(HTTPException):remediation.execute(planned['preview_id'],'admin',True)


def test_reset_refuses_changed_text_published_voice_or_existing_backup(failed_voice,db_session):
    planned=remediation.preview(failed_voice,'admin')
    row=db_session.query(Transcript).filter_by(project_id=failed_voice).one();value=copy.deepcopy(row.segments);value[0]['vi_text']='edited';row.segments=value;db_session.commit()
    with pytest.raises(HTTPException):remediation.execute(planned['preview_id'],'admin',True)
    db_session.add(ArtifactPointer(project_id=failed_voice,kind='voiceover',attempt_id='old',directory=''));db_session.commit()
    with pytest.raises(HTTPException):remediation.preview(failed_voice,'admin')


def test_reader_middleware_retains_lease_through_range_body(db_session,monkeypatch):
    import asyncio
    from src.server.readers import ReadMiddleware
    root=settings.storage_path/'p';root.mkdir();db_session.add(Project(id='p'));db_session.commit()
    seen=[]
    async def app(scope,receive,send):
        assert readers.has_readers(root)
        await send({'type':'http.response.start','status':206,'headers':[]})
        await send({'type':'http.response.body','body':b'first','more_body':True})
        assert readers.has_readers(root)
        await send({'type':'http.response.body','body':b'last','more_body':False})
    async def send(message):seen.append(message)
    async def receive():return {'type':'http.disconnect'}
    asyncio.run(ReadMiddleware(app)({'type':'http','path':'/api/projects/p/preview'},receive,send))
    assert not readers.has_readers(root) and seen[0]['status']==206


def test_admin_api_rejects_paths_unknown_ids_and_non_admin(abandoned):
    from src.api.app import app
    client=TestClient(app)
    response=client.post('/api/server/setup',json={'username':'admin','password':'a-long-audit-password'})
    headers={'X-CSRF-Token':response.json()['csrf']}
    batch=client.post('/api/admin/cleanup/preview',headers=headers).json()
    item=next(c for c in batch['candidates'] if c['kind']=='upload')
    payload=dict(batch_id=batch['batch_id'],candidate_ids=[item['candidate_id']],file_path='../../source.mp4')
    assert client.post('/api/admin/cleanup/execute',json=payload,headers=headers).status_code==422
    payload.pop('file_path');payload['candidate_ids']=['fake']
    assert client.post('/api/admin/cleanup/execute',json=payload,headers=headers).status_code==404
    assert abandoned[1].exists()
    store.save_user('operator','another-long-password','operator')
    other=TestClient(app);session=other.post('/api/server/login',json={'username':'operator','password':'another-long-password'}).json()
    assert other.post('/api/admin/cleanup/preview',headers={'X-CSRF-Token':session['csrf']}).status_code==403


def test_server_legacy_sweeper_cannot_bypass_preview(abandoned):
    from src.jobs.cleanup import cleanup_stale_files
    root=settings.storage_path/'proj_orphan';root.mkdir();part=root/'left.part';part.write_bytes(b'keep')
    result=cleanup_stale_files(max_part_age_seconds=0,min_orphan_age_seconds=0)
    assert not result['deleted_parts'] and part.exists() and root.exists()


def test_disconnect_releases_reader_guard(db_session):
    import asyncio
    root=settings.storage_path/'p';root.mkdir();db_session.add(Project(id='p'));db_session.commit()
    async def app(scope,receive,send):
        assert readers.has_readers(root);raise asyncio.CancelledError()
    async def noop(*args):pass
    with pytest.raises(asyncio.CancelledError):asyncio.run(readers.ReadMiddleware(app)({'type':'http','path':'/api/projects/p/preview'},noop,noop))
    assert not readers.has_readers(root)


def test_post_mark_new_reference_prevents_physical_delete(abandoned,monkeypatch):
    from src.database.models import Media
    identifier,path=abandoned;batch,item=selected()
    def attach(_):
        with store.transaction() as db:
            db.add(Project(id='new_project'));db.flush();db.add(Media(project_id='new_project',file_path=str(path/'payload.part')))
    monkeypatch.setattr(cleanup,'after_mark',attach)
    assert run(batch,item)['result']=='DELETE_FAILED' and path.exists()


def test_expired_cleanup_batch_refuses_without_deleting(abandoned):
    from src.database.models import CleanupBatch
    _,path=abandoned;batch,item=selected()
    with store.transaction() as db:db.get(CleanupBatch,batch['batch_id']).expires_at=0
    with pytest.raises(HTTPException):run(batch,item)
    assert path.exists()


def test_backup_working_root_keeps_legacy_versions_and_restore_expires_batches(failed_voice,tmp_path,monkeypatch):
    remediation.execute(remediation.preview(failed_voice,'admin')['preview_id'],'admin',True)
    first=backups.create();legacy=backups.find_backup(first['name'])
    cleanup.preview('admin')
    working=tmp_path/'other_volume/snapshots'
    monkeypatch.setattr(settings,'SERVER_BACKUP_ROOT',str(working))
    second=backups.create()
    assert backups.find_backup(first['name'])==legacy and legacy.exists()
    root=backups.find_backup(second['name']);assert root.parent==working
    restored=tmp_path/'recovered';backups.restore(root,restored)
    import sqlite3
    with sqlite3.connect(restored/'app.db') as db:assert db.execute('SELECT MAX(expires_at) FROM cleanup_batches').fetchone()[0]==0


def test_v2_backup_without_new_phase4_tables_remains_verifiable(failed_voice):
    remediation.execute(remediation.preview(failed_voice,'admin')['preview_id'],'admin',True)
    result=backups.create();root=backups.find_backup(result['name']);manifest=backups.verify(root)
    # Older v2 manifests explicitly cover fewer operational tables. Their SQL
    # integrity/required files still get verified; no fabricated coverage claim.
    for table in ('file_lifecycles','cleanup_batches','cleanup_candidates'):manifest['tables'].pop(table)
    backups.write_json(root/'manifest.json',manifest)
    assert backups.verify(root)['status']=='VALIDATED_PASS'


def test_attempt_directory_cannot_escape_its_owned_attempt_root(db_session):
    path=settings.storage_path.parent/'private';path.mkdir();(path/'keep.txt').write_text('protected')
    db_session.add(Project(id='p'))
    db_session.add(ProcessingAttempt(id='bad_location',project_id='p',job_id='j',kind='voiceover',number=1,fence=1,fingerprint='f',inputs={},options={},directory='../../private',state='UNCOMMITTED_ATTEMPT',created_at=time.time()-200000));db_session.commit()
    batch=cleanup.preview('admin');item=next(c for c in batch['candidates'] if c['owner_id']=='bad_location')
    assert item['blocked_reason']=='OUTSIDE_ATTEMPT_ROOT'
    assert run(batch,item)['result']=='SKIPPED_STATE_CHANGED' and (path/'keep.txt').is_file()
