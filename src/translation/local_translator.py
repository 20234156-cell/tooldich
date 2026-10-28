"""Explicit local translation provider; never silently replaces Gemini."""
import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import requests

from config.settings import settings
from src.jobs.logger import PipelineException, SystemErrorCodes, TaskCancelled
from src.translation.batch_translator import parse_translation_response

_LOCAL_LOCK = threading.Lock()


@contextmanager
def _runtime_slot(deadline, should_continue=None):
    # A plain `with Lock` could wait behind other projects indefinitely.
    # Waiting consumes this cue's same deadline and observes cancellation.
    while True:
        if should_continue and not should_continue():
            raise TaskCancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise requests.Timeout('Local translation deadline expired waiting for runtime')
        if _LOCAL_LOCK.acquire(timeout=min(.1, remaining)):
            break
    try:
        if should_continue and not should_continue():
            raise TaskCancelled()
        yield
    finally:
        _LOCAL_LOCK.release()


class LocalTranslator:
    name = 'local_hymt'
    version = 'HY-MT1.5-1.8B-Q4_K_M-265b2e6-single-v2'

    def __init__(self):
        self.url = (settings.LOCAL_TRANSLATION_URL or '').rstrip('/')
        parsed = urlparse(self.url)
        if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'} or parsed.username:
            raise PipelineException(SystemErrorCodes.AI_ERROR, 'Dịch cục bộ cần endpoint HTTP loopback đã cấu hình.')
        key_file = Path(settings.LOCAL_TRANSLATION_API_KEY_FILE)
        self.headers = {'Authorization': 'Bearer ' + key_file.read_text(encoding='utf-8').strip()} if key_file.is_file() else {}

    def health(self):
        response = requests.get(self.url+'/models', headers=self.headers, timeout=3, allow_redirects=False)
        response.raise_for_status()
        available = any('HY-MT1.5-1.8B' in row.get('id', '') for row in response.json().get('data', []))
        return {'available': available, 'model': self.version}

    def translate_cues(self, cues, output_dir=None, progress_callback=None, should_continue=None, tone='natural', publish_artifacts=False):
        # HY-MT is a translation model, not a reliable multi-row ID mapper.
        # One source per request prevents structurally valid shifted batches.
        total = len(cues)
        glossary_file = Path(output_dir)/'source/translation_glossary.json' if output_dir else None
        glossary = json.loads(glossary_file.read_text(encoding='utf-8')) if glossary_file and glossary_file.is_file() else {}
        with requests.Session() as session:
            session.headers.update(self.headers)
            for position, cue in enumerate(cues):
                if should_continue and not should_continue():
                    raise TaskCancelled()
                relevant = {source: target for source,target in glossary.items() if source and source in cue.effective_source_text}
                instruction = 'Translate the following segment into Vietnamese, without additional explanation. Use concise, natural subtitle language.'
                if relevant:
                    instruction += ' Use these terminology mappings: '+json.dumps(relevant,ensure_ascii=False)+'.'
                prompt = instruction+'\n\n'+cue.effective_source_text
                fingerprint = hashlib.sha256((self.version+':'+str(cue.index)+':'+tone+':'+prompt).encode()).hexdigest()
                cache = Path(output_dir)/'translation_cache'/('local_single_'+fingerprint+'.json') if output_dir else None
                rows = None
                if cache and cache.is_file():
                    try:
                        rows = parse_translation_response(cache.read_text(encoding='utf-8'), {cue.index})
                    except (ValueError, TypeError, OSError):
                        pass
                if rows is None:
                    last_error = None
                    deadline = time.monotonic()+settings.GEMINI_TOTAL_DEADLINE_SECONDS
                    for attempt in range(2):
                        result = None
                        response = None
                        if should_continue and not should_continue():
                            raise TaskCancelled()
                        remaining = deadline-time.monotonic()
                        if remaining <= 0:
                            break
                        try:
                            with _runtime_slot(deadline, should_continue):
                                remaining = deadline-time.monotonic()
                                if remaining <= 0:
                                    raise requests.Timeout('Local translation deadline expired waiting for runtime')
                                response = session.post(self.url+'/chat/completions', json={
                                    'messages': [{'role': 'user', 'content': prompt}],
                                    'temperature': .1, 'max_tokens': 500}, timeout=(5,remaining), allow_redirects=False)
                            response.raise_for_status()
                            result = response.json()
                            if 'HY-MT1.5-1.8B' not in result.get('model',''):
                                raise ValueError('Unexpected local translation model')
                            if result['choices'][0].get('finish_reason') != 'stop':
                                raise ValueError('Local translation was truncated')
                            text = result['choices'][0]['message']['content'].strip()
                            for source,target in relevant.items():
                                text = text.replace(source,target)
                            if text.startswith(('{','[')):
                                raise ValueError('Expected one plain subtitle, not an ID batch')
                            rows = parse_translation_response(json.dumps([{'id':cue.index,'translated':text}]), {cue.index})
                            break
                        except (requests.RequestException, ValueError, KeyError, TypeError, IndexError) as exc:
                            last_error = exc
                            if output_dir:
                                diagnostic = Path(output_dir)/'audit/local_translation_failures.jsonl'
                                diagnostic.parent.mkdir(parents=True,exist_ok=True)
                                with diagnostic.open('a',encoding='utf-8') as stream:
                                    stream.write(json.dumps({'timestamp':time.time(),'cue_ids':[cue.index], 'error':str(exc),
                                        'response':result['choices'][0]['message']['content'] if result and result.get('choices') else None,
                                        'http_status':response.status_code if response is not None else None,
                                        'http_error':response.text[:2000] if response is not None and response.status_code >= 400 else None},ensure_ascii=False)+'\n')
                    if rows is None:
                        raise PipelineException(SystemErrorCodes.INVALID_TRANSLATION_RESPONSE,
                            f'Local translation failed at cue {cue.index}: {last_error}') from last_error
                    if cache:
                        cache.parent.mkdir(parents=True,exist_ok=True)
                        temporary = cache.with_suffix('.tmp')
                        temporary.write_text(json.dumps(list(rows.values()),ensure_ascii=False),encoding='utf-8')
                        os.replace(temporary,cache)
                cue.vi_text = rows[cue.index]['translated'].strip()
                if progress_callback:
                    progress_callback(int((position+1)/total*100),f'Local HY-MT ({position+1}/{total} cues)')
        return cues
