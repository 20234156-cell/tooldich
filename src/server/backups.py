"""Consistent, verified SQLite/data snapshots and non-destructive offline restore."""
import asyncio
import hashlib
import json
import shutil
import sqlite3
import time
import uuid
import os
from contextlib import closing
from pathlib import Path

from fastapi import HTTPException
from config.settings import settings
from src.server import store, access
from src.server.backup_inventory import inventory, logical_tables


class BackupSourceError(ValueError):
    def __init__(self, status, errors, name):
        self.status, self.errors, self.name = status, errors, name
        super().__init__(status + ': ' + '; '.join(e.get('path', e.get('reference', e['reason'])) for e in errors)[:500])


def write_json(path, data):
    temporary=path.with_suffix(path.suffix+'.tmp')
    with temporary.open('w',encoding='utf-8') as stream:
        json.dump(data,stream,indent=2,ensure_ascii=False);stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


def snapshot(path):
    from src.database import connection
    raw=connection.engine.raw_connection()
    try:
        with closing(sqlite3.connect(path)) as dest:raw.driver_connection.backup(dest)
    finally:raw.close()


def backfill_baselines(snapshot_file):
    """Only enroll missing legacy baselines; never repair a mismatching baseline."""
    from src.database.models import FileBaseline
    catalog=inventory(snapshot_file,settings.storage_path.parent,settings.storage_path.name)
    enrolled=[]
    with store.transaction() as db:
        for item in catalog['required']:
            if item['provenance'] in {'VERIFIED_AT_CREATION','SNAPSHOT_METADATA'} or db.get(FileBaseline,item['path']):continue
            path=Path(item['source'])
            if not path.is_file():continue
            digest=checksum(path);size=path.stat().st_size
            if item['expected_sha256'] and item['expected_sha256']!=digest:continue
            if item['expected_size'] is not None and item['expected_size']!=size:continue
            db.add(FileBaseline(path=item['path'],sha256=digest,size=size,provenance='BASELINE_FROM_MIGRATION',recorded_at=time.time()))
            enrolled.append(item['path'])
    return enrolled


def capture_legacy_references():
    """Freeze legacy pipeline metadata under the same maintenance gate as snapshot.

    Legacy stage artifacts live in pipeline_state.json rather than SQL. Import
    those references into the snapshot; never discover requirements by globbing
    for whatever media happens to remain on disk.
    """
    from src.database.models import Project,ArtifactPointer,LegacyBackupReference
    with store.transaction() as db:
        db.query(LegacyBackupReference).delete()
        for project in db.query(Project):
            root=settings.storage_path/project.id
            state=root/'pipeline_state.json'
            if not state.is_file():continue
            value=json.loads(state.read_text(encoding='utf-8'))
            refs={state.relative_to(settings.storage_path.parent).as_posix():'legacy_pipeline:'+project.id}
            for stage,metadata in value.get('stages',{}).items():
                if metadata.get('status')!='COMPLETED':continue
                for role,path in metadata.get('artifacts',{}).items():
                    parts=Path(path).parts
                    if parts and parts[0] in {'output','voiceover'} and db.get(ArtifactPointer,(project.id,parts[0])):continue
                    target=Path(path) if Path(path).is_absolute() else root/path
                    if not target.resolve().is_relative_to(settings.storage_path.parent.resolve()):raise ValueError('Legacy artifact ngoài vùng quản lý')
                    refs[target.relative_to(settings.storage_path.parent).as_posix()]='legacy_stage:'+project.id+':'+stage+':'+role
            for path,reference in refs.items():
                source=settings.storage_path.parent/path
                mutable=source.suffix.lower() in {'.json','.srt','.ass','.txt'}
                db.add(LegacyBackupReference(path=path,reference=reference,sha256=checksum(source) if mutable and source.is_file() else None,
                                           size=source.stat().st_size if mutable and source.is_file() else None))


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def backup_root():
    return Path(settings.SERVER_BACKUP_ROOT) if settings.SERVER_BACKUP_ROOT else settings.storage_path.parent/'backups'


def backup_roots():
    return list(dict.fromkeys([backup_root().resolve(),(settings.storage_path.parent/'backups').resolve()]))


def find_backup(name):
    from src.security.validators import validate_identifier
    validate_identifier(name)
    matches=[root/name for root in backup_roots() if (root/name/'manifest.json').is_file() and (root/name).resolve().parent==root]
    if len(matches)!=1:raise ValueError('Không tìm thấy phiên sao lưu duy nhất.')
    return matches[0]


def managed_files(folder,excluded):
    """Do not traverse a live Chromium profile (changing/locked/long cache paths)."""
    for current,directories,files in os.walk(folder,followlinks=False):
        directories[:]=[name for name in directories if not any((Path(current)/name).resolve().is_relative_to(root) for root in excluded)]
        if any(Path(current).resolve().is_relative_to(root) for root in excluded):continue
        for name in files:
            yield Path(current)/name


def verify(folder):
    root=Path(folder).resolve()
    manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('version') not in (1,2) or not manifest.get('complete'):
        raise ValueError('Bản sao chưa hoàn tất hoặc không được hỗ trợ.')
    if manifest['version']==2 and manifest.get('status')!='VALIDATED_PASS':raise ValueError('Bản sao không đạt VALIDATED_PASS.')
    for name,digest in manifest['files'].items():
        path=(root/name).resolve()
        if not path.is_relative_to(root) or not path.is_file() or checksum(path)!=digest:
            raise ValueError('File sao lưu thiếu/hỏng: '+name)
    with closing(sqlite3.connect('file:'+str(root/'app.db').replace('\\','/')+'?mode=ro',uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise ValueError('Database sao lưu không toàn vẹn.')
    if manifest['version']==2:
        derived=inventory(root/'app.db',manifest['data_root'],manifest['projects_name'])
        if derived['errors'] or derived['required']!=manifest['required'] or derived['empty']!=manifest['intentionally_empty']:
            raise ValueError('Danh mục REQUIRED không khớp snapshot database.')
        for item in derived['required']:
            path=root/item['path']
            if not item['expected_sha256'] or not path.is_file() or path.stat().st_size!=item['expected_size'] or checksum(path)!=item['expected_sha256']:
                raise ValueError('File sao lưu thiếu/hỏng: '+item['path'])
        if logical_tables(root/'app.db',manifest['tables'])!=manifest['tables']:raise ValueError('Các bảng snapshot không khớp manifest.')
    return manifest


def create(expected_writers=0):
    from src.jobs.queue import queue
    with access.maintenance_lock:
        if access.maintenance or access.inflight_writes!=expected_writers:
            raise HTTPException(409,'Có thao tác đang ghi. Hãy thử sao lưu lại khi máy rảnh.')
        with queue._lock:
            if any(t.state in {'QUEUED','RUNNING'} or (t.future and not t.future.done()) for t in queue._tasks.values()):
                raise HTTPException(409,'Hãy chờ mọi tác vụ dừng/hoàn tất trước khi sao lưu.')
        access.maintenance=True
    target=None
    try:
        from src.database.models import UploadSession
        from src.cloud import drive
        with store.transaction() as db:
            if db.query(UploadSession).filter_by(status='VERIFYING').first():raise HTTPException(409,'Đang kiểm định upload; chờ trước khi sao lưu.')
        if drive._active_operations:raise HTTPException(409,'Đang có thao tác cloud; chờ trước khi sao lưu.')
        if not settings.DATABASE_URL.startswith('sqlite:///'):
            raise ValueError('Bản sao này chỉ hỗ trợ SQLite cục bộ.')
        data_root=settings.storage_path.parent.resolve()
        root=backup_root().resolve()
        if root.is_relative_to(settings.storage_path.resolve()):raise ValueError('Không đặt kho backup bên trong projects.')
        root.mkdir(parents=True,exist_ok=True)
        target=root/(time.strftime('%Y%m%dT%H%M%S')+'_'+uuid.uuid4().hex[:8])
        target.mkdir()
        capture_legacy_references()
        snapshot(target/'app.db')
        backfilled=backfill_baselines(target/'app.db')
        if backfilled:
            (target/'app.db').unlink()
            snapshot(target/'app.db')
        catalog=inventory(target/'app.db',data_root,settings.storage_path.name)
        errors=list(catalog['errors'])
        for item in catalog['required']:
            path=Path(item['source'])
            if not path.is_file():errors.append(dict(reason='MISSING_REQUIRED',path=item['path']))
            elif not item['expected_sha256']:errors.append(dict(reason='MISSING_BASELINE',path=item['path']))
            elif checksum(path)!=item['expected_sha256'] or path.stat().st_size!=item['expected_size']:
                errors.append(dict(reason='CORRUPT_SOURCE',path=item['path']))
        if errors:
            status='FAILED_CORRUPT_SOURCE' if any(e['reason'] in {'CORRUPT_SOURCE','CONFLICTING_BASELINE'} for e in errors) else 'FAILED_INCOMPLETE'
            failure=dict(version=2,complete=False,status=status,time=time.time(),files={},errors=errors)
            write_json(target/'manifest.json',failure)
            store.put('backup:last_error',dict(time=time.time(),message=status,name=target.name,errors=errors))
            raise BackupSourceError(status,errors,target.name)
        sources=[]
        excluded={ (data_root/'private/douyin_browser_profile').resolve(),
                   (Path(settings.BASE_DIR)/'storage/private/douyin_browser_profile').resolve() }
        if settings.DOUYIN_BROWSER_PROFILE:excluded.add(Path(settings.DOUYIN_BROWSER_PROFILE).resolve())
        managed=[settings.storage_path,*(data_root/name for name in ('voices','uploads','private','tts_cache','tts','eta_stats.json'))]
        for child in managed:
            if not child.exists():continue
            if child.is_file():sources.append((child,child.name))
            elif child.is_dir():
                sources.extend((p,str(p.relative_to(data_root))) for p in managed_files(child,excluded) if p.is_file())
        if any(source.is_symlink() or not source.resolve().is_relative_to(data_root) for source,_ in sources):
            raise ValueError('Không sao lưu tệp liên kết tới vị trí ngoài dữ liệu; cần kiểm tra cấu hình.')
        required_paths={item['path'] for item in catalog['required']}
        sources=[(p,n) for p,n in sources if (not any(part in {'attempts','tts_cache','logs'} for part in Path(n).parts) and not Path(n).as_posix().startswith('uploads/sessions/')) or Path(n).as_posix() in required_paths]
        source_map={Path(n).as_posix():p for p,n in sources}
        source_map.update({item['path']:Path(item['source']) for item in catalog['required']})
        sources=[(p,n) for n,p in source_map.items()]
        required=sum(source.stat().st_size for source,_ in sources)
        if shutil.disk_usage(root).free < required*1.1+200*1024**2:
            raise HTTPException(409,'Không đủ dung lượng cho bản sao mới; không xóa bản cũ tự động.')
        for source,relative in sources:
            dest=target/relative
            dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source,dest)
            with dest.open('r+b') as stream:os.fsync(stream.fileno())
            item=next((entry for entry in catalog['required'] if entry['path']==Path(relative).as_posix()),None)
            if item and (checksum(dest)!=item['expected_sha256'] or dest.stat().st_size!=item['expected_size']):
                failure=dict(version=2,complete=False,status='FAILED_CORRUPT_SOURCE',time=time.time(),files={},errors=[dict(reason='CHANGED_DURING_COPY',path=item['path'])])
                write_json(target/'manifest.json',failure)
                raise BackupSourceError(failure['status'],failure['errors'],target.name)
        env=Path(settings.BASE_DIR)/'.env'
        if env.is_file():shutil.copy2(env,target/'environment.reference')
        keys=Path(settings.BASE_DIR)/'config/gemini_keys.bin'
        if keys.is_file():shutil.copy2(keys,target/'gemini_keys.reference.bin')
        files={str(p.relative_to(target)).replace('\\','/'):checksum(p) for p in target.rglob('*') if p.is_file()}
        manifest=dict(version=2,complete=True,status='VALIDATED_PASS',time=time.time(),data_root=str(data_root),projects_name=settings.storage_path.name,files=files,
                      required=catalog['required'],intentionally_empty=catalog['empty'],optional_category='OPTIONAL_OR_UNCOMMITTED',tables=logical_tables(target/'app.db'),backfilled=backfilled,
                      excluded=['Persistent Chromium browser profiles: log in again after restore; live browser files are not a consistent snapshot.'])
        write_json(target/'manifest.json',manifest)
        verify(target)
        store.put('backup:last',{'time':time.time(),'name':target.name})
        store.put('backup:last_error',{})
        result={'name':target.name,'files':len(files),'verified':True,'status':'VALIDATED_PASS','backfilled':len(backfilled),'deletion_allowed':False}
        if settings.SERVER_BACKUP_DIRECTORY:
            from src.server.backup_remote import export_directory
            try:
                record=export_directory(target,settings.SERVER_BACKUP_DIRECTORY,settings.SERVER_BACKUP_ALLOW_SAME_DISK)
                result['replica_status']=record['status']
            except Exception as exc:
                # A transfer failure must never remove the locally validated package.
                result['replica_error']=str(exc)[:300]
                store.put('backup:last_error',dict(time=time.time(),name=target.name,message='Bản cục bộ đạt; chuyển kho chưa xong: '+str(exc)[:200]))
        return result
    except BackupSourceError:
        raise
    except Exception:
        if target and target.resolve().is_relative_to(backup_root().resolve()):
            shutil.rmtree(target)
        raise
    finally:
        with access.maintenance_lock:
            access.maintenance=False


def restore(folder,destination):
    """Restore only to a new data directory; never overwrite live user projects."""
    manifest=verify(folder)
    source=Path(folder).resolve()
    target=Path(destination).resolve()
    if target.is_relative_to(settings.storage_path.resolve()):raise ValueError('Phục hồi phải nằm ngoài thư mục dự án đang dùng.')
    if target.exists():
        raise ValueError('Thư mục phục hồi phải chưa tồn tại; không ghi đè dữ liệu đang dùng.')
    target.parent.mkdir(parents=True,exist_ok=True)
    required=sum((source/name).stat().st_size for name in manifest['files'])
    if shutil.disk_usage(target.parent).free<required+200*1024**2:raise ValueError('Không đủ đĩa để phục hồi; dữ liệu đang dùng được giữ nguyên.')
    target.mkdir(parents=True)
    for name in manifest['files']:
        path=(target/name).resolve()
        if not path.is_relative_to(target):raise ValueError('Đường dẫn phục hồi không hợp lệ.')
        path.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(source/name,path)
    old=manifest['data_root']
    new=str(target)
    def replace(text):
        substitutions={}
        for item in manifest.get('required',[]):
            original=item['source'];relocated=str(target/item['path'])
            if item['path'].startswith('external_required/'):
                for a,b in [(original.replace('\\','\\\\'),relocated.replace('\\','\\\\')),(original.replace('\\','/'),relocated.replace('\\','/')),(original,relocated)]:
                    token='__RESTORE_EXTERNAL_'+str(len(substitutions))+'__';substitutions[token]=b;text=text.replace(a,token)
        for a,b in [(old.replace('\\','\\\\'),new.replace('\\','\\\\')),(old.replace('\\','/'),new.replace('\\','/')),(old,new)]:
            text=text.replace(a,b)
        for token,value in substitutions.items():text=text.replace(token,value)
        return text
    # Update stored local media/upload paths, while leaving remote URLs intact.
    with closing(sqlite3.connect(target/'app.db')) as db:
        tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            quoted='"'+table.replace('"','""')+'"'
            for row in db.execute(f'SELECT rowid,* FROM {quoted}').fetchall():
                columns=[col[1] for col in db.execute(f'PRAGMA table_info({quoted})')]
                for index,value in enumerate(row[1:]):
                    if isinstance(value,str) and replace(value)!=value:
                        column='"'+columns[index].replace('"','""')+'"'
                        db.execute(f'UPDATE {quoted} SET {column}=? WHERE rowid=?',(replace(value),row[0]))
        # Restored sessions/invitations must not authorize a new server installation.
        db.execute("DELETE FROM server_records WHERE key LIKE 'session:%' OR key LIKE 'lease:%' OR key LIKE 'invite:%' OR key LIKE 'reset:preview:%'")
        if 'cleanup_batches' in tables:db.execute('UPDATE cleanup_batches SET expires_at=0')
        db.commit()
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Database phục hồi không toàn vẹn.')
    # Keep artifact bytes identical to baseline. Runtime artifact paths are relative;
    # rewriting arbitrary JSON would invalidate VERIFIED_AT_CREATION manifests.
    if manifest.get('version')==2:
        actual=logical_tables(target/'app.db',manifest['tables'])
        expected=json.loads(replace(json.dumps(manifest['tables'],ensure_ascii=False)))
        for row in expected.get('cleanup_batches',[]):row['expires_at']=0
        expected={table:sorted(rows,key=lambda row:json.dumps(row,sort_keys=True)) for table,rows in expected.items()}
        if actual!=expected:raise ValueError('Các bảng phục hồi không khớp snapshot sau chuyển đường dẫn.')
        for item in manifest['required']:
            path=target/item['path']
            if path.stat().st_size!=item['expected_size'] or checksum(path)!=item['expected_sha256']:raise ValueError('REQUIRED phục hồi hỏng: '+item['path'])
    configuration=f'DATABASE_URL=sqlite:///{(target/"app.db").as_posix()}\nSTORAGE_DIR={target/manifest["projects_name"]}\nSERVER_MODE=true\n'
    (target/'restore.env').write_text(configuration,encoding='utf-8')
    return {'directory':str(target),'verified':True,'configuration':'restore.env','note':'Model/công cụ vẫn cần cài; DPAPI/đăng nhập nguồn có thể cần làm lại trên tài khoản Windows khác.'}


async def automatic_loop():
    from starlette.concurrency import run_in_threadpool
    while True:
        await asyncio.sleep(60)
        if not settings.SERVER_AUTO_BACKUP_HOURS:continue
        last=store.get('backup:last')
        if last and time.time()-last['time']<settings.SERVER_AUTO_BACKUP_HOURS*3600:continue
        failure=store.get('backup:last_error') or {}
        if time.time()<failure.get('retry_after',0):continue
        try:
            await run_in_threadpool(create)
        except Exception as exc:
            attempts=failure.get('attempts',0)+1
            delay=min(3600,300*2**min(attempts-1,4))
            store.put('backup:last_error',{'time':time.time(),'error':type(exc).__name__,'message':str(exc)[:300],
                                         'attempts':attempts,'retry_after':time.time()+delay})
