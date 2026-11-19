"""Resource interruption is distinct from user cancellation and provider failure."""
import errno
import re
import shutil
import subprocess
from config.settings import settings
from src.jobs.logger import PipelineException


class TaskInterrupted(PipelineException):
    def __init__(self, reason='USER_PAUSE', message='Tác vụ bị gián đoạn; dữ liệu hoàn tất được giữ lại.'):
        super().__init__(reason,message,details={'interrupted':True})
        self.reason=reason


def storage_reason(error):
    seen=set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error,OSError) and (error.errno in {errno.ENOSPC,errno.EDQUOT} or getattr(error,'winerror',None)==112):return 'LOW_DISK_SPACE'
        if isinstance(error,OSError) and error.errno==errno.EIO:return 'IO_ERROR'
        if 'database or disk is full' in str(error).lower():return 'LOW_DISK_SPACE'
        if 'disk i/o error' in str(error).lower():return 'IO_ERROR'
        error=getattr(error,'orig',None) or error.__cause__ or error.__context__
    return None


def check_disk(start=False):
    if settings.SERVER_MODE:
        threshold=settings.MIN_FREE_DISK_START_BYTES if start else settings.MIN_FREE_DISK_RUNTIME_BYTES
    elif settings.PERSONAL_MODE and settings.AUTO_DISK_GUARD_ENABLED:
        threshold=int(settings.AUTO_DISK_GUARD_MIN_FREE_GB*1024**3)
    else:return
    try:free=shutil.disk_usage(settings.storage_path).free
    except OSError as exc:raise TaskInterrupted('IO_ERROR','Không đọc được dung lượng ổ dữ liệu.') from exc
    if free<threshold:raise TaskInterrupted('LOW_DISK_SPACE','Dung lượng trống dưới ngưỡng an toàn. Giải phóng đĩa rồi kiểm tra điều kiện tiếp tục.')


def stage_status(error):
    if isinstance(error,TaskInterrupted) or storage_reason(error):return 'INTERRUPTED'
    from src.jobs.logger import TaskCancelled
    return 'CANCELLED' if isinstance(error,TaskCancelled) else 'FAILED'


def media_error(stderr,stage,exit_code=None):
    """Classify decoder evidence, never infer a timestamp not supplied by tool."""
    text=str(stderr or '')
    patterns=('invalid data found','error while decoding','error submitting packet','corrupt decoded frame','invalid nal','error decoding','moov atom not found','invalid sample rate','packet corrupt')
    if not any(term in text.lower() for term in patterns):return None
    match=re.search(r'(?:pts_time[:=]|time=)([0-9:.]+)',text)
    stream='audio' if any(label in text.lower() for label in ('[aac','[mp3','audio decoder')) else 'video' if any(label in text.lower() for label in ('[h264','[hevc','[mpeg','video decoder','invalid nal')) else 'unknown'
    details={'stage':stage,'stream':stream,'exit_code':exit_code,'stderr':text[-3000:],'timestamp':match.group(1) if match else None}
    return PipelineException('CORRUPT_MEDIA_STREAM','Phát hiện media hỏng ở công đoạn '+stage+(' tại '+match.group(1) if match else '')+'; xem log giải mã.',details=details)


def check_media_cancel(should_continue):
    if should_continue is not None and not should_continue():
        from src.jobs.logger import TaskCancelled
        raise TaskCancelled()


def run_media(command, *, timeout, stage, should_continue=None, on_poll=None):
    import time
    check_media_cancel(should_continue)
    check_disk()
    proc=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',errors='replace')
    deadline=time.monotonic()+timeout
    try:
        while True:
            check_media_cancel(should_continue)
            check_disk()
            remaining=deadline-time.monotonic()
            if remaining<=0:raise subprocess.TimeoutExpired(command,timeout)
            if on_poll:
                on_poll()
            try:
                stdout,stderr=proc.communicate(timeout=min(.25,remaining));break
            except subprocess.TimeoutExpired:pass
        check_media_cancel(should_continue)
        error=media_error(stderr,stage,proc.returncode)
        if proc.returncode and error:raise error
        return subprocess.CompletedProcess(command,proc.returncode,stdout,stderr)
    except BaseException:
        if proc.poll() is None:
            proc.kill()
        proc.communicate()
        raise


def run_checked_media(command, *, timeout, stage, should_continue=None):
    if should_continue is None:
        return subprocess.run(command, capture_output=True, text=True, check=True, timeout=timeout)
    result = run_media(command, timeout=timeout, stage=stage, should_continue=should_continue)
    result.check_returncode()
    return result


def media_timeout(fixed_timeout, duration_seconds=0):
    """Allow long media the same work budget as short media plus duration."""
    import math
    duration = float(duration_seconds or 0)
    if not math.isfinite(duration) or duration < 0:
        raise ValueError('Invalid media duration')
    return max(fixed_timeout, int(duration * 2.5) + 300)


def probe_media_duration(path):
    """Read duration metadata before scheduling a full-stream operation."""
    import json
    from pathlib import Path
    if not Path(path).is_file():
        return 0.0
    executable = shutil.which('ffprobe')
    if not executable:
        return 0.0
    result = subprocess.run(
        [executable, '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'json', str(path)], capture_output=True, text=True,
        timeout=media_timeout(settings.REQUEST_TIMEOUT_SECONDS),
    )
    try:
        return max(0.0, float(json.loads(result.stdout)['format']['duration']))
    except (ValueError, KeyError, TypeError):
        return 0.0


def check_sufficient_disk_space(target_dir, required_bytes, stage_label):
    from pathlib import Path
    target = Path(target_dir).resolve()
    while not target.exists():
        target = target.parent
    free = shutil.disk_usage(target).free
    if free < required_bytes:
        raise TaskInterrupted('LOW_DISK_SPACE',
            f'{stage_label}: cần {required_bytes / 1024**3:.2f} GiB trống, '
            f'chỉ còn {free / 1024**3:.2f} GiB tại {target}.')
