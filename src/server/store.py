import copy
import hashlib
import hmac
import secrets
import threading
import time
from contextlib import contextmanager

from fastapi import HTTPException
from src.database import connection
from src.database.models import ServerRecord

lock = threading.RLock()


@contextmanager
def transaction():
    with lock, connection.SessionLocal() as db:
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise


def get(key, db=None):
    if db is None:
        with transaction() as session:
            return get(key, session)
    row = db.get(ServerRecord, key)
    return copy.deepcopy(row.data) if row else None


def put(key, data, db=None):
    if db is None:
        with transaction() as session:
            return put(key, data, session)
    row = db.get(ServerRecord, key)
    if row is None:
        row = ServerRecord(key=key)
        db.add(row)
    row.data = copy.deepcopy(data)
    row.updated_at = time.time()


def rows(prefix, db=None):
    if db is None:
        with transaction() as session:
            return rows(prefix, session)
    return [(r.key, copy.deepcopy(r.data)) for r in db.query(ServerRecord).filter(ServerRecord.key.startswith(prefix)).all()]


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode('utf-8'), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return f'{salt}:{digest}'


def password_matches(password, encoded):
    try:
        return hmac.compare_digest(password_hash(password, encoded.split(':')[0]), encoded)
    except (ValueError, TypeError):
        return False


def user_public(user):
    return {k:user[k] for k in ('username', 'role', 'enabled')}


def save_user(username, password, role, enabled=True, first=False):
    import re
    if not re.fullmatch(r'[A-Za-z0-9_-]{3,40}', username) or role not in {'admin','operator','viewer'}:
        raise HTTPException(422, 'Tên tài khoản 3–40 ký tự chữ/số/_/-, quyền admin/operator/viewer.')
    if password and not 12 <= len(password) <= 200:
        raise HTTPException(422, 'Mật khẩu cần từ 12 đến 200 ký tự.')
    with transaction() as db:
        users = [u for _,u in rows('user:', db)]
        if first and users:
            raise HTTPException(409, 'Đã có tài khoản quản trị.')
        old = get('user:'+username, db)
        if old is None and not password:
            raise HTTPException(422, 'Cần mật khẩu cho tài khoản mới.')
        if old and old['role']=='admin' and (role!='admin' or not enabled):
            if not any(u['username']!=username and u['role']=='admin' and u['enabled'] for u in users):
                raise HTTPException(409, 'Phải giữ ít nhất một quản trị viên đang hoạt động.')
        user = dict(username=username, role=role, enabled=enabled,
                    password=password_hash(password) if password else old['password'])
        put('user:'+username, user, db)
        if old and (password or not enabled or role!=old['role']):
            for key, session in rows('session:', db):
                if session['username']==username:
                    session['expires']=0
                    put(key, session, db)
        return user_public(user)


def audit(username, action, project=None, status=None):
    with transaction() as db:
        put('audit:'+secrets.token_hex(12), dict(time=time.time(), username=username,
            action=action, project=project, status=status), db)
        records = sorted(rows('audit:', db), key=lambda r:r[1]['time'])
        for key,_ in records[:-500]:
            db.delete(db.get(ServerRecord, key))
