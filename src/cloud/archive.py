"""Byte-identical archive, persisted resumable state, and explicit local cleanup."""
import datetime as dt
import hashlib
import uuid
from functools import wraps
from pathlib import Path

from config.settings import settings
from src.cloud.drive import CloudError, Drive, config, seal, unseal
from src.database import connection
from src.database.models import CloudUpload, Project
from src.jobs.logger import TaskCancelled
from src.cloud.drive import ExpiredSession

CHUNK_SIZE = 8*1024*1024  # Multiple of Drive's 256 KiB chunk unit.


def account_operation(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        from src.cloud import drive
        with drive.LOCK:
            drive._active_operations += 1
        try:
            return function(*args, **kwargs)
        finally:
            with drive.LOCK:
                drive._active_operations -= 1
    return wrapped


def persist(row):
    with connection.SessionLocal() as db:
        db.merge(row)
        db.commit()


def digest(path, should_continue=None):
    md5, sha = hashlib.md5(), hashlib.sha256()
    with path.open('rb') as stream:
        before = path.stat()
        while True:
            if should_continue and not should_continue():
                raise TaskCancelled()
            chunk = stream.read(CHUNK_SIZE)
            if not chunk:
                break
            md5.update(chunk)
            sha.update(chunk)
    after = path.stat()
    if (before.st_size,before.st_mtime_ns,before.st_ino) != (after.st_size,after.st_mtime_ns,after.st_ino):
        raise CloudError('Video thay đổi trong lúc đọc; chưa tải lên hoặc xóa tệp.')
    return after.st_size, md5.hexdigest(), sha.hexdigest(), after


def delivery(project_id):
    from src.security.validators import validate_identifier
    from src.pipeline.state_machine import load_pipeline_state
    project_id = validate_identifier(project_id, 'Project ID')
    with connection.SessionLocal() as db:
        if not db.get(Project, project_id):
            raise CloudError('Không tìm thấy dự án.')
    root = (settings.storage_path/project_id).resolve()
    if not root.is_relative_to(settings.storage_path.resolve()):
        raise CloudError('Đường dẫn dự án không hợp lệ.')
    from src.pipeline.attempts import artifact_path
    path = artifact_path(root,'output/video_vi.mp4')
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise CloudError('Tệp video ngoài thư mục dự án; không xử lý.')
    state = load_pipeline_state(root)
    qa = state.stages.get('VERIFIED')
    if not qa or qa.status != 'COMPLETED':
        raise CloudError('Video chưa đạt QA kỹ thuật hiện tại. Hãy chạy QA trước khi lưu cloud.')
    if not path.is_file() or not path.stat().st_size:
        raise CloudError('Không còn video thành phẩm trên máy để tải lên.')
    return path


def public(row):
    return dict(id=row.id, project_id=row.project_id, status=row.status, file_size=row.file_size,
        uploaded_bytes=row.uploaded_bytes, error=row.error, local_removed=row.local_removed,
        verified_at=row.verified_at.replace(tzinfo=None).isoformat()+'Z' if row.verified_at else None,
        web_url='https://drive.google.com/file/d/'+row.remote_id+'/view' if row.status=='VERIFIED' else None)


def list_uploads(project_id=None):
    with connection.SessionLocal() as db:
        query = db.query(CloudUpload)
        if project_id:
            query = query.filter(CloudUpload.project_id == project_id)
        return [public(r) for r in query.order_by(CloudUpload.created_at.desc()).limit(100).all()]


def verify_remote(drive, row):
    meta = drive.metadata(row.remote_id)
    if meta.get('id') != row.remote_id or meta.get('trashed') is not False or str(meta.get('size')) != str(row.file_size) or meta.get('md5Checksum') != row.md5:
        raise CloudError('Kích thước/checksum Drive không khớp hoặc tệp đã bị xóa. Giữ nguyên video trên máy.')


@account_operation
def upload(project_id, should_continue=None, progress_callback=None):
    row = None
    def continuing():
        if should_continue and not should_continue():
            raise TaskCancelled()
    try:
        continuing()
        path = delivery(project_id)
        cfg = config()
        if not cfg.get('account_id') or not cfg.get('refresh_token'):
            raise CloudError('Chưa kết nối Google Drive.')
        if progress_callback:
            progress_callback(0,'Đang tính checksum video trước khi upload')
        size, md5, sha, initial = digest(path, should_continue)
        drive = Drive()
        with connection.SessionLocal() as db:
            row = db.query(CloudUpload).filter_by(project_id=project_id,account_id=cfg['account_id'],sha256=sha).order_by(CloudUpload.created_at.desc()).first()
        if row and row.status == 'VERIFIED':
            verify_remote(drive,row)
            return public(row)
        if row is None:
            row = CloudUpload(id=str(uuid.uuid4()),project_id=project_id,account_id=cfg['account_id'],
                file_size=size,md5=md5,sha256=sha,remote_id=drive.new_id(),status='PENDING',uploaded_bytes=0,local_removed=False)
            persist(row)
        if row.file_size != size or row.md5 != md5:
            raise CloudError('Identity video không khớp; không dùng lại phiên tải.')
        # Recover completion after a crash between Google's final response and DB commit.
        if drive.exists(row.remote_id):
            verify_remote(drive,row)
        else:
            if not row.encrypted_session:
                row.encrypted_session = seal({'url':drive.begin(row)})
                persist(row)
            session = unseal(row.encrypted_session)['url']
            try:
                offset = drive.send(session,size)
            except ExpiredSession:
                row.encrypted_session = seal({'url':drive.begin(row)})
                persist(row)
                session = unseal(row.encrypted_session)['url']
                offset = drive.send(session,size)
            row.status, row.error = 'UPLOADING', None
            persist(row)
            with path.open('rb') as stream:
                while offset < size:
                    continuing()
                    current = path.stat()
                    if (current.st_size,current.st_mtime_ns,current.st_ino) != (initial.st_size,initial.st_mtime_ns,initial.st_ino):
                        raise CloudError('Video thay đổi trong lúc tải. Giữ lại bản trên máy.')
                    stream.seek(offset)
                    chunk = stream.read(min(CHUNK_SIZE,size-offset))
                    next_offset = drive.send(session,size,offset,chunk)
                    if not offset < next_offset <= offset+len(chunk):
                        raise CloudError('Drive không xác nhận đúng phạm vi byte vừa tải. Bấm tải lại để kiểm tra phiên.')
                    offset = next_offset
                    row.uploaded_bytes = offset
                    persist(row)
                    if progress_callback:
                        progress_callback(min(99,int(offset*100/size)), f'Tải Drive: {offset}/{size} byte')
            verify_remote(drive,row)
        continuing()
        # Confirm the local identity still matches the exact file archived remotely.
        if digest(path, should_continue)[:3] != (size,md5,sha):
            raise CloudError('Video đã đổi sau upload; chưa xác minh bản hiện tại.')
        row.status, row.uploaded_bytes, row.error = 'VERIFIED',size,None
        row.verified_at = dt.datetime.now(dt.timezone.utc)
        row.encrypted_session = None
        persist(row)
        if progress_callback:
            progress_callback(100,'Đã xác minh kích thước và MD5 trên Google Drive')
        return public(row)
    except Exception as exc:
        if row is not None:
            row.status = 'PAUSED' if isinstance(exc,TaskCancelled) else 'FAILED'
            row.error = 'Đã dừng; có thể tiếp tục.' if isinstance(exc,TaskCancelled) else str(exc) if isinstance(exc,CloudError) else 'Không lưu được trạng thái hoặc xử lý tệp. Bản trên máy được giữ lại.'
            persist(row)
        if isinstance(exc,(CloudError,TaskCancelled)):
            raise
        raise CloudError('Upload chưa hoàn tất. Bản trên máy được giữ lại; kiểm tra database/ổ đĩa và thử lại.') from None


@account_operation
def remove_local(project_id, upload_id, should_continue=None, progress_callback=None):
    if settings.SERVER_MODE:
        raise CloudError('Dùng Quản lý máy chủ → Dọn dữ liệu để xem trước và khóa DELETING; không xóa trực tiếp.')
    path = delivery(project_id)
    with connection.SessionLocal() as db:
        row = db.get(CloudUpload,upload_id)
    if not row or row.project_id != project_id or row.status != 'VERIFIED':
        raise CloudError('Chưa có bản cloud đã xác minh của video này.')
    if config().get('account_id') != row.account_id:
        raise CloudError('Tài khoản Drive hiện tại khác tài khoản lưu video. Hãy kết nối đúng tài khoản.')
    size,md5,sha,stat = digest(path, should_continue)
    if (size,md5,sha) != (row.file_size,row.md5,row.sha256):
        raise CloudError('Video hiện tại khác bản cloud; không xóa.')
    verify_remote(Drive(),row)  # Recheck trash/checksum immediately before explicit deletion.
    if should_continue and not should_continue():
        raise TaskCancelled()
    current = path.stat()
    if (stat.st_size,stat.st_mtime_ns,stat.st_ino) != (current.st_size,current.st_mtime_ns,current.st_ino):
        raise CloudError('Video đã thay đổi; không xóa.')
    # Commit the cleanup intent before touching the file. If storage is already
    # unavailable, no deletion is attempted. Filesystem + DB are not one transaction.
    row.error = 'Đã yêu cầu dọn bản MP4; cần kiểm tra tệp nếu tác vụ bị gián đoạn.'
    persist(row)
    # Only this exact MP4 is removed. No recursive project/source/cache deletion.
    from src.server.backup_remote import deletion_allowed
    if not deletion_allowed(path):
        raise CloudError('Chưa có gói sao lưu hệ thống VALIDATED_PASS tại kho độc lập được kiểm tra lại; chưa cho xóa bản gốc.')
    path.unlink()
    row.local_removed = True
    row.error = None
    persist(row)
    return public(row)


def auto_archive(project_id, should_continue=None, progress_callback=None):
    """Cloud failure is explicit but must not erase a valid technical QA result."""
    try:
        if config().get('auto_upload'):
            return upload(project_id,should_continue,progress_callback)
    except TaskCancelled:
        raise
    except Exception:
        return {'status':'FAILED','message':'QA kỹ thuật đã hoàn tất nhưng lưu Drive chưa thành công. Mở Kho cloud để thử lại; video trên máy được giữ nguyên.'}
    return {'status':'DISABLED'}
