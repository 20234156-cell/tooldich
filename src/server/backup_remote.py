"""Versioned system packages; reuse Drive transport, never prune good versions."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from types import SimpleNamespace
import zipfile

from src.cloud.archive import account_operation, digest, verify_remote, CHUNK_SIZE
from src.cloud.drive import CloudError, Drive, config, seal, unseal, ExpiredSession
from src.server import store
from src.server.backups import verify, restore, checksum, backup_root, write_json


def package(folder):
    folder=Path(folder).resolve()
    manifest=verify(folder)
    if manifest.get('status')!='VALIDATED_PASS':raise ValueError('Chỉ xuất gói VALIDATED_PASS v2.')
    root=backup_root()/'packages';root.mkdir(parents=True,exist_ok=True)
    target=root/(folder.name+'.zip');temporary=target.with_suffix('.zip.tmp')
    if target.exists():
        # Never accept a stale/partial archive just because the name matches.
        with tempfile.TemporaryDirectory(dir=root) as staging:
            recovered=unpack(target,Path(staging)/'package')
            if verify(recovered)!=manifest:raise ValueError('Gói đã có không khớp phiên sao lưu; không ghi đè.')
        return target
    try:
        with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as archive:
            for name in [*manifest['files'],'manifest.json']:archive.write(folder/name,name)
        with temporary.open('r+b') as stream:os.fsync(stream.fileno())
        os.replace(temporary,target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def unpack(package_path,destination):
    target=Path(destination).resolve()
    if target.exists():raise ValueError('Thư mục giải gói phải chưa tồn tại.')
    target.mkdir(parents=True)
    with zipfile.ZipFile(package_path) as archive:
        names=set()
        total=sum(item.file_size for item in archive.infolist())
        if shutil.disk_usage(target).free<total+200*1024**2:raise ValueError('Không đủ đĩa để giải gói.')
        for item in archive.infolist():
            path=(target/item.filename).resolve()
            if path==target or not path.is_relative_to(target) or item.filename in names or item.is_dir() or ((item.external_attr>>16)&0o170000)==0o120000:
                raise ValueError('Đường dẫn gói không hợp lệ.')
            names.add(item.filename);path.parent.mkdir(parents=True,exist_ok=True)
            with archive.open(item) as source,path.open('xb') as dest:shutil.copyfileobj(source,dest)
    manifest=verify(target)
    if names!={*manifest['files'],'manifest.json'}:raise ValueError('Gói chứa tệp ngoài manifest.')
    return target


def distinct_volume(destination):
    """Different drive letters alone are not proof of different physical disks."""
    from config.settings import settings
    source=settings.storage_path.resolve()
    destination=Path(destination).resolve()
    if destination.is_relative_to(source.parent):return False
    if os.name!='nt':return source.stat().st_dev!=destination.stat().st_dev
    if str(destination).startswith('\\\\'):return True
    def disk(path):
        letter=path.drive.rstrip(':').upper()
        if len(letter)!=1 or not letter.isalpha():raise ValueError('Không xác định ổ vật lý.')
        result=subprocess.run(['powershell','-NoProfile','-NonInteractive','-Command',f"Get-Partition -DriveLetter {letter} | Select-Object -ExpandProperty DiskNumber"],capture_output=True,text=True,check=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        return int(result.stdout.strip())
    return disk(source)!=disk(destination)


def export_directory(folder,destination,allow_same_disk=False):
    destination=Path(destination).resolve()
    if not destination.is_dir():raise ValueError('Thư mục đích chưa tồn tại.')
    independent=distinct_volume(destination)
    if not independent and not allow_same_disk:raise ValueError('Đích phải là ổ vật lý khác hoặc kho mạng độc lập.')
    packed=package(folder);target=destination/packed.name
    if target.exists():
        if checksum(target)!=checksum(packed):raise ValueError('Tên phiên đã tồn tại nhưng checksum khác; không ghi đè.')
    else:
        if shutil.disk_usage(destination).free<packed.stat().st_size+200*1024**2:raise ValueError('Không đủ dung lượng kho sao chép.')
        temporary=destination/(packed.name+'.'+str(time.time_ns())+'.tmp')
        try:
            shutil.copyfile(packed,temporary)
            with temporary.open('r+b') as stream:os.fsync(stream.fileno())
            if checksum(temporary)!=checksum(packed):raise ValueError('Kho độc lập sai checksum.')
            # Exclusive destination prevents replacing an existing good version.
            os.link(temporary,target)  # Atomic no-replace publish on the destination volume.
        finally:temporary.unlink(missing_ok=True)
    record=dict(kind='directory',path=str(target),sha256=checksum(packed),size=packed.stat().st_size,independent=independent,status='REMOTE_VERIFIED' if independent else 'LOCAL_REPLICA_VERIFIED',time=time.time())
    name=Path(folder).name
    store.put('backup:replica:'+name,record)
    preferred=store.get('backup:remote:'+name) or {}
    if preferred.get('kind')!='drive' or preferred.get('status')!='REMOTE_VERIFIED':store.put('backup:remote:'+name,record)
    return record


@account_operation
def export_drive(folder):
    packed=package(folder);cfg=config()
    if not cfg.get('account_id') or not cfg.get('refresh_token'):raise CloudError('Kết nối Google Drive trước khi xuất gói sao lưu.')
    size,md5,sha,_=digest(packed);key='backup:drive:'+Path(folder).name
    data=store.get(key) or {}
    if data and (data.get('sha256')!=sha or data.get('account_id')!=cfg['account_id']):raise CloudError('Gói hoặc tài khoản đổi; không ghi đè phiên trước.')
    drive=Drive()
    if not data:
        data=dict(kind='drive',account_id=cfg['account_id'],remote_id=drive.new_id(),sha256=sha,md5=md5,file_size=size,
                  id=Path(folder).name,project_id='system-backup',filename=packed.name,content_type='application/zip',status='PENDING',time=time.time())
        store.put(key,data)
    row=SimpleNamespace(**data)
    if not drive.exists(row.remote_id):
        session=unseal(data['session'])['url'] if data.get('session') else None
        if not session:
            session=drive.begin(row);data['session']=seal({'url':session});store.put(key,data)
        try:offset=drive.send(session,size)
        except ExpiredSession:
            session=drive.begin(row);data['session']=seal({'url':session});store.put(key,data);offset=drive.send(session,size)
        with packed.open('rb') as stream:
            while offset<size:
                stream.seek(offset);chunk=stream.read(min(CHUNK_SIZE,size-offset));next_offset=drive.send(session,size,offset,chunk)
                if not offset<next_offset<=offset+len(chunk):raise CloudError('Drive xác nhận sai phạm vi gói sao lưu.')
                offset=next_offset
    verify_remote(drive,row)
    if digest(packed)[:3]!=(size,md5,sha):raise CloudError('Gói thay đổi trong lúc tải.')
    data.pop('session',None);data['status']='REMOTE_VERIFIED';store.put(key,data)
    store.put('backup:remote:'+Path(folder).name,data)
    return {'status':data['status'],'remote_id':data['remote_id'],'deletion_allowed':False}


def fetch(record,target):
    target=Path(target)
    if target.exists():raise ValueError('Không ghi đè file tải xuống.')
    required=record.get('size',record.get('file_size',0))
    if shutil.disk_usage(target.parent).free<required+200*1024**2:raise ValueError('Không đủ dung lượng để tải gói phục hồi.')
    if record['kind']=='directory':
        path=Path(record['path'])
        if record.get('independent') and not distinct_volume(path.parent):raise ValueError('Kho không còn độc lập.')
        shutil.copyfile(path,target)
    else:
        from src.cloud.drive import API,request,valid_id
        if config().get('account_id')!=record['account_id']:raise CloudError('Tài khoản Drive không khớp.')
        drive=Drive();verify_remote(drive,SimpleNamespace(**record))
        with request('GET',API+'/files/'+valid_id(record['remote_id']),headers=drive.headers,params={'alt':'media'},stream=True) as response:
            if response.status_code!=200:raise CloudError('Không tải được gói sao lưu từ Drive.')
            with target.open('xb') as stream:
                for chunk in response.iter_content(CHUNK_SIZE):stream.write(chunk)
    if checksum(target)!=record['sha256']:raise ValueError('Gói từ kho độc lập không khớp SHA-256.')
    return target


@account_operation
def drill(name,destination):
    from src.security.validators import validate_identifier
    validate_identifier(name)
    record=store.get('backup:remote:'+name)
    if not record or record.get('status') not in {'REMOTE_VERIFIED','LOCAL_REPLICA_VERIFIED'}:raise ValueError('Chưa có gói đã xác minh.')
    destination=Path(destination).resolve()
    if destination.exists():raise ValueError('Thư mục phục hồi phải chưa tồn tại.')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='restore_drill_',dir=destination.parent) as staging:
        staging=Path(staging);packed=fetch(record,staging/'download.zip');unpacked=unpack(packed,staging/'package')
        result=restore(unpacked,destination)
        manifest=verify(unpacked)
    record['drill_at']=time.time();record['required']={item['source']:item['expected_sha256'] for item in manifest['required']}
    store.put('backup:remote:'+name,record)
    status='RESTORE_DRILL_PASS' if record['status']=='REMOTE_VERIFIED' else 'LOCAL_RESTORE_DRILL_PASS'
    write_json(destination/'drill_report.json',dict(status=status,required_files=len(manifest['required']),tables=list(manifest['tables']),package_sha256=record['sha256']))
    return dict(result,status=status,required_files=len(manifest['required']),deletion_allowed=record['status']=='REMOTE_VERIFIED')


def deletion_allowed(path):
    """Fail closed; future cleanup must additionally acquire its reader/delete lock."""
    path=Path(path).resolve()
    if not path.is_file():return False
    sha=checksum(path)
    for _,record in store.rows('backup:remote:'):
        if record.get('status')!='REMOTE_VERIFIED' or not record.get('drill_at') or record.get('required',{}).get(str(path))!=sha:continue
        try:
            with tempfile.TemporaryDirectory(prefix='backup_recheck_') as folder:fetch(record,Path(folder)/'package.zip')
            return True
        except Exception:continue
    return False
