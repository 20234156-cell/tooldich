"""Google Drive REST client and local encrypted configuration.

Only fixed Google endpoints are used. Provider errors never include response bodies
or request URLs, which may contain upload credentials.
"""
import base64
import json
import re
import sys
import threading
import time
from urllib.parse import urlsplit

import requests

from src.database import connection
from src.database.models import CloudConnection

SCOPE = 'https://www.googleapis.com/auth/drive.file'
API = 'https://www.googleapis.com/drive/v3'
UPLOAD = 'https://www.googleapis.com/upload/drive/v3/files'
TOKEN = 'https://oauth2.googleapis.com/token'
LOCK = threading.RLock()
_active_operations = 0


def require_idle():
    if _active_operations:
        raise CloudError('Đang có thao tác lưu/dọn cloud. Hãy đợi hoàn tất trước khi đổi kết nối.')


class CloudError(RuntimeError):
    pass


class ExpiredSession(CloudError):
    pass


def seal(value):
    if sys.platform != 'win32':
        raise CloudError('Kho cloud hiện cần Windows DPAPI để bảo vệ thông tin đăng nhập.')
    from src.ai.key_pool import _dpapi_encrypt
    return base64.b64encode(_dpapi_encrypt(json.dumps(value).encode('utf-8'))).decode('ascii')


def unseal(value):
    if sys.platform != 'win32':
        raise CloudError('Không có Windows DPAPI để mở thông tin đăng nhập cloud.')
    from src.ai.key_pool import _dpapi_decrypt
    return json.loads(_dpapi_decrypt(base64.b64decode(value)))


def config():
    with connection.SessionLocal() as db:
        row = db.get(CloudConnection, 'drive')
        return unseal(row.encrypted_data) if row else {}


def save_config(value):
    encrypted = seal(value)
    with connection.SessionLocal() as db:
        db.merge(CloudConnection(id='drive', encrypted_data=encrypted))
        db.commit()


def configure(document):
    installed = document.get('installed') if isinstance(document, dict) else None
    if not isinstance(installed, dict):
        raise CloudError('Cần tệp OAuth JSON loại Desktop app (trường installed).')
    client_id, secret = installed.get('client_id'), installed.get('client_secret')
    if not isinstance(client_id, str) or not client_id.endswith('.apps.googleusercontent.com') or not isinstance(secret, str) or not secret:
        raise CloudError('OAuth JSON thiếu client_id hoặc client_secret hợp lệ.')
    with LOCK:
        require_idle()
        old = config()
        if old.get('client_id') == client_id:
            old['client_secret'] = secret
            save_config(old)
        else:
            save_config(dict(client_id=client_id, client_secret=secret, auto_upload=False))


def request(method, url, **kwargs):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc not in {'www.googleapis.com','oauth2.googleapis.com'}:
        raise CloudError('Địa chỉ Google không hợp lệ.')
    try:
        return requests.request(method, url, timeout=(10, 90), allow_redirects=False, **kwargs)
    except requests.RequestException:
        raise CloudError('Kết nối Google bị gián đoạn. Bấm tải lại để tiếp tục an toàn.') from None


def checked(method, url, expected=(200,), **kwargs):
    with request(method, url, **kwargs) as response:
        if response.status_code not in expected:
            raise CloudError(f'Google trả HTTP {response.status_code}. Kiểm tra kết nối, quyền và dung lượng rồi thử lại.')
        try:
            return response.json()
        except ValueError:
            raise CloudError('Google trả dữ liệu không hợp lệ.') from None


def access_token():
    with LOCK:
        cfg = config()
        if not cfg.get('refresh_token'):
            raise CloudError('Chưa kết nối Google Drive. Hãy đăng nhập trước.')
        if cfg.get('expires_at', 0) < time.time() + 120:
            result = checked('POST', TOKEN, data=dict(client_id=cfg['client_id'], client_secret=cfg['client_secret'],
                refresh_token=cfg['refresh_token'], grant_type='refresh_token'))
            if not isinstance(result.get('access_token'), str):
                raise CloudError('Không lấy được quyền truy cập Drive. Hãy kết nối lại.')
            cfg.update(access_token=result['access_token'], expires_at=time.time()+int(result.get('expires_in', 3600)))
            save_config(cfg)
        return cfg['access_token']


def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', value):
        raise CloudError('Google trả mã tệp không hợp lệ.')
    return value


class Drive:
    def __init__(self):
        self.account_id = config().get('account_id')

    @property
    def headers(self):
        if config().get('account_id') != self.account_id:
            raise CloudError('Tài khoản Drive đã thay đổi. Dừng và kết nối đúng tài khoản để tiếp tục.')
        return {'Authorization': 'Bearer ' + access_token()}

    def about(self):
        return checked('GET', API+'/about', headers=self.headers, params={'fields':'user(displayName,emailAddress,permissionId),storageQuota'})

    def new_id(self):
        result = checked('GET', API+'/files/generateIds', headers=self.headers, params={'count':1,'space':'drive'})
        return valid_id(result['ids'][0])

    def metadata(self, file_id):
        return checked('GET', API+'/files/'+valid_id(file_id), headers=self.headers,
            params={'fields':'id,size,md5Checksum,trashed,parents'})

    def exists(self, file_id):
        with request('GET', API+'/files/'+valid_id(file_id), headers=self.headers, params={'fields':'id'}) as response:
            if response.status_code == 404:
                return False
            if response.status_code != 200:
                raise CloudError(f'Không kiểm tra được tệp Drive (HTTP {response.status_code}).')
            return True

    def folder(self):
        with LOCK:
            cfg = config()
            if not cfg.get('folder_id'):
                cfg['folder_id'] = self.new_id()
                save_config(cfg)  # Known ID before ambiguous folder-create response.
            folder = valid_id(cfg['folder_id'])
            with request('GET', API+'/files/'+folder, headers=self.headers, params={'fields':'id,mimeType,trashed'}) as response:
                if response.status_code == 200:
                    data = response.json()
                    if data.get('trashed') or data.get('mimeType') != 'application/vnd.google-apps.folder':
                        raise CloudError('Thư mục lưu Drive đã bị xóa hoặc đổi loại. Hãy kết nối lại.')
                    return folder
                if response.status_code != 404:
                    raise CloudError(f'Không đọc được thư mục Drive (HTTP {response.status_code}).')
            checked('POST', API+'/files', headers=self.headers, expected=(200,201),
                json={'id':folder,'name':'DuanLayLink - ThanhPham','mimeType':'application/vnd.google-apps.folder'})
            return folder

    def begin(self, row):
        headers = self.headers | {'X-Upload-Content-Type':getattr(row,'content_type','video/mp4'),'X-Upload-Content-Length':str(row.file_size)}
        with request('POST', UPLOAD, headers=headers, params={'uploadType':'resumable'},
            json={'id':row.remote_id,'name':getattr(row,'filename',f'{row.project_id}-{row.sha256[:12]}.mp4'),'parents':[self.folder()],
                  'appProperties':{'archive_id':row.id,'sha256':row.sha256}}) as response:
            if response.status_code not in (200,201):
                raise CloudError(f'Không tạo được upload Drive (HTTP {response.status_code}).')
            session = response.headers.get('Location', '')
        self.validate_session(session)
        return session

    @staticmethod
    def validate_session(session):
        url = urlsplit(session)
        if url.scheme != 'https' or url.netloc != 'www.googleapis.com' or url.path != '/upload/drive/v3/files' or not url.query:
            raise CloudError('Phiên tải Drive không hợp lệ; không gửi dữ liệu.')

    def send(self, session, total, offset=None, chunk=b''):
        self.validate_session(session)
        headers = self.headers | {'Content-Length':str(len(chunk)),
            'Content-Range':f'bytes {offset}-{offset+len(chunk)-1}/{total}' if offset is not None else f'bytes */{total}'}
        with request('PUT', session, headers=headers, data=chunk) as response:
            status = response.status_code
            if status in (404,410):
                raise ExpiredSession('Phiên tải đã hết hạn.')
            if status in (200,201):
                return total
            if status == 308:
                value = response.headers.get('Range')
                if value is None:
                    return 0
                match = re.fullmatch(r'bytes=0-(\d+)', value)
                if not match or not 0 <= int(match[1]) < total:
                    raise CloudError('Drive trả byte range không hợp lệ; giữ nguyên bản trên máy.')
                return int(match[1])+1
            raise CloudError(f'Phiên tải Drive trả HTTP {status}. Bấm tải lại; phiên hết hạn cần tạo lượt mới.')
