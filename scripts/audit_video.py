"""Run the real queued user flow and retain a timed, resumable audit report.

External services are intentionally used here, outside pytest. No synthetic
translations, voices or pass flags are substituted for missing results.
"""
import argparse
import hashlib
import datetime
import json
import sys
import shutil
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests
from config.settings import settings
from src.jobs.logger import mask_secrets


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('project_id')
    parser.add_argument('--url', default='http://127.0.0.1:8088')
    parser.add_argument('--wait-for-ocr', action='store_true')
    parser.add_argument('--minimum-seconds', type=float, default=0)
    parser.add_argument('--started-at', help='ISO start of the actual audit window')
    parser.add_argument('--translation-provider', choices=['gemini', 'local_hymt'], default='gemini')
    parser.add_argument('--translation-model')
    parser.add_argument('--translation-batch-size',type=int)
    args = parser.parse_args()
    start = datetime.datetime.fromisoformat(args.started_at).timestamp() if args.started_at else time.time()
    directory = settings.storage_path / args.project_id / 'audit'
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / 'report.json').is_file():
        shutil.copy2(directory / 'report.json', directory / f'report.previous.{time.time_ns()}.json')
    session = requests.Session()
    if settings.API_TOKEN:
        session.headers['X-API-Token'] = settings.API_TOKEN
    root = Path(__file__).resolve().parents[1]
    code_files = sorted((root/'src').rglob('*.py')) + [root/'config/settings.py', root/'web/app.js', Path(__file__).resolve()]
    code_identity = {str(path.relative_to(root)).replace('\\', '/'): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in code_files if path.is_file()}
    report = {'project_id': args.project_id, 'started_at': start, 'passed': False, 'events': [],
              'translation_provider': args.translation_provider, 'python_version': sys.version.split()[0],
              'code_sha256_at_submission': code_identity}

    def save():
        report['elapsed_seconds'] = round(time.time()-start, 2)
        temporary = directory / 'report.tmp.json'
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(directory / 'report.json')

    def event(kind, **data):
        item = {'timestamp': time.time(), 'kind': kind, **data}
        report['events'].append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)
        save()

    def request(method, path, **kwargs):
        for attempt in range(3 if method == 'GET' else 1):
            try:
                response = session.request(method, args.url + path, timeout=60, **kwargs)
                response.raise_for_status()
                return response.json()
            except (requests.ConnectionError, requests.Timeout):
                if method != 'GET' or attempt == 2:
                    raise
                time.sleep(.5 * (attempt + 1))

    try:
        if args.wait_for_ocr:
            event('waiting_for_existing_ocr')
            while True:
                state = request('GET', f'/api/projects/{args.project_id}/state')
                if state['current_state'] in {'OCR_READY', 'SOURCE_REVIEWED', 'TRANSLATED', 'TRANSLATION_REVIEWED', 'VOICE_READY', 'RENDERED', 'VERIFIED'}:
                    break
                if state.get('stages', {}).get('OCR_READY', {}).get('status') == 'FAILED':
                    raise RuntimeError('Existing OCR failed; inspect its stage errors')
                if time.time()-start > 6*3600:
                    raise TimeoutError('OCR audit exceeded six hours')
                time.sleep(5)
        task = request('POST', f'/api/projects/{args.project_id}/run-all', json={
            'translation_provider':args.translation_provider,'translation_model':args.translation_model,
            'translation_batch_size':args.translation_batch_size})
        report['task_id'] = task['task_id']
        event('queued', task_id=task['task_id'])
        previous = None
        while True:
            task = request('GET', f"/api/tasks/{task['task_id']}")
            signature = (task['state'], task['progress'], task.get('stage'))
            if signature != previous:
                event('task', state=task['state'], progress=task['progress'], stage=task.get('stage'))
                previous = signature
            if task['state'] in {'FAILED', 'CANCELLED'}:
                report['task'] = task
                raise RuntimeError(mask_secrets(task.get('error_message') or task['state']))
            if task['state'] == 'DONE':
                report['task'] = task
                report['verification'] = task['result']['verification']
                if not report['verification']['passed']:
                    raise RuntimeError('Output QA failed')
                break
            time.sleep(3)
        # Repeated reload/read checks catch missing artifacts and state regressions
        # without spending provider quota or rendering the whole film again.
        while True:
            state = request('GET', f'/api/projects/{args.project_id}/state')
            cues = request('GET', f'/api/projects/{args.project_id}/cues')
            if state['current_state'] != 'VERIFIED' or not cues:
                raise RuntimeError('Completed project lost its verified state or cues')
            output = settings.storage_path / args.project_id / 'output/video_vi.mp4'
            if not output.is_file() or not output.stat().st_size:
                raise RuntimeError('Completed output disappeared')
            event('reload_check', cue_count=len(cues), output_bytes=output.stat().st_size)
            remaining = start + args.minimum_seconds - time.time()
            if remaining <= 0:
                break
            time.sleep(min(60, remaining))
        report['passed'] = True
        event('complete')
        return 0
    except Exception as exc:
        report['error'] = mask_secrets(str(exc))
        event('failed', error=report['error'])
        return 1
    finally:
        save()
        session.close()


if __name__ == '__main__':
    raise SystemExit(main())
