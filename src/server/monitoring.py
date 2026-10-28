"""Bounded, read-only host measurements for the administrator console."""
import csv
import io
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

import psutil

from config.settings import settings

STARTED_AT = psutil.Process().create_time()
_gpu_lock = threading.Lock()
_gpu_cache = (0, None)


def gpu_status():
    global _gpu_cache
    with _gpu_lock:
        if time.monotonic() - _gpu_cache[0] < 15:
            return _gpu_cache[1]
        value = dict(available=False, devices=[], message='Không đọc được NVIDIA GPU; chưa có số liệu.')
        try:
            result = subprocess.run(
                ['nvidia-smi', '--query-gpu=name,memory.total,memory.used,utilization.gpu', '--format=csv,noheader,nounits'],
                capture_output=True, text=True, timeout=2,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode == 0:
                devices = []
                for row in csv.reader(io.StringIO(result.stdout)):
                    name, total, used, utilization = [cell.strip() for cell in row]
                    devices.append(dict(name=name, total_mb=float(total), used_mb=float(used), utilization_percent=float(utilization)))
                if devices:
                    value = dict(available=True, devices=devices, message=None)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
        _gpu_cache = (time.monotonic(), value)
        return value


def host_status():
    memory = psutil.virtual_memory()
    paths = [('Dữ liệu dự án', settings.storage_path)]
    for label, configured in [('Snapshot sao lưu', settings.SERVER_BACKUP_ROOT), ('Kho sao lưu', settings.SERVER_BACKUP_DIRECTORY)]:
        if configured:
            paths.append((label, Path(configured)))
    # Include the application/system volume as well as each configured data volume.
    paths.append(('Ứng dụng', Path(__file__).resolve()))
    disks = {}
    for label, path in paths:
        path = Path(path).resolve()
        root = Path(path.anchor)
        key = os.path.normcase(str(root))
        if key in disks:
            disks[key]['purposes'].append(label)
            continue
        try:
            usage = shutil.disk_usage(root)
            disks[key] = dict(root=str(root), purposes=[label], total_bytes=usage.total,
                              free_bytes=usage.free, used_percent=round(100 * usage.used / usage.total, 1), error=None)
        except OSError:
            disks[key] = dict(root=str(root), purposes=[label], total_bytes=None, free_bytes=None,
                              used_percent=None, error='Không đọc được ổ đĩa.')
    alerts = []
    if memory.available < 2 * 1024**3:
        alerts.append(dict(section='tasks', message='RAM trống dưới 2 GB. Hạn chế chạy thêm ứng dụng AI hoặc tác vụ nặng.'))
    for disk in disks.values():
        if disk['error']:
            alerts.append(dict(section='storage', message=disk['root'] + ': ' + disk['error']))
        elif disk['free_bytes'] < max(settings.MIN_FREE_DISK_START_BYTES, 10 * 1024**3):
            alerts.append(dict(section='cleanup', message=f"Ổ {disk['root']} còn dưới ngưỡng dung lượng cảnh báo. Xem trước dữ liệu có thể dọn."))
    return dict(measured_at=time.time(), uptime_seconds=max(0, time.time()-STARTED_AT),
                cpu_percent=psutil.cpu_percent(interval=0.1), cpu_threads=psutil.cpu_count(),
                ram=dict(total_bytes=memory.total, available_bytes=memory.available, used_percent=memory.percent),
                disks=list(disks.values()), gpu=gpu_status(), alerts=alerts,
                heavy_task_limit=1)
