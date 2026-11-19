"""Explicit project-local media reclamation; metadata and database are never removed."""
from pathlib import Path
import threading

MEDIA_SUFFIXES = {'.mp4', '.m4a', '.m4s', '.wav', '.mp3', '.aac', '.flac', '.webm', '.mkv', '.mov', '.part', '.bin', '.pcm'}
PROTECTED_SUFFIXES = {'.json', '.srt', '.db', '.sqlite', '.sqlite3', '.txt', '.md', '.ass', '.vtt'}
_disk_guard_lock = threading.Lock()

def _files(proj_dir):
    supplied = Path(proj_dir)
    if supplied.is_symlink() or (hasattr(supplied, 'is_junction') and supplied.is_junction()):
        raise ValueError('Không dọn media qua liên kết thư mục.')
    root = supplied.resolve()
    for path in root.rglob('*'):
        if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root):
            # Never cross a junction/symlinked directory into another project.
            if any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction())
                   for p in path.parents if p != root and p.is_relative_to(root)):
                continue
            yield path

def _intermediate(path):
    return (path.name == '_vsf_scan_proxy.mp4' or
            any(p in {'RGBImages', 'TXTImages', 'ISAImages'} for p in path.parts) or
            ('audio_recheck' in path.parts and path.suffix.lower() in {'.wav', '.pcm'}) or
            ('voiceover' in path.parts and (path.name == 'master.wav' or path.name.startswith('clip_') and path.suffix == '.wav')))

def _heavy(path):
    return path.suffix.lower() not in PROTECTED_SUFFIXES and (
        path.suffix.lower() in MEDIA_SUFFIXES or _intermediate(path) or
        'source' in path.parts and path.suffix.lower() in {'.jpg', '.jpeg', '.png', '.webp', '.bmp'})

def disk_usage(proj_dir):
    heavy = light = 0
    for path in _files(proj_dir):
        if _heavy(path): heavy += path.stat().st_size
        elif path.suffix.lower() in {'.json', '.srt'}: light += path.stat().st_size
    fmt = lambda n: f'{n / 1024**3:.2f} GiB' if n >= 1024**3 else f'{n / 1024**2:.2f} MiB'
    return dict(heavy_media_bytes=heavy, light_metadata_bytes=light,
                heavy_media_display=fmt(heavy), light_metadata_display=fmt(light))

def cleanup_intermediate_artifacts(proj_dir):
    root = Path(proj_dir).resolve()
    return _remove(proj_dir, lambda path: 'source' not in path.relative_to(root).parts and _intermediate(path))

def release_media(proj_dir):
    return _remove(proj_dir, _heavy)

def _remove(proj_dir, select):
    removed = 0
    errors = []
    for path in list(_files(proj_dir)):
        if path.suffix.lower() in PROTECTED_SUFFIXES or not select(path): continue
        try:
            size = path.stat().st_size
            path.unlink()
            removed += size
        except OSError as exc:
            errors.append(dict(path=str(path.relative_to(proj_dir)), error=str(exc)))
    return dict(reclaimed_bytes=removed, errors=errors)


def project_root(project_id):
    """Resolve only an existing DB project; never follow project directory links."""
    from config.settings import settings
    from src.cloud.metadata_archive import project_root as checked_root
    supplied = settings.storage_path / project_id
    if supplied.is_symlink() or (hasattr(supplied, 'is_junction') and supplied.is_junction()):
        raise ValueError('Không thao tác media qua liên kết thư mục.')
    return checked_root(project_id)


def storage_free_bytes():
    import shutil
    from config.settings import settings
    # Probe the actual storage volume, including when the install lives off D:.
    path = settings.storage_path.resolve()
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free


def get_project_disk_usage(project_id):
    return disk_usage(project_root(project_id))


def media_identity(project_id):
    """Bind a destructive confirmation to the files that were actually shown."""
    import hashlib
    import json
    root = project_root(project_id)
    rows = sorted((p.relative_to(root).as_posix(), p.stat().st_size, p.stat().st_mtime_ns)
                  for p in _files(root) if _heavy(p))
    return hashlib.sha256(json.dumps(rows, separators=(',', ':')).encode()).hexdigest()


def backup_status(project_id):
    """Validate receipts against current bytes/account; light metadata isn't an MP4 backup."""
    from src.cloud import youtube_upload, metadata_archive
    def read(function):
        try:
            result = function(project_id)
            return result if result.get('matches_current') else {}
        except Exception:
            return {}
    youtube = read(youtube_upload.receipt_status)
    drive = read(metadata_archive.receipt_status)
    return dict(youtube=bool(youtube), drive=bool(drive),
                drive_mode=(drive.get('receipt') or {}).get('mode'),
                media_backup=bool(youtube or drive and
                                  (drive.get('receipt') or {}).get('mode') == 'full_delivery'))


def release_project_media(project_id, *, expected_identity=None, require_backup=False):
    """Queue worker or idle caller only; preserve SQLite, scripts, JSON and subtitles."""
    from src.jobs.queue import queue, _current_task_var
    from src.database import connection
    from src.database.models import Project
    with queue._lock:
        current = _current_task_var.get()
        if _project_busy(project_id, queue, current):
            raise ValueError('Dự án đang xử lý; chưa được giải phóng media.')
        if current is None or current.project_id != project_id:
            # Reserve this project through the real queue, not by holding its
            # GLOBAL lock while hashing/deleting gigabytes. Other projects and
            # the fast Telegram status/cancel lane must remain responsive.
            task = queue.submit(project_id, 'MEDIA_RELEASE', release_project_media, project_id,
                expected_identity=expected_identity, require_backup=require_backup, update_pipeline_job=False)
        else:
            task = None
    if task is not None:
        task.future.result()
        if task.state != 'DONE':
            raise ValueError(task.error_message or 'Chưa giải phóng được media.')
        return task.result
    root = project_root(project_id)
    if expected_identity is not None and media_identity(project_id) != expected_identity:
        raise ValueError('Media đã thay đổi; hãy yêu cầu xác nhận xóa mới.')
    if require_backup:
        with connection.SessionLocal() as db:
            project = db.get(Project, project_id)
            if not project or project.status != 'VERIFIED':
                raise ValueError('Chỉ tự dọn dự án đã VERIFIED.')
        if not backup_status(project_id)['media_backup']:
            raise ValueError('Chưa có biên nhận khớp bản MP4 đầy đủ; giữ video trên máy.')
    return dict(**release_media(root), free_bytes=storage_free_bytes())


def _project_busy(project_id, queue, current=None):
    from config.settings import settings
    from src.database import connection
    from src.database.models import Job
    active = queue.get_by_project(project_id)
    if active and active.state in {'QUEUED', 'RUNNING'} and active is not current:
        return True
    with connection.SessionLocal() as db:
        unfinished = {'PENDING', 'RESOLVING', 'DOWNLOADING', 'PROCESSING', 'TRANSCRIBING',
                      'TRANSLATING', 'DUBBING', 'EXPORTING', 'EXTRACTING_AUDIO', 'ANALYZING'}
        if db.query(Job).filter(Job.project_id == project_id, Job.status.in_(unfinished)).first():
            return True
    if settings.SERVER_MODE:
        from src.server import store
        return any(t.get('project_id') == project_id and t.get('state') in {'QUEUED', 'RUNNING'}
                   and t.get('task_id') != getattr(current, 'task_id', None)
                   for _, t in store.rows('task:'))
    return False


def auto_disk_guard_if_needed():
    """Oldest verified, idle, backed-up projects first. Never delete on receipt names alone."""
    from config.settings import settings
    from src.database import connection
    from src.database.models import Project
    from src.jobs.queue import queue
    minimum = settings.AUTO_DISK_GUARD_MIN_FREE_GB * 1024**3
    result = dict(free_bytes=storage_free_bytes(), reclaimed_bytes=0, projects=[], errors=[])
    if not settings.AUTO_DISK_GUARD_ENABLED or result['free_bytes'] >= minimum:
        return result
    with _disk_guard_lock:
        with connection.SessionLocal() as db:
            candidates = [p.id for p in db.query(Project).filter_by(status='VERIFIED')
                          .order_by(Project.created_at.asc()).all()]
        for project_id in candidates:
            if storage_free_bytes() >= minimum:
                break
            # Selection/submission is short; actual IO is serialized per project
            # by JobQueue, so a large backup check cannot freeze /status or cancel.
            with queue._lock:
                if _project_busy(project_id, queue):
                    continue
                task = queue.submit(project_id, 'AUTO_DISK_GUARD', _guard_candidate, project_id,
                                    update_pipeline_job=False)
            task.future.result()
            if task.state != 'DONE':
                result['errors'].append(project_id)
                continue
            released = task.result
            if released.get('skipped'):
                continue
            result['reclaimed_bytes'] += released['reclaimed_bytes']
            result['projects'].append(project_id)
            if released['errors']:
                result['errors'].append(project_id)
        result['free_bytes'] = storage_free_bytes()
    if result['projects']:
        from src.notifications.telegram import notify_project_event
        notify_project_event('disk_guard', 'AUTO_DISK_GUARD',
            'Đã tự dọn: ' + ', '.join(result['projects']) +
            f". Thu hồi {result['reclaimed_bytes']/1024**3:.2f} GiB; còn {result['free_bytes']/1024**3:.2f} GiB.")
    return result


def _guard_candidate(project_id):
    if get_project_disk_usage(project_id)['heavy_media_bytes'] <= 100 * 1024**2:
        return {'skipped': True}
    try:
        return release_project_media(project_id, require_backup=True)
    except ValueError:
        return {'skipped': True}


def create_telegram_demo_clip(project_id: str, max_duration: float = 120.0):
    """Encode a measured, size-checked preview. Caller must unlink path in finally."""
    import json
    import math
    import subprocess
    from src.pipeline.render import get_ffmpeg_bin, get_ffprobe_bin
    from src.pipeline.attempts import artifact_path
    from src.database import connection
    from src.database.models import Transcript
    root = project_root(project_id)
    video = artifact_path(root, 'output/video_vi.mp4')
    if not video.is_file() or video.is_symlink() or not video.resolve().is_relative_to(root):
        raise ValueError('Chưa có MP4 hợp lệ để xem Demo.')
    if not math.isfinite(max_duration) or max_duration <= 0:
        raise ValueError('Thời lượng demo không hợp lệ.')
    probe = subprocess.run([str(get_ffprobe_bin()), '-v', 'error', '-show_entries',
        'format=duration', '-of', 'json', str(video)], capture_output=True, text=True, check=True, timeout=30)
    duration = float(json.loads(probe.stdout)['format']['duration'])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Không đo được thời lượng video.')
    length = min(120., max_duration, duration)
    start = min(duration * .25, 600., duration-length) if duration > 240 else 0.
    with connection.SessionLocal() as db:
        transcript = db.query(Transcript).filter_by(project_id=project_id).first()
        # Even an empty DB transcript is authoritative.
        segments = transcript.segments if transcript else None
    if segments is None:
        timeline = artifact_path(root, 'voiceover/timeline.json')
        segments = json.loads(timeline.read_text(encoding='utf-8')) if timeline.is_file() else []
    positions = []
    for row in segments or []:
        value = row.get('actual_start', row.get('start')) if isinstance(row, dict) else None
        if type(value) in (int, float) and math.isfinite(value) and 0 <= value < duration:
            positions.append(value)
    positions.sort()
    if positions and duration > length:
        right = best = 0
        for left, anchor in enumerate(positions):
            while right < len(positions) and positions[right] < anchor+length:
                right += 1
            if right-left > best:
                best = right-left
                start = min(anchor, duration-length)
    output = root/'output/demo_preview_tele.mp4'
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_symlink() or not output.resolve().is_relative_to(root):
        raise ValueError('Đường dẫn demo không hợp lệ.')
    try:
        subprocess.run([str(get_ffmpeg_bin()), '-nostdin', '-v', 'error', '-y',
            '-ss', str(start), '-t', str(length), '-i', str(video),
            '-vf', "scale='trunc(min(1280,iw)/2)*2':-2", '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-preset', 'veryfast',
            '-crf', '28', '-maxrate', '1800k', '-bufsize', '3600k', '-c:a', 'aac',
            '-b:a', '128k', '-movflags', '+faststart', str(output)],
            capture_output=True, check=True, timeout=300)
        if not output.is_file() or not 0 < output.stat().st_size < 35 * 1024**2:
            raise ValueError('Demo vượt giới hạn 35 MiB hoặc không tạo được tệp.')
        measured = subprocess.run([str(get_ffprobe_bin()), '-v', 'error', '-show_entries',
            'format=duration:stream=codec_type,width,height', '-of', 'json', str(output)],
            capture_output=True, text=True, check=True, timeout=30)
        metadata = json.loads(measured.stdout)
        video_stream = next((s for s in metadata.get('streams', []) if s.get('codec_type') == 'video'), {})
        width, height = video_stream.get('width'), video_stream.get('height')
        measured_length = float(metadata.get('format', {}).get('duration', 0))
        if (type(width) is not int or type(height) is not int or width <= 0 or height <= 0 or
                not math.isfinite(measured_length) or measured_length <= 0):
            raise ValueError('Không kiểm định được tệp Demo đã mã hóa.')
        return dict(path=output, start_seconds=start, duration_seconds=measured_length,
                    source_duration=duration, width=width, height=height)
    except BaseException:
        output.unlink(missing_ok=True)
        raise


