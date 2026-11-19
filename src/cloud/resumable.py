"""Bounded-memory upload recovery shared by Drive and YouTube.

The server's acknowledged offset is authoritative. Never replay a timed-out
chunk until a status query resolves how much was accepted. Chunk size remains
constant for the session (YouTube requirement), except the final chunk.
"""
import datetime as dt
from email.utils import parsedate_to_datetime
import random
import time

from src.cloud.drive import CloudError, RetryableUploadError

MAX_RETRIES = 5
MAX_RECOVERY_SECONDS = 180


def retry_delay(attempt, retry_after=None):
    delay = min(32, 2 ** (attempt - 1)) + random.random()
    if retry_after is not None:
        try:
            requested = float(retry_after)
        except (ValueError, TypeError):
            try:
                requested = (parsedate_to_datetime(retry_after) - dt.datetime.now(dt.timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                requested = 0
        if requested > MAX_RECOVERY_SECONDS:
            raise CloudError('Máy chủ yêu cầu chờ lâu; phiên tải được giữ để tiếp tục sau.')
        delay = max(delay, requested)
    return delay


def _wait(seconds, check):
    end = time.monotonic() + seconds
    while True:
        check()
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(.25, remaining))


def transfer(stream, total, send, *, chunk_size, check, on_progress=None):
    """send(None, b'') queries; send(offset, bytes) uploads. Returns (offset, receipt).

    Authentication is refreshed by send/check. Session creation and final checksum
    verification stay with the provider. No persistent session URL is logged here.
    """
    if type(total) is not int or total < 0 or type(chunk_size) is not int or chunk_size <= 0:
        raise ValueError('Invalid upload dimensions')
    offset = 0
    query = True
    recovering = False
    attempted_end = total
    failures = 0
    deadline = None

    def backoff(error):
        nonlocal failures, deadline
        failures += 1
        now = time.monotonic()
        if deadline is None:
            deadline = now + MAX_RECOVERY_SECONDS
        if failures > MAX_RETRIES:
            raise CloudError('Tải lên chưa tiến triển sau các lần thử; phiên được giữ để tiếp tục.') from None
        delay = retry_delay(failures, getattr(error, 'retry_after', None))
        if now + delay >= deadline:
            raise CloudError('Hết thời gian phục hồi mạng; phiên tải được giữ để tiếp tục.') from None
        _wait(delay, check)

    while True:
        check()
        if deadline is not None and time.monotonic() >= deadline:
            raise CloudError('Hết thời gian phục hồi mạng; phiên tải được giữ để tiếp tục.')
        chunk = b''
        if not query:
            stream.seek(offset)
            wanted = min(chunk_size, total - offset)
            chunk = stream.read(wanted)
            if len(chunk) != wanted or not chunk:
                raise CloudError('Tệp bị rút ngắn trong lúc tải; chưa xác nhận thành công.')
            attempted_end = offset + len(chunk)
        try:
            acknowledged, receipt = send(None if query else offset, chunk)
        except RetryableUploadError as exc:
            # Release the chunk before waiting. No second in-flight buffer.
            del chunk
            if not query:
                recovering = True
            query = True
            backoff(exc)
            continue
        limit = attempted_end if recovering or not query else total
        if type(acknowledged) is not int or not offset <= acknowledged <= limit:
            raise CloudError('Máy chủ xác nhận phạm vi byte không hợp lệ; giữ phiên để kiểm tra.')
        if receipt is not None and acknowledged != total:
            raise CloudError('Biên nhận cuối không khớp kích thước tệp.')
        if acknowledged > offset or acknowledged == total:
            offset = acknowledged
            failures = 0
            deadline = None
            if on_progress:
                on_progress(offset)
        elif not query:
            backoff(RetryableUploadError('Máy chủ chưa xác nhận thêm byte.'))
            query = True
            recovering = True
            continue
        if offset == total:
            return offset, receipt
        query = False
        recovering = False
