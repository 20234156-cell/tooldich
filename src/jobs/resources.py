"""Resource interruption is distinct from user cancellation and provider failure."""
import errno
import re
import shutil
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
    if not settings.SERVER_MODE:return
    threshold=settings.MIN_FREE_DISK_START_BYTES if start else settings.MIN_FREE_DISK_RUNTIME_BYTES
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


def run_media(command, *, timeout, stage):
    import subprocess,time
    check_disk()
    proc=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',errors='replace')
    deadline=time.monotonic()+timeout
    try:
        while True:
            check_disk()
            remaining=deadline-time.monotonic()
            if remaining<=0:raise subprocess.TimeoutExpired(command,timeout)
            try:
                stdout,stderr=proc.communicate(timeout=min(.25,remaining));break
            except subprocess.TimeoutExpired:pass
        error=media_error(stderr,stage,proc.returncode)
        if proc.returncode and error:raise error
        return subprocess.CompletedProcess(command,proc.returncode,stdout,stderr)
    except BaseException:
        proc.kill();proc.communicate();raise
