"""Compare production Gemini batch sizes on authoritative cue data in isolation."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('project_id')
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--sizes', type=int, nargs='+', default=[100, 60])
args = parser.parse_args()
output = args.output.resolve()
if not output.is_relative_to(ROOT / 'storage/maintenance') or output.exists():
    parser.error('Use a new isolated directory under storage/maintenance')
if len(set(args.sizes)) != len(args.sizes) or any(not 1 <= size <= 150 for size in args.sizes):
    parser.error('Choose unique batch sizes between 1 and 150')
if not args.project_id.isalnum() and not args.project_id.replace('_', '').isalnum():
    parser.error('Invalid project ID')
output.mkdir(parents=True)
audit_db = output / 'usage.db'
os.environ['DATABASE_URL'] = 'sqlite:///' + audit_db.as_posix()
os.environ['STORAGE_DIR'] = str(output / 'projects')
os.environ['OCR_LEARNING_DIR'] = str(output / 'learning')

from config.settings import settings
from src.ai.key_pool import GeminiKeyPool, key_pool
from src.database.connection import init_db
from src.jobs.logger import PipelineException, mask_secrets
from src.ocr.base import Cue
from src.translation.batch_translator import BatchTranslator, parse_translation_response
from src.translation.memory import atomic_json

def source_snapshot():
    uri = (ROOT / 'storage/app.db').as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True) as conn:
        row = conn.execute('SELECT segments FROM transcripts WHERE project_id=?',
                           (args.project_id,)).fetchone()
    if row is None:
        raise ValueError('Project has no transcript')
    return row[0]

snapshot = source_snapshot()
cues = [Cue.model_validate(row).model_copy(update={'vi_text': None})
        for row in json.loads(snapshot)]
if not cues or not key_pool.count or settings.OFFLINE_MODE:
    raise SystemExit('Benchmark requires source cues and a configured online key pool')
init_db()
settings.TRANSLATION_AUTO_GLOSSARY = False
summary = []
for size in args.sizes:
    directory = output / str(size)
    (directory / 'source').mkdir(parents=True)
    glossary = ROOT / 'storage/projects' / args.project_id / 'source/translation_glossary.json'
    if glossary.is_file():
        shutil.copyfile(glossary, directory / 'source/translation_glossary.json')
    pool = GeminiKeyPool(directory)
    pool._keys = copy.deepcopy(key_pool._keys)
    pool.save_keys = lambda: None  # audit uses the real pool without persisting credentials
    translator = BatchTranslator(pool=pool, batch_size=size)
    translator.video_id = f'benchmark-{size}'
    inputs = [cue.model_copy(deep=True) for cue in cues]
    started = time.monotonic()
    row = {'batch_limit': size, 'cues': len(cues)}
    try:
        translated = translator.translate_cues(inputs, directory, publish_artifacts=False,
            progress_callback=lambda pct, message: atomic_json(directory / 'progress.json',
                {'progress': pct, 'message': message, 'seconds': time.monotonic()-started}))
        row.update(status='complete', valid_cues=sum(bool(c.vi_text) for c in translated))
        atomic_json(directory / 'translations.json', [c.model_dump() for c in translated])
    except PipelineException as exc:
        memory = getattr(translator, 'memory', None)
        row.update(status='failed', error_code=exc.code, error=mask_secrets(str(exc)),
                   valid_cues=sum(bool(memory.read(c.index, parse_translation_response))
                                  for c in inputs) if memory else 0)
    row['seconds'] = round(time.monotonic() - started, 3)
    with sqlite3.connect(audit_db) as conn:
        row['requests_by_status'] = conn.execute(
            'SELECT outcome,http_status,count(*) FROM api_requests WHERE video_id=? GROUP BY 1,2',
            (translator.video_id,)).fetchall()
    summary.append(row)
    atomic_json(output / 'result.json', {'project_id': args.project_id,
        'source_transcript_sha256': hashlib.sha256(snapshot.encode()).hexdigest(),
        'project_transcript_unchanged': snapshot == source_snapshot(), 'runs': summary})
    print(json.dumps(row, ensure_ascii=True), flush=True)
