"""Explicit live Gemini audit on synthetic dialogue, outside pytest/user projects."""
import json
import sys
import threading
import time
import argparse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ai.key_pool import key_pool
from src.jobs.logger import mask_secrets
from src.ocr.base import Cue
from src.translation.batch_translator import BatchTranslator
from config.settings import settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch-size', type=int, choices=[2, 35, 100, 150], default=2)
    parser.add_argument('--cue-count', type=int, default=0)
    parser.add_argument('--resume-dir', type=Path)
    parser.add_argument('--cache-only', action='store_true')
    args = parser.parse_args()
    output = args.resume_dir or Path('storage/audits') / ('translation_workers_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    if not output.resolve().is_relative_to(Path('storage/audits').resolve()):
        raise ValueError('Audit output must remain within storage/audits')
    output.mkdir(parents=True, exist_ok=bool(args.resume_dir))
    keys = key_pool.translation_keys(5)
    sources = ['你好。', '我们现在出发。', '请等一下。', '我忘了带钥匙。', '你看见我的朋友了吗？', '他在门口等你。', '今天下雨了。', '记得带伞。', '谢谢你的帮助。', '我们明天再见。'][:len(keys) * 2]
    if args.cue_count:
        if not 1 <= args.cue_count <= 300:
            raise ValueError('Audit cue count must be 1..300')
        originals = list(sources) or ['你好。']
        sources = [f'第{i+1}次，张三说：' + originals[i % len(originals)] for i in range(args.cue_count)]
    cues = [Cue(index=i + 1, start=i * 3, end=i * 3 + 3, text=text) for i, text in enumerate(sources)]
    translator = BatchTranslator(batch_size=args.batch_size)
    pristine = [c.model_copy(deep=True) for c in cues]
    original = translator._call_gemini_with_rotation
    events, lock = [], threading.Lock()
    active, peak = set(), [0]
    def measured(*args, **kwargs):
        if parser_args_cache_only:
            raise AssertionError('Cache-only audit attempted a provider request')
        key = getattr(translator._worker, 'key', None)
        identity = key.id if key else 'none'
        start = time.monotonic()
        with lock:
            active.add(identity)
            peak[0] = max(peak[0], len(active))
        status = 'failed'
        try:
            result = original(*args, **kwargs)
            status = 'success'
            return result
        finally:
            with lock:
                active.discard(identity)
                events.append({'key_id': identity, 'elapsed_s': round(time.monotonic() - start, 3), 'status': status})
    parser_args_cache_only = args.cache_only
    translator._call_gemini_with_rotation = measured
    report = {'configured_keys': key_pool.count, 'worker_keys': len(keys), 'batch_size': args.batch_size,
              'cue_count':len(cues), 'production_default_batch_size': settings.TRANSLATION_BATCH_SIZE,
              'requested_model':translator.model,
              'scope': 'Live synthetic dialogue API/ID/cache check; not full-film throughput or semantic certification'}
    started = time.monotonic()
    try:
        if not keys:
            raise RuntimeError('No usable keys configured')
        translator.translate_cues(cues, output, publish_artifacts=False)
        report.update(passed=True, translations=[{'id': c.index, 'source': c.text, 'translated': c.vi_text} for c in cues])
        warm = BatchTranslator(batch_size=args.batch_size)
        def forbidden(*args, **kwargs):
            raise AssertionError('Warm matching cache attempted a provider request')
        warm._call_gemini_with_rotation = forbidden
        warm.translate_cues(pristine, output, publish_artifacts=False)
        report['warm_cache_no_provider'] = [c.vi_text for c in pristine] == [c.vi_text for c in cues]
        if not report['warm_cache_no_provider']:
            raise AssertionError('Warm cache changed translations')
    except Exception as exc:
        report.update(passed=False, error_type=type(exc).__name__, error=mask_secrets(str(exc))[:1500])
    report.update(elapsed_s=round(time.monotonic() - started, 3), peak_active_key_calls=peak[0], events=events, models=translator.used_models,
                  warnings=translator.warnings, degraded=translator.degraded, repaired_cue_ids=sorted(translator.repaired_ids))
    path = output / ('resume_report.json' if args.resume_dir else 'report.json')
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(path), 'passed': report['passed'], 'worker_keys': len(keys), 'peak_active_key_calls': peak[0], 'elapsed_s': report['elapsed_s']}, ensure_ascii=True))


if __name__ == '__main__':
    main()
