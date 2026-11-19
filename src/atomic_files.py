"""Atomic file replacement with a bounded Windows sharing retry."""
import os
import time


def replace_with_retry(source, target):
    for attempt in range(6):
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            if getattr(exc, 'winerror', None) not in (5, 32, 33) or attempt == 5:
                raise
            time.sleep(.02 * 2**attempt)
