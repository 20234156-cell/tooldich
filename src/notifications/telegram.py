"""Optional best-effort notifications. Never expose provider URLs or credentials."""
import logging
import json
import re
from functools import wraps
from pathlib import Path
from typing import Optional, List
import requests
import httpx
from config.settings import settings
from src.cloud.drive import network_allowed

logger=logging.getLogger(__name__)
MAX_ATTACHMENT_BYTES=45*1024*1024
VIDEO_UPLOAD_TIMEOUT_SECONDS=180
_METHODS = frozenset({'sendMessage', 'sendDocument', 'sendVideo', 'sendPhoto', 'editMessageText', 'getUpdates', 'answerCallbackQuery', 'setMyCommands', 'setChatMenuButton'})


class _TelegramLogFilter(logging.Filter):
    def filter(self, record):
        try:
            message = record.getMessage()
            masked = re.sub(r'(https://api\.telegram\.org/bot)[^/\s\"\']+', r'\1[hidden]', message)
            if masked != message:
                record.msg, record.args = masked, ()
        except Exception:
            return False
        return True


# HTTPX logs request URLs at INFO; Telegram embeds its token in that URL.
logging.getLogger('httpx').addFilter(_TelegramLogFilter())


class TelegramRequestError(RuntimeError):
    """Only fixed messages and whitelisted metadata may escape the HTTP boundary."""
    _MESSAGES = {
        'TELEGRAM_TIMEOUT': 'Telegram chưa phản hồi trong thời gian chờ. Kiểm tra tin đã nhận trước khi gửi lại.',
        'TELEGRAM_CONNECTION_ERROR': 'Chưa kết nối được Telegram. Kiểm tra mạng rồi thử lại.',
        'TELEGRAM_RATE_LIMITED': 'Telegram đang giới hạn tần suất yêu cầu. Hãy đợi trước khi thử lại.',
        'TELEGRAM_HTTP_4XX': 'Telegram từ chối yêu cầu. Kiểm tra cấu hình và quyền của bot.',
        'TELEGRAM_HTTP_5XX': 'Dịch vụ Telegram tạm thời chưa xử lý được yêu cầu.',
        'TELEGRAM_HTTP_ERROR': 'Telegram trả về trạng thái HTTP không phù hợp.',
        'TELEGRAM_INVALID_RESPONSE': 'Telegram trả về phản hồi không hợp lệ.',
        'TELEGRAM_REJECTED': 'Telegram chưa xác nhận yêu cầu.',
        'TELEGRAM_REQUEST_FAILED': 'Kết nối Telegram gián đoạn; không hiển thị thông tin đăng nhập.',
    }
    _TRANSPORT_ERRORS = frozenset({'timeout', 'connection', 'http', 'invalid_json', 'invalid_payload', 'api_rejected', 'unexpected'})

    def __init__(self, code, *, method=None, http_status=None, transport_error=None, retry_after=None):
        self.code = code if isinstance(code, str) and code in self._MESSAGES else 'TELEGRAM_REQUEST_FAILED'
        self.details = {}
        if isinstance(method, str) and method in _METHODS:
            self.details['method'] = method
        if type(http_status) is int and 100 <= http_status <= 599:
            self.details['http_status'] = http_status
        if isinstance(transport_error, str) and transport_error in self._TRANSPORT_ERRORS:
            self.details['transport_error'] = transport_error
        if type(retry_after) is int and retry_after >= 0:
            self.details['retry_after'] = min(retry_after, 3600)
        super().__init__(self._MESSAGES[self.code])


def _rate_limit_delay(payload):
    parameters = payload.get('parameters') if isinstance(payload, dict) else None
    return parameters.get('retry_after') if isinstance(parameters, dict) else None


def enabled():
    return bool(not settings.OFFLINE_MODE and settings.TELEGRAM_NOTIFY_ENABLED and settings.TELEGRAM_BOT_TOKEN and
                settings.TELEGRAM_CHAT_ID and network_allowed())


def _send(method,data,files=None):
    return _request(method, data, files=files)


def _request(method, data, files=None, timeout=10):
    # Separate transport guard: a test overriding enabled() still cannot reach HTTP.
    from src.cloud import drive
    if not drive.network_allowed():
        raise RuntimeError('Telegram đã được cô lập.')
    if method not in _METHODS:
        raise ValueError('Phương thức Telegram không được hỗ trợ.')
    response = None
    try:
        url='https://api.telegram.org/bot'+settings.TELEGRAM_BOT_TOKEN+'/'+method
        if files:
            # HTTPX streams multipart file bodies. requests prepares one large
            # in-memory body whose write can exhaust its socket timeout.
            upload_timeout=httpx.Timeout(timeout, connect=min(timeout,10), pool=min(timeout,10))
            response=httpx.post(url,data=data,files=files,timeout=upload_timeout,follow_redirects=False)
        else:
            response=requests.post(url,data=data,timeout=timeout,allow_redirects=False)
        status = response.status_code
        if status != 200:
            if status == 429:
                try:
                    delay = _rate_limit_delay(response.json())
                except Exception:
                    delay = None
                raise TelegramRequestError('TELEGRAM_RATE_LIMITED', method=method, http_status=status,
                    transport_error='http', retry_after=delay)
            code = ('TELEGRAM_HTTP_4XX' if type(status) is int and 400 <= status < 500 else
                    'TELEGRAM_HTTP_5XX' if type(status) is int and 500 <= status < 600 else
                    'TELEGRAM_HTTP_ERROR')
            raise TelegramRequestError(code, method=method, http_status=status, transport_error='http')
        try:
            payload = response.json()
        except Exception:
            raise TelegramRequestError('TELEGRAM_INVALID_RESPONSE', method=method, http_status=status,
                transport_error='invalid_json') from None
        if not isinstance(payload, dict):
            raise TelegramRequestError('TELEGRAM_INVALID_RESPONSE', method=method, http_status=status,
                transport_error='invalid_payload')
        if payload.get('ok') is not True:
            raise TelegramRequestError('TELEGRAM_REJECTED', method=method, http_status=status,
                transport_error='api_rejected')
        return payload.get('result')
    except TelegramRequestError:
        raise
    except (requests.Timeout, httpx.TimeoutException):
        raise TelegramRequestError('TELEGRAM_TIMEOUT', method=method, transport_error='timeout') from None
    except (requests.ConnectionError, httpx.TransportError):
        raise TelegramRequestError('TELEGRAM_CONNECTION_ERROR', method=method, transport_error='connection') from None
    except Exception:
        raise TelegramRequestError('TELEGRAM_REQUEST_FAILED', method=method, transport_error='unexpected') from None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass  # Transport cleanup must not replace a sanitized exception with a URL/token.


def safe_text(text):
    from src.jobs.logger import mask_secrets
    text = mask_secrets(text)
    for secret in (settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID):
        if secret:
            text = text.replace(secret, '[ẩn]')
    return text


def notify_project_event(project_id: str,event_type: str,summary_text: str,attachments: Optional[List[Path]]=None, *, details=None):
    try:
        if not enabled():return {'status':'DISABLED'}
        from src.notifications.assistant import keyboard, project_info
        text = f'{event_type} · {project_id}\n{summary_text}'
        markup = None
        if event_type in {'OCR_REVIEW_REQUIRED', 'TIMING_REVIEW_REQUIRED', 'QA_REVIEW_REQUIRED', 'VERIFIED'}:
            details = details or {}
            if not details:
                from src.pipeline.auto_workflow import read
                details = (read(project_id) or {}).get('review', {})
            if event_type == 'OCR_REVIEW_REQUIRED':
                text += f"\nCâu OCR cần kiểm tra: {len(details.get('cue_indices', []))}."
            elif event_type == 'TIMING_REVIEW_REQUIRED':
                ids = details.get('timing_cause_cues') or details.get('timing_cues')
                text += f"\nCâu gây lệch timing: {len(ids)}." if ids is not None else '\nXem báo cáo timing trong Studio.'
            info = project_info(project_id)
            if event_type == 'VERIFIED':
                from src.pipeline.auto_workflow import content_warnings
                warnings = content_warnings(project_id)
                if warnings: text += '\n' + warnings
                duration = info.get('duration_seconds') or 0
                text += f"\nThời lượng: {duration/60:.2f} phút · MP4: {info['mp4_bytes']/1024**2:.2f} MiB."
                if duration >= settings.TELEGRAM_DEMO_TRIGGER_DURATION_SECONDS:
                    text += (f'\n📹 Đây là video dài ({duration/60:.1f} phút). Bạn có muốn xem trước '
                             'trích đoạn Demo 2 phút ngay trên Telegram trước khi duyệt đẩy lên YouTube không?')
            markup = keyboard(project_id, event_type, has_video=info['mp4_bytes'] > 0)
        if markup is None:
            from src.notifications.assistant import callback
            markup = {'inline_keyboard': [[{'text':'📋 Mở dự án · xử lý lỗi / xóa',
                                            'callback_data':callback('open',project_id)}]]}
        text = safe_text(text)
        for offset in range(0,len(text),3800):
            data = {'chat_id':settings.TELEGRAM_CHAT_ID,'text':text[offset:offset+3800]}
            # Buttons follow the final chunk so the user reads the full gate first.
            if markup and offset+3800 >= len(text):
                data['reply_markup'] = json.dumps(markup, ensure_ascii=False)
            sent = _send('sendMessage',data)
            if 'reply_markup' in data:
                bot_service.remember_keyboard(sent, markup)
        root=(settings.storage_path/project_id).resolve()
        for item in attachments or []:
            path=Path(item)
            if path.suffix.lower() not in {'.srt','.json'} or path.is_symlink() or not path.resolve().is_relative_to(root):continue
            if not path.is_file() or path.stat().st_size>=MAX_ATTACHMENT_BYTES:continue
            with path.open('rb') as stream:
                _send('sendDocument',{'chat_id':settings.TELEGRAM_CHAT_ID},files={'document':(path.name,stream)})
        return {'status':'SENT'}
    except Exception:
        logger.warning('Không gửi được thông báo Telegram; kết quả pipeline được giữ nguyên.')
        return {'status':'FAILED'}


def notify_run_all(function):
    """Keep the production signature for queue cancellation/progress injection."""
    @wraps(function)
    def wrapped(self,project_id,*args,**kwargs):
        try:
            result=function(self,project_id,*args,**kwargs)
        except Exception as exc:
            # notify_project_event is best-effort and suppresses all provider failures.
            code=getattr(exc,'code','FAILED')
            try:notify_project_event(project_id,str(code),str(exc),details=getattr(exc,'details',None))
            except Exception:logger.warning('Không gửi được thông báo dừng; giữ nguyên lỗi pipeline.')
            raise
        try:
            if enabled() and result.get('verification',{}).get('passed') is True:
                from src.cloud.youtube_upload import publishing_pack
                from src.pipeline.attempts import artifact_path
                report=result['verification']
                summary=f"QA đạt: {report.get('audio_metrics',{})}\nMax drift: {report.get('max_drift')} · Mean drift: {report.get('mean_drift')}"
                try:
                    pack=publishing_pack(project_id)
                    summary+='\n'+'\n'.join(t.get('text','') if isinstance(t,dict) else str(t) for t in pack.get('youtube_titles',[]))
                    summary+='\n'+pack.get('facebook_post','')
                except Exception:
                    summary+='\nChưa chuẩn bị được gói đăng kênh; xem dự án để tạo lại.'
                root=self.get_project_dir(project_id)
                attachments=[artifact_path(root,'voiceover/vi.dub.srt'),artifact_path(root,'output/publishing_pack.json')]
                # Reuse the existing checksum-verified metadata uploader; never claim
                # that a light pack is a backup of the MP4 itself.
                from src.notifications.assistant import cloud_status
                if cloud_status()['drive']['identity_confirmed']:
                    try:
                        from src.cloud.metadata_archive import archive_project_metadata_pack, light_receipt_status
                        if not light_receipt_status(project_id).get('matches_current'):
                            archive_project_metadata_pack(project_id)
                        summary += '\n☁️ Đã lưu hồ sơ nhẹ Drive (không gồm MP4).'
                    except Exception:
                        summary += '\nChưa lưu được hồ sơ nhẹ Drive; MP4 trên máy được giữ nguyên.'
                notify_project_event(project_id,'VERIFIED',summary,attachments)
        except Exception:
            logger.warning('Không chuẩn bị được hồ sơ thông báo; kết quả QA được giữ nguyên.')
        return result
    return wrapped


from src.notifications.telegram_bot import TelegramBotService, bot_service
