import hashlib
import json
from pathlib import Path
import sqlite3
import time

import pytest

from config.settings import settings
from src.database.models import Project,Media,Transcript,ProcessingAttempt,ArtifactPointer,FileBaseline
from src.server import backups,store,access
from src.server import backup_remote as remote


@pytest.fixture
def legacy(db_session,monkeypatch):
    from src.jobs.queue import queue
    monkeypatch.setattr(queue,'_tasks',{})
    # Never copy production .env/key material into test output.
    monkeypatch.setattr(settings,'BASE_DIR',settings.storage_path.parent/'application')
    folder=settings.storage_path/'legacy';folder.mkdir()
    path=folder/'source.mp4';path.write_bytes(b'original video fixture')
    db_session.add(Project(id='legacy'));db_session.flush()
    db_session.add(Media(project_id='legacy',file_path=str(path),is_valid=True))
    db_session.add(Transcript(project_id='legacy',segments=[{'index':1,'vi_text':'','voice_audio_path':None}]))
    db_session.commit()
    return path


def folder(result):return backups.backup_root()/result['name']


def test_missing_required_detected_from_snapshot_even_before_scan(legacy):
    legacy.unlink()
    with pytest.raises(backups.BackupSourceError) as error:backups.create()
    assert error.value.status=='FAILED_INCOMPLETE'
    manifest=json.loads((backups.backup_root()/error.value.name/'manifest.json').read_text())
    assert not manifest['complete'] and manifest['errors'][0]['reason']=='MISSING_REQUIRED'
    assert access.maintenance is False


def test_legacy_baseline_never_relearns_corruption(legacy):
    good=folder(backups.create())
    manifest=backups.verify(good)
    assert manifest['required'][0]['provenance']=='BASELINE_FROM_MIGRATION'
    assert manifest['intentionally_empty'][0]['category']=='INTENTIONALLY_EMPTY'
    legacy.write_bytes(b'X'+legacy.read_bytes()[1:])
    with pytest.raises(backups.BackupSourceError) as error:backups.create()
    assert error.value.status=='FAILED_CORRUPT_SOURCE'
    assert backups.verify(good)['status']=='VALIDATED_PASS'
    assert store.get('backup:last')['name']==good.name


def published(db_session,legacy):
    project=legacy.parent
    directory=project/'attempts/published/voiceover';directory.mkdir(parents=True)
    voice=directory/'voiceover.wav';voice.write_bytes(b'voice fixture')
    name=voice.relative_to(project).as_posix()
    db_session.add(ProcessingAttempt(id='published',project_id='legacy',job_id='j',kind='voiceover',number=1,fence=1,
        fingerprint='f',inputs={},options={},directory='attempts/published',state='PUBLISHED',created_at=time.time(),
        manifest={'files':{name:dict(sha256=backups.checksum(voice),size=voice.stat().st_size,verification='VERIFIED_AT_CREATION')}}))
    db_session.add(ArtifactPointer(project_id='legacy',kind='voiceover',attempt_id='published',directory='attempts/published/voiceover'))
    db_session.commit()
    return voice


@pytest.mark.parametrize('fault',['missing','corrupt'])
def test_published_artifact_uses_creation_baseline(db_session,legacy,fault):
    voice=published(db_session,legacy)
    if fault=='missing':voice.unlink()
    else:voice.write_bytes(b'Voice fixture')
    with pytest.raises(backups.BackupSourceError) as error:backups.create()
    assert error.value.status==('FAILED_INCOMPLETE' if fault=='missing' else 'FAILED_CORRUPT_SOURCE')
    assert db_session.query(FileBaseline).filter(FileBaseline.path.contains('voiceover')).count()==0


def test_required_inventory_cannot_be_removed_from_package_manifest(legacy):
    root=folder(backups.create());manifest=backups.verify(root)
    item=manifest['required'][0];(root/item['path']).unlink();manifest['files'].pop(item['path'])
    backups.write_json(root/'manifest.json',manifest)
    with pytest.raises(ValueError,match='thiếu/hỏng'):backups.verify(root)


def test_uncommitted_optional_not_included_but_current_pointer_is(db_session,legacy):
    voice=published(db_session,legacy)
    uncommitted=legacy.parent/'attempts/uncommitted/output';uncommitted.mkdir(parents=True)
    (uncommitted/'partial.mp4').write_bytes(b'partial')
    root=folder(backups.create());manifest=backups.verify(root)
    assert any(r['provenance']=='VERIFIED_AT_CREATION' for r in manifest['required'])
    assert not any('uncommitted' in name for name in manifest['files'])
    assert (root/voice.relative_to(settings.storage_path.parent)).is_file()


def test_independent_export_drill_and_cleanup_gate(legacy,tmp_path,monkeypatch):
    root=folder(backups.create());destination=tmp_path/'independent';destination.mkdir()
    monkeypatch.setattr(remote,'distinct_volume',lambda path:True)
    assert not remote.deletion_allowed(legacy)
    record=remote.export_directory(root,destination)
    assert record['status']=='REMOTE_VERIFIED' and not remote.deletion_allowed(legacy)
    restored=tmp_path/'restored'
    result=remote.drill(root.name,restored)
    assert result['status']=='RESTORE_DRILL_PASS' and remote.deletion_allowed(legacy)
    assert (restored/'projects/legacy/source.mp4').read_bytes()==legacy.read_bytes()
    with sqlite3.connect(restored/'app.db') as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert db.execute('SELECT file_path FROM media').fetchone()[0]==str(restored/'projects/legacy/source.mp4')
    # A remote version that goes missing/changes must revoke permission.
    Path(record['path']).write_bytes(b'corrupt remote zip')
    assert not remote.deletion_allowed(legacy)
    with pytest.raises(ValueError):remote.drill(root.name,tmp_path/'bad_restore')
    assert backups.verify(root)['status']=='VALIDATED_PASS'


def test_same_volume_and_failed_packages_cannot_be_exported(legacy,tmp_path,monkeypatch):
    root=folder(backups.create());destination=tmp_path/'other';destination.mkdir()
    monkeypatch.setattr(remote,'distinct_volume',lambda path:False)
    with pytest.raises(ValueError,match='vật lý'):remote.export_directory(root,destination)
    manifest=json.loads((root/'manifest.json').read_text());manifest.update(complete=False,status='FAILED_INCOMPLETE')
    backups.write_json(root/'manifest.json',manifest)
    with pytest.raises(ValueError):remote.package(root)


def test_change_during_copy_never_publishes_pass(legacy,monkeypatch):
    original=backups.shutil.copy2
    def mutate(source,dest):
        result=original(source,dest)
        if Path(source)==legacy:Path(dest).write_bytes(b'bad bytes after copy')
        return result
    monkeypatch.setattr(backups.shutil,'copy2',mutate)
    with pytest.raises(backups.BackupSourceError) as error:backups.create()
    assert error.value.status=='FAILED_CORRUPT_SOURCE'
    assert not access.maintenance


def test_external_source_is_packaged_and_relocated(legacy,db_session,tmp_path):
    outside=tmp_path.parent/(tmp_path.name+'_outside.mp4');outside.write_bytes(b'external source')
    row=db_session.query(Media).one();row.file_path=str(outside);db_session.commit()
    root=folder(backups.create());manifest=backups.verify(root)
    assert manifest['required'][0]['path'].startswith('external_required/')
    target=tmp_path/'relocated';backups.restore(root,target)
    with sqlite3.connect(target/'app.db') as db:
        assert Path(db.execute('SELECT file_path FROM media').fetchone()[0]).read_bytes()==outside.read_bytes()


def test_cloud_system_package_reuses_transport_without_overwriting_versions(legacy,monkeypatch,tmp_path):
    root=folder(backups.create())
    destination=tmp_path/'same_disk';destination.mkdir()
    monkeypatch.setattr(remote,'distinct_volume',lambda path:False)
    remote.export_directory(root,destination,allow_same_disk=True)
    class FakeDrive:
        def new_id(self):return 'remote_test'
        def exists(self,identifier):return hasattr(self,'size')
        def begin(self,row):self.size=row.file_size;self.md5=row.md5;self.bytes=bytearray();return 'fake-session'
        def send(self,session,total,offset=None,chunk=b''):
            if offset is None:return len(self.bytes)
            assert offset==len(self.bytes);self.bytes.extend(chunk);return len(self.bytes)
        def metadata(self,identifier):return dict(id=identifier,trashed=False,size=str(self.size),md5Checksum=self.md5)
    drive=FakeDrive()
    monkeypatch.setattr(remote,'Drive',lambda:drive)
    monkeypatch.setattr(remote,'config',lambda:dict(account_id='account',refresh_token='fake'))
    monkeypatch.setattr(remote,'seal',lambda value:value)
    monkeypatch.setattr(remote,'unseal',lambda value:value)
    first=remote.export_drive(root)
    second=remote.export_drive(root)
    assert first==second and first['status']=='REMOTE_VERIFIED'
    assert hashlib.sha256(drive.bytes).hexdigest()==store.get('backup:remote:'+root.name)['sha256']
    assert store.get('backup:replica:'+root.name)['status']=='LOCAL_REPLICA_VERIFIED'
    assert not remote.deletion_allowed(legacy)


def test_same_disk_replica_drill_never_opens_deletion_gate(legacy,tmp_path,monkeypatch):
    destination=tmp_path/'replica';destination.mkdir()
    monkeypatch.setattr(remote,'distinct_volume',lambda path:False)
    monkeypatch.setattr(settings,'SERVER_BACKUP_DIRECTORY',str(destination))
    monkeypatch.setattr(settings,'SERVER_BACKUP_ALLOW_SAME_DISK',True)
    result=backups.create();root=folder(result)
    assert result['replica_status']=='LOCAL_REPLICA_VERIFIED'
    restored=remote.drill(root.name,tmp_path/'local_drill')
    assert restored['status']=='LOCAL_RESTORE_DRILL_PASS' and not restored['deletion_allowed']
    assert not remote.deletion_allowed(legacy)


def test_replica_failure_preserves_validated_local_package(legacy,tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'SERVER_BACKUP_DIRECTORY',str(tmp_path/'does_not_exist'))
    result=backups.create()
    assert result['replica_error']
    assert backups.verify(folder(result))['status']=='VALIDATED_PASS'


def test_cue_audio_reference_required_and_empty_cue_is_not(db_session,legacy):
    tr=db_session.query(Transcript).one()
    tr.segments=[dict(index=1,vi_text='Có lời',voice_audio_path='voiceover/clips/missing.wav'),
                 dict(index=2,vi_text='',voice_audio_path='voiceover/clips/intentionally_empty.wav')]
    db_session.commit()
    with pytest.raises(backups.BackupSourceError) as error:backups.create()
    assert [item['path'] for item in error.value.errors]==['projects/legacy/voiceover/clips/missing.wav']


def test_legacy_stage_missing_delivery_required_and_mutable_metadata_refreshes(legacy):
    state=legacy.parent/'pipeline_state.json'
    state.write_text(json.dumps(dict(stages={'RENDERED':dict(status='COMPLETED',artifacts={'video':'output/final.mp4'})})))
    with pytest.raises(backups.BackupSourceError) as error:backups.create()
    assert any('final.mp4' in r['path'] for r in error.value.errors)
    output=legacy.parent/'output/final.mp4';output.parent.mkdir();output.write_bytes(b'final video')
    first=backups.create();assert first['verified']
    value=json.loads(state.read_text());value['updated_at']=time.time();state.write_text(json.dumps(value))
    second=backups.create();assert second['verified']
    assert any(r['provenance']=='SNAPSHOT_METADATA' for r in backups.verify(folder(second))['required'])


def test_current_attempt_includes_sibling_transcript_manifest(db_session,legacy):
    published(db_session,legacy)
    transcript=legacy.parent/'attempts/published/transcript/cues.json';transcript.parent.mkdir();transcript.write_text('[]')
    attempt=db_session.get(ProcessingAttempt,'published')
    value=dict(attempt.manifest);value['files']=dict(value['files'])
    value['files'][transcript.relative_to(legacy.parent).as_posix()]=dict(sha256=backups.checksum(transcript),size=transcript.stat().st_size,verification='VERIFIED_AT_CREATION')
    attempt.manifest=value;db_session.commit()
    result=backups.create()
    assert any('/transcript/cues.json' in r['path'] for r in backups.verify(folder(result))['required'])


def test_backup_refuses_background_upload_verification(legacy,db_session):
    from src.database.models import UploadSession
    from fastapi import HTTPException
    db_session.add(UploadSession(upload_id='a'*32,owner_username='test',filename='video.mp4',total_size=100,chunk_size=10,received_offset=100,status='VERIFYING',created_at=time.time(),updated_at=time.time(),expires_at=time.time()+100))
    db_session.commit()
    with pytest.raises(HTTPException,match='409'):backups.create()
    assert not access.maintenance
