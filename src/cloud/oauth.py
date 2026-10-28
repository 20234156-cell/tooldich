"""Desktop OAuth PKCE with a short-lived private loopback callback listener."""
import base64
import hashlib
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from src.cloud.drive import CloudError, LOCK, SCOPE, TOKEN, checked, config, save_config, require_idle

_pending = False
_message = None
_attempt = None


def cancel():
    global _pending, _message, _attempt
    with LOCK:
        if _attempt is not None:
            _attempt.set()
        _attempt = None
        _pending = False
        _message = 'Đã hủy phiên đăng nhập. Bạn có thể đổi JSON hoặc kết nối lại.'
    return status()


def status():
    with LOCK:
        cfg = config()
        return dict(configured=bool(cfg.get('client_id')), connected=bool(cfg.get('refresh_token')),
            account=cfg.get('email'), display_name=cfg.get('display_name'),
            auto_upload=cfg.get('auto_upload', False), auto_delete=False,
            connecting=_pending, message=_message)


def start():
    global _pending, _message, _attempt
    with LOCK:
        require_idle()
        cfg = config()
        if not cfg.get('client_id'):
            raise CloudError('Hãy nhập tệp cấu hình OAuth Desktop app trước.')
        if _pending:
            raise CloudError('Đang chờ đăng nhập Google. Hoàn tất tab đã mở hoặc đợi tối đa 5 phút để thử lại.')
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        result = {}

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Authorization codes must never reach HTTP logs.

            def do_GET(self):
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                valid = parsed.path == '/callback' and secrets.compare_digest(query.get('state', [''])[0], state)
                if valid and not result:
                    result.update(code=query.get('code', [''])[0], denied=bool(query.get('error')))
                self.send_response(200 if valid else 400)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Cache-Control','no-store')
                self.send_header('Referrer-Policy','no-referrer')
                self.end_headers()
                self.wfile.write(('Đã nhận phản hồi Google. Bạn có thể đóng tab và quay lại ứng dụng.' if valid else 'Phản hồi không hợp lệ.').encode('utf-8'))

        server = HTTPServer(('127.0.0.1', 0), Callback)
        server.timeout = 1
        redirect = f'http://127.0.0.1:{server.server_port}/callback'
        _pending, _message = True, None
        attempt = threading.Event()
        _attempt = attempt

    def listen():
        global _pending, _message, _attempt
        try:
            deadline = time.monotonic()+300
            while not result and not attempt.is_set() and time.monotonic() < deadline:
                server.handle_request()
            if attempt.is_set():
                return
            if not result.get('code') or result.get('denied'):
                raise CloudError('Đăng nhập bị hủy hoặc hết thời gian. Hãy kết nối lại.')
            tokens = checked('POST', TOKEN, data=dict(code=result['code'], client_id=cfg['client_id'],
                client_secret=cfg['client_secret'], redirect_uri=redirect, code_verifier=verifier,
                grant_type='authorization_code'))
            if not tokens.get('refresh_token') or not tokens.get('access_token'):
                raise CloudError('Google chưa cấp quyền truy cập lâu dài. Hãy kết nối lại và đồng ý quyền Drive.')
            if SCOPE not in tokens.get('scope', '').split():
                raise CloudError('Google chưa cấp đủ quyền tệp Drive.')
            # Obtain identity BEFORE replacing the existing account configuration.
            from src.cloud.drive import API
            about = checked('GET', API+'/about', headers={'Authorization':'Bearer '+tokens['access_token']},
                params={'fields':'user(displayName,emailAddress,permissionId)'})
            user = about['user']
            if not user.get('permissionId'):
                raise CloudError('Không xác minh được tài khoản Google Drive.')
            with LOCK:
                if _attempt is not attempt or attempt.is_set():
                    return
                require_idle()
                current = config()
                if current.get('client_id') != cfg['client_id']:
                    raise CloudError('Cấu hình OAuth đã thay đổi. Hãy kết nối lại.')
                same_account = current.get('account_id') == user['permissionId']
                save_config(dict(client_id=cfg['client_id'],client_secret=cfg['client_secret'],
                    refresh_token=tokens['refresh_token'],access_token=tokens['access_token'],
                    expires_at=time.time()+int(tokens.get('expires_in',3600)),
                    account_id=user['permissionId'],email=user.get('emailAddress'),display_name=user.get('displayName'),
                    folder_id=current.get('folder_id') if same_account else None,
                    auto_upload=current.get('auto_upload',False) if same_account else False))
                _message = 'Đã kết nối Google Drive.'
        except Exception:
            with LOCK:
                if _attempt is attempt:
                    _message = 'Không hoàn tất được đăng nhập. Kiểm tra OAuth Desktop app, Drive API, test user và thử lại.'
        finally:
            server.server_close()
            with LOCK:
                if _attempt is attempt:
                    _pending = False
                    _attempt = None

    threading.Thread(target=listen, name='drive-oauth', daemon=True).start()
    return {'authorization_url':'https://accounts.google.com/o/oauth2/v2/auth?'+urlencode(dict(
        client_id=cfg['client_id'],redirect_uri=redirect,response_type='code',scope=SCOPE,
        state=state,code_challenge=challenge,code_challenge_method='S256',access_type='offline',prompt='consent'))}
