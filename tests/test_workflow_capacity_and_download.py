"""Offline regressions for capacity planning and bounded scheduling."""
import concurrent.futures
import threading
from unittest.mock import MagicMock

import pytest

from src.pipeline.capacity import estimate_remaining, GIB


def test_settings_repr_does_not_expose_download_credentials(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings,'BILIBILI_COOKIE','test-private-cookie')
    monkeypatch.setattr(settings,'BILIBILI_SESSDATA','test-private-session')
    assert 'test-private-cookie' not in repr(settings)
    assert 'test-private-session' not in repr(settings)


def test_capacity_accounts_for_long_voice_and_render_generations():
    short = estimate_remaining(60, 20*1024**2, profile='compact', audio_mode='mix')
    long = estimate_remaining(6*3600, 10*GIB, profile='compact', audio_mode='mix')
    subtitles = estimate_remaining(6*3600, 10*GIB, profile='compact', audio_mode='original_only')
    assert short['required_free_bytes'] < 9*GIB
    assert long['required_free_bytes'] > 30*GIB
    assert long['required_free_bytes'] > subtitles['required_free_bytes']
    assert long['render_bytes'] >= 20*GIB
    assert estimate_remaining(3600, GIB, profile='quality')['required_free_bytes'] > estimate_remaining(3600,GIB,profile='compact')['required_free_bytes']


def test_capacity_stops_before_work_without_deleting(monkeypatch,tmp_path):
    from src.pipeline import capacity
    from src.jobs.logger import PipelineException
    video=tmp_path/'source.mp4';video.write_bytes(b'original')
    monkeypatch.setattr(capacity,'project_capacity',lambda *a,**k:dict(sufficient=False,required_free_bytes=40*GIB,free_bytes=30*GIB))
    with pytest.raises(PipelineException,match='40.0 GiB') as error:capacity.require_capacity('p')
    assert error.value.code=='LOW_DISK_SPACE'
    assert video.read_bytes()==b'original'


def test_capacity_uses_real_media_and_current_free_space(db_session,monkeypatch,tmp_path):
    from src.database.models import Project,Media
    from src.pipeline.capacity import project_capacity
    video=tmp_path/'source.mp4';video.write_bytes(b'media')
    db_session.add(Project(id='capacity'))
    db_session.add(Media(project_id='capacity',file_path=str(video),duration=21600))
    db_session.commit()
    monkeypatch.setattr('src.pipeline.media_storage.storage_free_bytes',lambda:20*GIB)
    result=project_capacity('capacity',profile='compact',audio_mode='mix')
    assert result['estimated'] and not result['sufficient']
    assert result['free_bytes']==20*GIB


def test_personal_disk_guard_interrupts_without_deleting(monkeypatch):
    from config.settings import settings
    from src.jobs.resources import check_disk,TaskInterrupted
    from types import SimpleNamespace
    monkeypatch.setattr(settings,'SERVER_MODE',False)
    monkeypatch.setattr(settings,'PERSONAL_MODE',True)
    monkeypatch.setattr(settings,'AUTO_DISK_GUARD_ENABLED',True)
    monkeypatch.setattr(settings,'AUTO_DISK_GUARD_MIN_FREE_GB',8)
    monkeypatch.setattr('src.jobs.resources.shutil.disk_usage',lambda _:SimpleNamespace(free=7*GIB))
    with pytest.raises(TaskInterrupted) as error:check_disk()
    assert error.value.reason=='LOW_DISK_SPACE'
    monkeypatch.setattr(settings,'AUTO_DISK_GUARD_ENABLED',False)
    check_disk()


def test_range_does_not_enqueue_entire_large_file(monkeypatch,tmp_path):
    from src.download.manager import DownloadManager
    from src.download.range_downloader import AdaptiveRangeDownloader
    real_executor=concurrent.futures.ThreadPoolExecutor
    gate=threading.Event();started=threading.Event();submitted=[];errors=[]
    data=b'abcdefgh'*64
    class ObservedExecutor(real_executor):
        def submit(self,*args,**kwargs):
            submitted.append(1)
            return super().submit(*args,**kwargs)
    monkeypatch.setattr(concurrent.futures,'ThreadPoolExecutor',ObservedExecutor)
    def get(url,headers,**kwargs):
        started.set()
        assert gate.wait(20)
        a,b=map(int,headers['Range'][6:].split('-'))
        r=MagicMock(status_code=206)
        r.headers={'Content-Range':f'bytes {a}-{b}/{len(data)}'}
        r.iter_content.return_value=[data[a:b+1]]
        return r
    monkeypatch.setattr(DownloadManager,'_get',staticmethod(get))
    def work():
        try:
            assert AdaptiveRangeDownloader(initial_chunk_size=8,min_chunk_size=4).download_range(
                'https://example.org/video',{},len(data),tmp_path/'video.part',tmp_path/'video.src')
        except BaseException as exc:errors.append(exc)
    worker=threading.Thread(target=work);worker.start()
    try:
        assert started.wait(10)
        # Old implementation queued all 64 chunks while the first request waited.
        import time
        time.sleep(.1)
        assert len(submitted)<=4
    finally:
        gate.set();worker.join(30)
    assert not worker.is_alive() and not errors
    assert (tmp_path/'video.part').read_bytes()==data


def test_personal_heavy_tasks_wait_but_download_can_run(monkeypatch,tmp_path):
    from config.settings import settings
    from src.jobs.queue import JobQueue
    monkeypatch.setattr(settings,'SERVER_MODE',False)
    monkeypatch.setattr(settings,'PIPELINE_SERIALIZE_HEAVY_TASKS',True)
    monkeypatch.setattr(settings,'STORAGE_DIR',str(tmp_path))
    queue=JobQueue(max_workers=3)
    first_started=threading.Event();release=threading.Event();second_started=threading.Event()
    def first():first_started.set();release.wait(20)
    try:
        a=queue.submit('one','run-all',first)
        assert first_started.wait(10)
        b=queue.submit('two','ocr',lambda:second_started.set())
        c=queue.submit('three','download',lambda:'downloaded')
        assert c.future.result(timeout=10)=='downloaded' or c.result=='downloaded'
        assert not second_started.is_set()
        release.set();a.future.result(timeout=10);b.future.result(timeout=10)
        assert second_started.is_set()
    finally:
        release.set();queue._executor.shutdown(wait=True)
