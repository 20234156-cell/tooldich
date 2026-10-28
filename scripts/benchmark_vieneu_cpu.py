"""Reusable real-voice CPU measurement. Never writes user projects or .env."""
import argparse
import io
import json
import platform
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import psutil
import soundfile as sf

from config.settings import settings
from src.tts.vieneu import VieNeuTTSProvider, VOICE_ID

TEXTS = [
    'Đừng vội bỏ cuộc.',
    'Ngày mười hai tháng ba, anh ấy trở về quê để gặp lại gia đình sau nhiều năm xa cách.',
    'Mọi người đều nghĩ rằng cuộc hành trình đã kết thúc. Nhưng phía sau cánh cửa ấy vẫn còn một bí mật chưa được giải đáp. Cô quyết định quay lại, tìm hiểu sự thật và bảo vệ những người mình yêu thương.',
]


def busy_reason():
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.pid != psutil.Process().pid and any(
                Path(arg).name == 'vieneu_worker.py' for arg in proc.info['cmdline'] or []
            ):
                return 'Another VieNeu worker exists; finish the active job and wait for idle release.'
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    if psutil.virtual_memory().available < 2 * 1024**3:
        return 'Less than 2 GiB available RAM; close unused applications before measuring.'
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('storage/audits') / time.strftime('vieneu_cpu_%Y%m%dT%H%M%S'))
    parser.add_argument('--repeats', type=int, default=2)
    args = parser.parse_args()
    if args.repeats < 2:
        parser.error('Use at least two repetitions per text length.')
    # A new directory prevents accidental overwrites of earlier measurements.
    args.output.mkdir(parents=True, exist_ok=False)
    report = {'machine': platform.platform(), 'cpu': platform.processor(),
              'logical_cpus': psutil.cpu_count(), 'physical_cpus': psutil.cpu_count(logical=False),
              'ram_bytes': psutil.virtual_memory().total, 'voice': VOICE_ID,
              'status': 'running', 'configurations': []}
    original = settings.VIENEU_CPU_THREADS
    provider = VieNeuTTSProvider()
    try:
        reason = busy_reason()
        if reason:
            report.update(status='deferred', reason=reason)
            return 2
        report['model_identity'] = provider.cache_version
        candidates = sorted(set([1, 2, min(4, psutil.cpu_count(logical=False) or 2)]))
        for threads in candidates:
            provider.close()
            settings.VIENEU_CPU_THREADS = threads
            group = {'threads': threads, 'samples': []}
            report['configurations'].append(group)
            started = time.perf_counter()
            provider.synthesize(TEXTS[0], VOICE_ID)
            group['cold_start_seconds'] = time.perf_counter() - started
            for repeat in range(args.repeats):
                for index, text in enumerate(TEXTS):
                    started = time.perf_counter()
                    payload, duration = provider.synthesize(text, VOICE_ID)
                    elapsed = time.perf_counter() - started
                    audio, sr = sf.read(io.BytesIO(payload), dtype='float32')
                    if not audio.size or not np.isfinite(audio).all() or float(np.max(np.abs(audio))) < 1e-5:
                        raise ValueError('Invalid measured audio')
                    name = f'threads{threads}_repeat{repeat}_text{index}.wav'
                    (args.output/name).write_bytes(payload)
                    group['samples'].append({'text': text, 'seconds': elapsed, 'audio_seconds': duration,
                                             'sample_rate': sr, 'wav': name})
                    print(f'threads={threads} repeat={repeat} text={index} seconds={elapsed:.3f}', flush=True)
            group['median_round_seconds'] = statistics.median(
                sum(item['seconds'] for item in group['samples'][i:i+len(TEXTS)])
                for i in range(0, len(group['samples']), len(TEXTS)))
        best = min(report['configurations'], key=lambda group: group['median_round_seconds'])
        report.update(status='measured', suggested_threads=best['threads'],
                      listening_review_required=True, settings_applied=False)
        return 0
    except Exception as exc:
        report.update(status='failed', error=type(exc).__name__ + ': ' + str(exc))
        return 1
    finally:
        if provider._timer:
            provider._timer.cancel()
        provider.close()
        settings.VIENEU_CPU_THREADS = original
        (args.output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'status': report['status'], 'report': str(args.output/'report.json')}, ensure_ascii=True))


if __name__ == '__main__':
    raise SystemExit(main())
