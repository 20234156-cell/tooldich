"""Small project artifacts, independently checksum-verified on Drive."""
import datetime as dt
import json
import mimetypes
import uuid
from types import SimpleNamespace
from config.settings import settings
from src.cloud import drive
from src.cloud.archive import digest, verify_remote, CHUNK_SIZE, account_operation
from src.jobs.logger import TaskCancelled
from src.translation.memory import atomic_json, fingerprint

FILES=('transcript/vi.srt','voiceover/vi.dub.srt','transcript/source.srt',
       'output/publishing_pack.json','output/verify_report.json','output/audio_qc.json')


def project_root(project_id):
    from src.database import connection
    from src.database.models import Project
    from src.security.validators import validate_identifier
    project_id=validate_identifier(project_id,'Project ID')
    with connection.SessionLocal() as db:
        if not db.get(Project,project_id):raise drive.CloudError('Không tìm thấy dự án.')
    root=(settings.storage_path/project_id).resolve()
    if not root.is_relative_to(settings.storage_path.resolve()):raise drive.CloudError('Dự án ngoài thư mục dữ liệu.')
    return root


def inventory(project_id):
    from src.pipeline.attempts import artifact_path
    root=project_root(project_id); files=[]
    for name in FILES:
        path=artifact_path(root,name)
        if not path.is_file():continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):raise drive.CloudError('Tệp hồ sơ ngoài dự án.')
        size,md5,sha,_=digest(path)
        files.append(dict(path=name,size=size,md5=md5,sha256=sha,local=path))
    if not files:raise drive.CloudError('Chưa có hồ sơ nhẹ để lưu.')
    identity=fingerprint([{k:v for k,v in f.items() if k!='local'} for f in files])
    return root,files,identity


def receipt_status(project_id):
    root=project_root(project_id)
    try:
        value=json.loads((root/'output/drive_archive.json').read_text(encoding='utf-8'))
        if value.get('mode')=='full_delivery':
            from src.cloud.archive import delivery
            try:
                matches=digest(delivery(project_id))[2]==value.get('mp4_sha256') and value.get('account_id')==drive.config().get('account_id') and value.get('status')=='VERIFIED'
            except (OSError,drive.CloudError):
                matches=False
            return dict(receipt=value,matches_current=matches,remote_rechecked=False)
        _,_,identity=inventory(project_id)
        account=drive.config().get('account_id')
        valid=(value.get('content_hash')==identity and value.get('account_id')==account
               and value.get('status')=='VERIFIED' and bool(value.get('files')))
        return dict(receipt=value, matches_current=valid, remote_rechecked=False)
    except (OSError,ValueError,TypeError,AttributeError,drive.CloudError):return dict(receipt=None,matches_current=False,remote_rechecked=False)


def light_receipt_status(project_id):
    """Keep the metadata receipt independent of the full MP4 backup receipt."""
    root=project_root(project_id)
    for name in ('drive_metadata_archive.json','drive_archive.json'):
        try:
            value=json.loads((root/'output'/name).read_text(encoding='utf-8'))
            if value.get('mode')!='metadata_only':continue
            _,_,identity=inventory(project_id)
            valid=(value.get('content_hash')==identity and value.get('status')=='VERIFIED'
                   and value.get('account_id')==drive.config().get('account_id') and bool(value.get('files')))
            return dict(receipt=value,matches_current=valid,remote_rechecked=False)
        except (OSError,ValueError,TypeError,AttributeError,drive.CloudError):
            continue
    return dict(receipt=None,matches_current=False,remote_rechecked=False)


@account_operation
def archive_project_metadata_pack(project_id,should_continue=None,progress_callback=None):
    drive.require_online()
    if should_continue and not should_continue():raise TaskCancelled()
    root=project_root(project_id)
    from src.pipeline.attempts import artifact_path
    if not artifact_path(root,'output/publishing_pack.json').is_file():
        from src.cloud.youtube_upload import publishing_pack
        publishing_pack(project_id)
    root,files,identity=inventory(project_id)
    account=drive.config().get('account_id')
    if not account:raise drive.CloudError('Chưa kết nối Google Drive.')
    client=drive.Drive()
    def check():
        if should_continue and not should_continue():raise TaskCancelled()
        if drive.config().get('account_id')!=account:raise drive.CloudError('Tài khoản Drive đã đổi.')
    previous=light_receipt_status(project_id)
    if previous['matches_current']:
        receipt=previous['receipt']
        saved=receipt.get('files')
        if not isinstance(saved,list) or len(saved)!=len(files) or any(not isinstance(row,dict) for row in saved):
            raise drive.CloudError('Biên nhận hồ sơ nhẹ không hợp lệ; chưa dùng lại bản lưu.')
        by_path={row.get('path'):row for row in saved}
        if len(by_path)!=len(files):raise drive.CloudError('Biên nhận hồ sơ nhẹ bị trùng tệp.')
        for item in files:
            check()
            old=by_path.get(item['path'],{})
            if any(old.get(key)!=item[key] for key in ('size','md5','sha256')):
                raise drive.CloudError('Biên nhận hồ sơ nhẹ không khớp tệp hiện tại.')
            row=SimpleNamespace(remote_id=old.get('remote_id'),file_size=item['size'],md5=item['md5'])
            verify_remote(client,row)
        check()
        if inventory(project_id)[2]!=identity:raise drive.CloudError('Hồ sơ thay đổi, cần tải lại.')
        if progress_callback:progress_callback(100,'Đã kiểm tra lại hồ sơ nhẹ trên Drive')
        return receipt
    parent=client.project_folder(project_id)
    journal=root/'output/.drive_metadata_sessions.json'
    try:state=json.loads(journal.read_text(encoding='utf-8'))
    except (OSError,ValueError):state={}
    if state.get('identity')!=identity or state.get('account_id')!=account:
        state=dict(identity=identity,account_id=account,files={})
    verified=[]
    for number,item in enumerate(files):
        check();entry=state['files'].get(item['path'])
        if entry is None:
            entry={'id':client.new_id()};state['files'][item['path']]=entry;atomic_json(journal,state)
        row=SimpleNamespace(id=str(uuid.uuid4()),project_id=project_id,remote_id=entry['id'],
            file_size=item['size'],md5=item['md5'],sha256=item['sha256'],parent_id=parent,
            filename=item['path'].replace('/','-'),content_type=mimetypes.guess_type(item['path'])[0] or 'application/octet-stream')
        if not client.exists(row.remote_id):
            if not entry.get('session'):
                entry['session']=drive.seal({'url':client.begin(row)});atomic_json(journal,state)
            session=drive.unseal(entry['session'])['url']
            from src.cloud.resumable import transfer
            def transmit(offset, chunk):
                return client.send(session,row.file_size,offset,chunk), None
            with item['local'].open('rb') as stream:
                try:
                    transfer(stream,row.file_size,transmit,chunk_size=CHUNK_SIZE,check=check)
                except drive.ExpiredSession:
                    check()
                    if not client.exists(row.remote_id):
                        entry['session']=drive.seal({'url':client.begin(row)});atomic_json(journal,state)
                        session=drive.unseal(entry['session'])['url']
                        transfer(stream,row.file_size,transmit,chunk_size=CHUNK_SIZE,check=check)
        verify_remote(client,row)
        if digest(item['local'])[:3]!=(item['size'],item['md5'],item['sha256']):
            raise drive.CloudError('Hồ sơ thay đổi trong lúc tải; chưa xác nhận bản mới.')
        verified.append({k:v for k,v in item.items() if k!='local'} | {'remote_id':row.remote_id})
        if progress_callback:progress_callback(int((number+1)*100/len(files)),'Đã xác minh hồ sơ nhẹ trên Drive')
    check()
    if inventory(project_id)[2]!=identity:raise drive.CloudError('Hồ sơ thay đổi, cần tải lại.')
    receipt=dict(status='VERIFIED',mode='metadata_only',content_hash=identity,files=verified,
        account_id=account,uploaded_at=dt.datetime.now(dt.timezone.utc).isoformat())
    try:
        current=json.loads((root/'output/drive_archive.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):
        current={}
    atomic_json(root/'output/drive_metadata_archive.json',receipt)
    if not (isinstance(current,dict) and current.get('mode')=='full_delivery'):
        atomic_json(root/'output/drive_archive.json',receipt)
    journal.unlink(missing_ok=True)
    return receipt
