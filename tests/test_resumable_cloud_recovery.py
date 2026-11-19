"""Offline failure injection: acknowledged bytes, bounded retries and memory."""
import io

import pytest

from src.cloud import resumable
from src.cloud.drive import CloudError, RetryableUploadError
from src.jobs.logger import TaskCancelled


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(resumable, '_wait', lambda seconds, check: check())


@pytest.mark.parametrize('accepted', [0, 3, 8])
def test_timeout_queries_server_before_sending_remaining_bytes(accepted):
    data = b'0123456789abcdefghijk'
    remote = bytearray()
    calls = []
    failed = False
    def send(offset, chunk):
        nonlocal failed
        calls.append((offset, len(chunk)))
        if offset is None:
            return len(remote), None
        assert offset == len(remote)
        assert chunk == data[offset:offset + len(chunk)]
        if not failed:
            failed = True
            remote.extend(chunk[:accepted])
            raise RetryableUploadError('connection lost after partial acceptance')
        remote.extend(chunk)
        return len(remote), {'id': 'receipt'} if len(remote) == len(data) else None
    progress = []
    result = resumable.transfer(io.BytesIO(data), len(data), send, chunk_size=8,
                                check=lambda: None, on_progress=progress.append)
    assert bytes(remote) == data and result == (len(data), {'id': 'receipt'})
    assert calls[:3] == [(None, 0), (0, 8), (None, 0)]
    assert calls[3][0] == accepted
    assert all(length <= 8 for _, length in calls)
    assert progress == sorted(set(progress))


def test_lost_final_response_recovers_receipt_without_reupload():
    events = iter([(0, None), RetryableUploadError('lost receipt'), (4, {'id': 'final'})])
    calls = []
    def send(offset, chunk):
        calls.append(offset)
        event = next(events)
        if isinstance(event, Exception):raise event
        return event
    assert resumable.transfer(io.BytesIO(b'data'), 4, send, chunk_size=8,
                              check=lambda: None) == (4, {'id': 'final'})
    assert calls == [None, 0, None]


@pytest.mark.parametrize('fault', ['network', 'no_progress'])
def test_recovery_has_finite_retry_budget(fault):
    calls = []
    def send(offset, chunk):
        calls.append(offset)
        if fault == 'network':raise RetryableUploadError('offline')
        return 0, None
    with pytest.raises(CloudError, match='chưa tiến triển'):
        resumable.transfer(io.BytesIO(b'data'), 4, send, chunk_size=8, check=lambda: None)
    assert len(calls) <= 2 * (resumable.MAX_RETRIES + 1)


@pytest.mark.parametrize('offset', [-1, 10, True, '3'])
def test_bad_acknowledgement_never_skips_bytes(offset):
    replies = iter([(0, None), (offset, None)])
    with pytest.raises(CloudError, match='phạm vi'):
        resumable.transfer(io.BytesIO(b'123456789'), 9, lambda *_: next(replies),
                           chunk_size=4, check=lambda: None)


def test_truncated_local_file_fails_before_sending():
    calls = []
    def send(offset, chunk):
        calls.append(offset)
        return 0, None
    with pytest.raises(CloudError, match='rút ngắn'):
        resumable.transfer(io.BytesIO(b'x'), 8, send, chunk_size=4, check=lambda: None)
    assert calls == [None]


def test_cancellation_during_recovery_prevents_any_replay(monkeypatch):
    calls = []
    def cancel():raise TaskCancelled()
    def send(offset, chunk):
        calls.append(offset)
        raise RetryableUploadError('offline')
    monkeypatch.setattr(resumable, '_wait', lambda *_: cancel())
    with pytest.raises(TaskCancelled):
        resumable.transfer(io.BytesIO(b'x'), 1, send, chunk_size=4, check=lambda: None)
    assert calls == [None]


def test_permanent_error_is_not_retried():
    calls = []
    def send(offset, chunk):
        calls.append(offset)
        raise CloudError('permission denied')
    with pytest.raises(CloudError, match='permission'):
        resumable.transfer(io.BytesIO(b'x'), 1, send, chunk_size=4, check=lambda: None)
    assert calls == [None]


def test_retry_after_is_honored_or_deferred_without_sleeping_for_hours():
    assert resumable.retry_delay(1, '20') == 20
    with pytest.raises(CloudError, match='chờ lâu'):
        resumable.retry_delay(1, '3600')


def test_large_virtual_file_reads_only_one_chunk_at_a_time():
    class VirtualFile:
        position = 0
        reads = []
        def seek(self, offset):self.position = offset
        def read(self, size):
            assert size <= 8 * 1024**2
            self.reads.append(size)
            return b'x' * size
    total = 6 * 1024**3 + 1
    chunk_size = 8 * 1024**2
    # Resume near the end of a 6 GiB file; no allocation of the preceding bytes.
    accepted = total - chunk_size - 1
    def send(offset, chunk):
        nonlocal accepted
        if offset is not None:
            assert offset == accepted
            accepted += len(chunk)
        return accepted, None
    stream = VirtualFile()
    assert resumable.transfer(stream, total, send, chunk_size=chunk_size,
                              check=lambda: None) == (total, None)
    assert stream.reads == [chunk_size, 1]
