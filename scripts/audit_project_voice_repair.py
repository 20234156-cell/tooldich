"""Real project voice recovery in isolated DB/storage; never publishes user edits."""
import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('project_id')
    parser.add_argument('--voice', default='vi-VN-NamMinhNeural')
    parser.add_argument('--resume-dir', type=Path)
    args = parser.parse_args()
    from config.settings import settings
    from src.security.validators import validate_identifier
    project = validate_identifier(args.project_id, 'project')
    original_storage = settings.storage_path
    original_db = Path(settings.DATABASE_URL.removeprefix('sqlite:///')).resolve()
    def snapshot():
        with sqlite3.connect(f'file:{original_db.as_posix()}?mode=ro', uri=True) as db:
            return db.execute('select segments from transcripts where project_id=? order by id limit 1', (project,)).fetchone()[0]
    original = snapshot()
    root = args.resume_dir or Path('storage/audits')/('project_voice_repair_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    root = root.resolve()
    if not root.is_relative_to(Path('storage/audits').resolve()):
        raise ValueError('Audit storage must stay within storage/audits')
    root.mkdir(parents=True, exist_ok=bool(args.resume_dir))
    if (root/'report.json').is_file():
        (root/'report.json').rename(root/f'report.previous.{time.time_ns()}.json')
    settings.DATABASE_URL = 'sqlite:///'+(root/'audit.db').as_posix()
    settings.STORAGE_DIR = str(root/'projects')
    from src.database.connection import init_db, SessionLocal
    from src.database.models import Project
    init_db()
    from src.pipeline.orchestrator import PipelineOrchestrator
    from src.pipeline.state_machine import PipelineState, ProjectState
    from src.ocr.base import Cue
    from src.tts.cache import TTSCache
    from src.tts.normalizer_vi import normalize_vietnamese_text
    from src.tts.speech_text import spoken_text
    runner = PipelineOrchestrator()
    with SessionLocal() as db:
        resumed = db.get(Project, project) is not None
        if not resumed:
            db.add(Project(id=project, title='Isolated voice repair audit'))
            db.commit()
    cues = [Cue.model_validate(c) for c in json.loads(original)]
    if not resumed:
        runner.save_cues(project, cues)
        runner.save_state(project, PipelineState(current_state=ProjectState.TRANSLATION_REVIEWED))
    cache = TTSCache()
    copied = 0
    for cue in cues:
        key = cache.compute_key('edge', args.voice, normalize_vietnamese_text(spoken_text(cue)), rate=cue.tts_rate or '+0%')
        old = original_storage/'tts_cache'/(key+'.bin')
        if old.is_file():
            shutil.copy2(old, cache.cache_dir/old.name)
            copied += 1
    for folder in ('translation_cache', 'translation_memory'):
        old = original_storage/project/folder
        if old.is_dir():
            shutil.copytree(old, runner.get_project_dir(project)/folder, dirs_exist_ok=True)
    for relative in ('voiceover/timing_failures.json', 'source/translation_glossary.json'):
        old = original_storage/project/relative
        if old.is_file() and not (runner.get_project_dir(project)/relative).exists():
            target = runner.get_project_dir(project)/relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old, target)
    report = {'source_project': project, 'copied_cache': copied,
              'cue_snapshot_sha256': hashlib.sha256(original.encode()).hexdigest()}
    started = time.monotonic()
    def progress(percent, message):
        event = {'seconds': round(time.monotonic()-started, 2), 'progress': percent, 'message': message}
        with (root/'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(event, ensure_ascii=False)+'\n')
        print(json.dumps(event, ensure_ascii=True), flush=True)
    try:
        result = runner.run_voiceover(project, voice=args.voice, auto_fit=True,
                                      repair_existing=True, progress_callback=progress)
        report.update(passed=True, mean_drift=result['mean_drift'], max_drift=result['max_drift'])
    except Exception as exc:
        report.update(passed=False, error_type=type(exc).__name__, error_code=getattr(exc, 'code', None))
    report.update(seconds=time.monotonic()-started, user_transcript_unchanged=snapshot()==original)
    (root/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(root/'report.json'), **report}, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
