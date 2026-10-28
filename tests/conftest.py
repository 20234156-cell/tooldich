import math
import struct
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Imports during pytest collection may construct ProjectManager/init_db before
# autouse fixtures run. Sandbox that bootstrap too, not only individual tests.
import os
import tempfile
_COLLECTION_SANDBOX=tempfile.TemporaryDirectory(prefix='duan_pytest_collection_')
_COLLECTION_ROOT=Path(_COLLECTION_SANDBOX.name)
os.environ['DATABASE_URL']='sqlite:///'+(_COLLECTION_ROOT/'app.db').as_posix()
os.environ['STORAGE_DIR']=str(_COLLECTION_ROOT/'projects')

from config.settings import settings
from src.database.models import Base
import src.database.connection as db_conn


@pytest.fixture(autouse=True)
def prohibit_external_network(monkeypatch):
    """Mock providers explicitly: an accidental real API call must fail a test."""
    import requests
    import httpx
    def blocked(*args, **kwargs):
        raise AssertionError("Real network is disabled in pytest; mock the provider")
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    import src.providers.douyin_browser as douyin_browser
    monkeypatch.setattr(douyin_browser, "read_video_page", blocked)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Isolate storage and database for every test."""
    test_db_path = tmp_path / "test_app.db"
    test_storage = tmp_path / "projects"
    test_storage.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite:///{test_db_path.as_posix()}")
    monkeypatch.setattr(settings, "STORAGE_DIR", str(test_storage))
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "fake-test-key")
    monkeypatch.setattr(settings, "DOUYIN_COOKIE_FILE", None)
    monkeypatch.setattr(settings, "SERVER_BACKUP_DIRECTORY", None)
    monkeypatch.setattr(settings, "SERVER_BACKUP_ROOT", None)
    monkeypatch.setattr(settings, "DOUYIN_BROWSER_PROFILE", str(tmp_path/'douyin_profile'))

    test_engine = create_engine(
        f"sqlite:///{test_db_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    test_session_local = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    monkeypatch.setattr(db_conn, "engine", test_engine)
    monkeypatch.setattr(db_conn, "SessionLocal", test_session_local)

    import src.jobs.system as sys_mod
    monkeypatch.setattr(sys_mod, "SessionLocal", test_session_local)
    import src.api.app as app_mod
    monkeypatch.setattr(app_mod, "SessionLocal", test_session_local)
    import src.pipeline.orchestrator as orchestrator_mod
    monkeypatch.setattr(orchestrator_mod, "SessionLocal", test_session_local)
    import src.jobs.queue as queue_mod
    monkeypatch.setattr(queue_mod, "SessionLocal", test_session_local)
    import src.jobs.cleanup as cleanup_mod
    monkeypatch.setattr(cleanup_mod, "SessionLocal", test_session_local)

    Base.metadata.create_all(bind=test_engine)

    yield {
        "tmp_path": tmp_path,
        "storage_dir": test_storage,
        "engine": test_engine,
        "SessionLocal": test_session_local,
    }

    Base.metadata.drop_all(bind=test_engine)
    test_engine.dispose()


@pytest.fixture
def db_session(isolated_env):
    """Provide a clean SQLAlchemy session."""
    session = isolated_env["SessionLocal"]()
    try:
        yield session
    finally:
        session.close()


class FakeGeminiClient:
    """Mock GeminiClient returning predefined responses or handling queues."""

    def __init__(self, responses: Optional[List[Union[str, Exception]]] = None):
        self.responses = list(responses or [])
        self.call_history: List[Dict[str, Any]] = []

    def complete(
        self,
        system: str,
        content: Union[str, List[Dict[str, Any]]],
        max_tokens: Optional[int] = None,
        error_code: Optional[str] = None,
    ) -> str:
        self.call_history.append({
            "system": system,
            "content": content,
            "max_tokens": max_tokens,
            "error_code": error_code,
        })
        if not self.responses:
            return '{"translations": []}'
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


@pytest.fixture
def fake_gemini():
    return FakeGeminiClient()


def generate_pcm_audio(
    duration_seconds: float,
    sample_rate: int = 24000,
    frequency: float = 440.0,
    amplitude: float = 0.3,
) -> bytes:
    """Generate 16-bit mono PCM sine wave audio."""
    num_samples = int(duration_seconds * sample_rate)
    samples = []
    for i in range(num_samples):
        if frequency <= 0:
            val = 0
        else:
            val = int(amplitude * 32767.0 * math.sin(2.0 * math.pi * frequency * i / sample_rate))
        samples.append(struct.pack("<h", max(-32768, min(32767, val))))
    return b"".join(samples)


@pytest.fixture
def pcm_generator():
    return generate_pcm_audio


def pytest_sessionfinish(session, exitstatus):
    # Release pooled Windows SQLite handles before deleting collection storage.
    db_conn.engine.dispose()
    _COLLECTION_SANDBOX.cleanup()
