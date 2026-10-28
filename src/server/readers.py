"""Single-backend reader leases coordinated with durable deletion tombstones."""
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
import threading

from fastapi import HTTPException
from config.settings import settings
from src.database.models import FileLifecycle
from src.server import store

lock=threading.RLock()
active=Counter()


def overlaps(a,b):
    a,b=Path(a),Path(b)
    return a.is_relative_to(b) or b.is_relative_to(a)


def assert_available(path,db=None):
    if not settings.SERVER_MODE:return
    path=Path(path).resolve()
    if db is None:
        with store.transaction() as session:return assert_available(path,session)
    for row in db.query(FileLifecycle).filter(FileLifecycle.state.in_(['DELETING','DELETED'])):
        target=Path(row.path)
        if path.is_relative_to(target) or (row.state=='DELETING' and target.is_relative_to(path)):
            raise HTTPException(409 if row.state=='DELETING' else 404,'Tệp đang được dọn hoặc đã được xóa theo biên bản.')


@contextmanager
def read_guard(path):
    if not settings.SERVER_MODE:
        yield;return
    key=str(Path(path).resolve())
    # Global lock order: store, registry. Writers already holding store may nest.
    with store.lock,lock:
        assert_available(key)
        active[key]+=1
    try:yield
    finally:
        with lock:
            active[key]-=1
            if not active[key]:del active[key]


def has_readers(path):
    with lock:return any(count and overlaps(path,key) for key,count in active.items())


class ReadMiddleware:
    """Acquire before route/stat/open; release only after final ASGI body/disconnect."""
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        import re
        if scope['type']!='http' or not settings.SERVER_MODE:
            return await self.app(scope,receive,send)
        match=re.match(r'^/api/(?:cloud/)?projects/([A-Za-z0-9_-]{1,64})(?:/|$)',scope['path'])
        if not match or match[1]=='batch':return await self.app(scope,receive,send)
        # A pathsend hand-off can outlive the response callable. Stream bytes
        # through ASGI so the lease spans every read/send and disconnect.
        scope=dict(scope);scope['extensions']={key:value for key,value in scope.get('extensions',{}).items() if key!='http.response.pathsend'}
        guard=read_guard(settings.storage_path/match[1])
        try:guard.__enter__()
        except HTTPException as exc:
            from fastapi.responses import JSONResponse
            return await JSONResponse({'message':exc.detail},status_code=exc.status_code)(scope,receive,send)
        try:return await self.app(scope,receive,send)
        finally:guard.__exit__(None,None,None)
