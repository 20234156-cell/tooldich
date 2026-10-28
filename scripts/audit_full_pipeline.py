"""Measure the real pipeline in a fresh, isolated project; never mutate the source project.

Fresh mode redoes OCR/translation/speech. Reviewed mode explicitly reuses the
authoritative text/timing of an already VERIFIED project, but synthesizes new
speech and encodes again. Reports never equate these two acceptance scopes.
External services are used; run outside pytest.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def source_snapshot(path, project):
    from contextlib import closing
    with closing(sqlite3.connect('file:'+path.as_posix()+'?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = {}
        for table in ['projects', 'sources', 'media', 'transcripts', 'jobs']:
            key = 'id' if table == 'projects' else 'project_id'
            rows[table] = [dict(row) for row in db.execute(f'SELECT * FROM {table} WHERE {key}=?', (project,))]
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project')
    parser.add_argument('--mode', choices=['fresh', 'reviewed'], default='fresh')
    parser.add_argument('--voice', default='vi-VN-NamMinhNeural')
    parser.add_argument('--check-source', action='store_true')
    parser.add_argument('--resume-dir',type=Path,help='Resume the isolated audit after explicit evidence review')
    parser.add_argument('--deadline', type=int, default=1800)
    args = parser.parse_args()
    from config.settings import settings
    original_db = Path(settings.DATABASE_URL.removeprefix('sqlite:///')).resolve()
    original_root = settings.storage_path.resolve()/args.project
    before = source_snapshot(original_db, args.project)
    if len(before['projects']) != 1 or not before['media']:
        raise ValueError('Source project/media not found')
    if args.mode == 'reviewed' and before['projects'][0]['status'] != 'VERIFIED':
        raise ValueError('Reviewed replay requires an already VERIFIED project')
    media = before['media'][0]
    original_video = Path(media['file_path'])
    root = (args.resume_dir or Path('storage/audits')/('full_'+args.mode+'_'+time.strftime('%Y%m%dT%H%M%S'))).resolve()
    if not root.is_relative_to(Path('storage/audits').resolve()):raise ValueError('Audit root must be under storage/audits')
    if args.resume_dir and not (root/'audit.db').is_file():raise ValueError('No isolated audit database to resume')
    root.mkdir(parents=True, exist_ok=bool(args.resume_dir))
    settings.DATABASE_URL = 'sqlite:///'+(root/'audit.db').as_posix()
    settings.STORAGE_DIR = str(root/'projects')
    settings.SERVER_MODE = True
    # Bind imports only after isolation. The production encrypted pool is read,
    # but quota/cooldown mutations are written to this audit's private copy.
    from src.database.connection import init_db, SessionLocal
    from src.database.models import Project, Source, Media, Job
    init_db()
    from src.ai.key_pool import key_pool
    key_pool.storage_dir = root/'private'; key_pool.storage_dir.mkdir(exist_ok=bool(args.resume_dir))
    key_pool.storage_file = key_pool.storage_dir/'gemini_keys.bin'
    if key_pool.storage_file.exists():key_pool.load_keys()
    from src.pipeline.orchestrator import PipelineOrchestrator
    from src.pipeline.state_machine import PipelineState, ProjectState
    from src.pipeline.attempts import artifact_path
    from src.jobs.queue import JobQueue
    from src.jobs.logger import mask_secrets
    from src.ocr.base import Cue
    from src.download.validation import validate_download
    import psutil
    project = 'audit_'+args.mode
    runner = PipelineOrchestrator()
    folder = runner.get_project_dir(project)
    (folder/'source').mkdir(exist_ok=bool(args.resume_dir))
    report = {'mode': args.mode, 'source_project':args.project, 'project':project,
              'scope':'Fresh OCR/translation/speech' if args.mode=='fresh' else 'Existing DB text/timing; new speech/render/QA',
              'passed':False, 'events':[], 'resources':{}, 'download':{'status':'NOT_REQUESTED'}}
    report_path = root/'report.json'
    if args.resume_dir:
        report=json.loads(report_path.read_text(encoding='utf-8'))
        if report['source_project']!=args.project or report['mode']!=args.mode:raise ValueError('Resume identity mismatch')
        if digest(original_video)!=report['source_sha256']:raise ValueError('Original source changed')
        report.setdefault('previous_runs',[]).append({k:report.get(k) for k in ['task','passed','elapsed_seconds','error']})
        report['passed']=False;report.pop('error',None)
    report.setdefault('code_versions',[]).append({str(path).replace('\\','/'):digest(path) for path in sorted(Path('src').rglob('*.py'))})
    lock = threading.RLock()
    def save():
        with lock:
            temporary = root/'report.writing.json'
            temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
            os.replace(temporary, report_path)
    started = time.monotonic()
    last_print = [None, 0.0]
    def progress(percent, stage):
        with lock:
            if report['events'] and (percent,stage)==(report['events'][-1]['percent'],report['events'][-1]['stage']):return
            row = {'seconds':round(time.monotonic()-started,2),'percent':percent,'stage':mask_secrets(stage)}
            report['events'].append(row);save()
        if percent != last_print[0] or time.monotonic()-last_print[1] >= 15:
            last_print[:]=[percent,time.monotonic()]
            print(json.dumps(row,ensure_ascii=True),flush=True)
    stop = threading.Event()
    def monitor():
        current=psutil.Process()
        peak=0; minimum=psutil.virtual_memory().available
        while not stop.wait(1):
            total=0
            for proc in [current,*current.children(recursive=True)]:
                try:total+=proc.memory_info().rss
                except psutil.Error:pass
            peak=max(peak,total);minimum=min(minimum,psutil.virtual_memory().available)
            with lock:report['resources'].update(peak_process_tree_rss_bytes=peak,min_system_available_bytes=minimum)
    monitoring=threading.Thread(target=monitor,daemon=True);monitoring.start()
    queue=JobQueue(1)
    try:
        report['source_sha256']=digest(original_video)
        report['source_duration']=media['duration']
        if args.check_source:
            try:
                from src.providers.bilibili import BilibiliProvider
                from src.download.manager import DownloadManager
                if before['sources'][0]['platform']!='bilibili':raise ValueError('Source check currently supports Bilibili')
                remote=BilibiliProvider().get_media(before['sources'][0]['input_url'])
                result=DownloadManager(progress_callback=progress).download(remote,root/'downloaded.mp4',validation_mode='strict',
                    should_continue=lambda:time.monotonic()-started<args.deadline)
                report['download']={'status':'COMPLETED','bytes':result.file_size_bytes,'sha256':digest(result.file_path)}
            except Exception as exc:
                report['download']={'status':'FAILED','error':mask_secrets(str(exc)),'code':getattr(exc,'code',type(exc).__name__)}
            save()
        # Always preserve the selected source/trim identity of the user's project.
        source=folder/'source/video_source.mp4'
        if not args.resume_dir:shutil.copy2(original_video,source)
        if digest(source)!=report['source_sha256']:raise ValueError('Source copy identity mismatch')
        report['source_validation']=validate_download(source,mode='strict').model_dump()
        if not args.resume_dir:
            with SessionLocal() as db:
                db.add(Project(id=project,title='Isolated acceptance '+args.mode))
                db.add(Source(project_id=project,platform='local_file',input_url=str(source),duration=media['duration']))
                db.add(Media(project_id=project,file_path=str(source),duration=media['duration'],width=media['width'],height=media['height'],is_valid=True,file_size_bytes=source.stat().st_size))
                db.add(Job(id='job_'+project,project_id=project,status='PENDING',current_stage='INIT'))
                db.commit()
            runner.save_state(project,PipelineState(current_state=ProjectState.MEDIA_READY))
            roi=json.loads((original_root/'source/roi.json').read_text(encoding='utf-8'))
            runner.save_roi_set(project,roi['rois'])
        config=original_root/'source/subtitle_settings.json'
        if config.is_file() and not args.resume_dir:
            from src.pipeline.render import RenderConfig
            initial_config=RenderConfig.model_validate(json.loads(config.read_text(encoding='utf-8')))
            (folder/'source/subtitle_settings.json').write_text(initial_config.model_dump_json(),encoding='utf-8')
        if args.mode=='reviewed' and not args.resume_dir:
            if not before['transcripts']:raise ValueError('Missing authoritative transcript')
            segments=before['transcripts'][0]['segments']
            segments=json.loads(segments) if isinstance(segments,str) else segments
            cues=[Cue.model_validate({**row,'voice_audio_path':None,'voice_duration':None}) for row in segments]
            if not cues:raise ValueError('Intentionally empty transcript cannot be replayed')
            runner.save_cues(project,cues)
            runner.save_state(project,PipelineState(current_state=ProjectState.TRANSLATION_REVIEWED))
        task=queue.submit(project,'run-all',runner.run_all,project,inject_cancel=True,
                          voice=args.voice,progress_callback=progress)
        while not task.future.done():
            if time.monotonic()-started>args.deadline:
                queue.pause(task.task_id)
            time.sleep(1)
        task.future.result()
        report['task']={'state':task.state,'error_code':task.error_code,'error_message':mask_secrets(task.error_message or ''),'error_details':task.error_details,'result':task.result}
        cues=runner.get_cues(project)
        report['cues']=[c.model_dump() for c in cues]
        report['pipeline_state']=runner.get_state(project).model_dump()
        output=artifact_path(folder,'output/video_vi.mp4')
        if output.is_file():
            report['output']={'path':str(output),'sha256':digest(output),'validation':validate_download(output,mode='strict').model_dump()}
        report['passed']=task.state=='DONE' and bool(report.get('output'))
    except Exception as exc:
        report['error']={'code':getattr(exc,'code',type(exc).__name__),'message':mask_secrets(str(exc))}
        report['passed']=False
    finally:
        queue.shutdown()
        stop.set();monitoring.join(timeout=5)
        report['elapsed_seconds']=round(time.monotonic()-started,2)
        report['total_work_seconds']=round(report['elapsed_seconds']+sum(run.get('elapsed_seconds') or 0 for run in report.get('previous_runs',[])),2)
        report['source_rows_unchanged']=source_snapshot(original_db,args.project)==before
        report['source_file_unchanged']=digest(original_video)==report.get('source_sha256')
        if not report['source_rows_unchanged'] or not report['source_file_unchanged']:report['passed']=False
        save();print(json.dumps({'passed':report['passed'],'report':str(report_path)},ensure_ascii=True),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
