"""Real isolated API/SQLite/FFmpeg/Gemini/Edge/browser matrix, outside pytest.

Synthetic source footage is explicit. Provider replies, speech, encodes and QA
are real. User projects, the running user API, Drive and credentials are untouched.
"""
import argparse
import hashlib
import json
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def db_snapshot(path):
    with sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True) as db:
        rows = db.execute('SELECT project_id, segments FROM transcripts ORDER BY id').fetchall()
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cases', nargs='+', choices=['normal', 'dense', 'original'], default=['normal', 'dense', 'original'])
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--resume-dir', type=Path)
    args = parser.parse_args()
    from config.settings import settings
    original_db = Path(settings.DATABASE_URL.removeprefix('sqlite:///')).resolve()
    original_snapshot = db_snapshot(original_db)
    root = (args.resume_dir or Path('storage/audits') / ('system_matrix_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))).resolve()
    if not root.is_relative_to(Path('storage/audits').resolve()):
        raise ValueError('Audit must remain inside storage/audits')
    root.mkdir(parents=True, exist_ok=bool(args.resume_dir))
    if (root/'report.json').is_file():
        (root/'report.json').rename(root/f'report.previous.{time.time_ns()}.json')
    settings.DATABASE_URL = 'sqlite:///' + (root/'audit.db').as_posix()
    settings.STORAGE_DIR = str(root/'projects')
    settings.API_TOKEN = ''
    # All imports that bind database/storage come after isolation.
    from src.database.connection import init_db, SessionLocal
    from src.database.models import Project, Source, Media, Job, APIRequest
    init_db()
    from src.api.app import app, orchestrator, queue
    from src.ocr.base import Cue
    from src.pipeline.render import get_ffmpeg_bin, get_ffprobe_bin, cues_to_ass, RenderConfig
    from src.pipeline.roi import ROIBox
    from src.pipeline.state_machine import PipelineState, ProjectState
    from src.tts.edge import EdgeTTSProvider
    from src.translation.batch_translator import BatchTranslator
    import requests
    import uvicorn

    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, lifespan='off', log_level='error', access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(.1)
    endpoint = f'http://127.0.0.1:{port}'
    session = requests.Session()
    report = {'root':str(root), 'cases':[], 'events':[], 'edge_requests':[], 'browser':None,
              'scope':'Synthetic source clips; real local API/queue/DB, OCR (normal case), Gemini/Edge, FFmpeg encode/audio QA and Chromium. Not a full-film semantic/performance or live download/STT/Drive certification.'}
    lock = threading.Lock()
    original_edge = EdgeTTSProvider._synthesize_async
    async def measured_edge(provider, *values, **options):
        started = time.monotonic()
        outcome = 'success'
        try:
            return await original_edge(provider, *values, **options)
        except Exception as exc:
            outcome = type(exc).__name__
            raise
        finally:
            with lock:
                report['edge_requests'].append({'voice':values[1], 'boundary':values[4],
                    'outcome':outcome, 'seconds':round(time.monotonic()-started,3)})
    def save():
        (root/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    def event(case, **values):
        item = dict(case=case, **values)
        report['events'].append(item)
        print(json.dumps(item, ensure_ascii=True), flush=True)
        save()
    def request(method, path, **kwargs):
        response = session.request(method, endpoint+path, timeout=20, **kwargs)
        response.raise_for_status()
        return response.json()
    def run_task(case, project):
        task = request('POST', f'/api/projects/{project}/run-all', json={'voice':'vi-VN-HoaiMyNeural', 'rate':'+0%', 'fit_to_video':True, 'shorten_to_fit':True})
        deadline = time.monotonic()+600
        previous = None
        while True:
            current = request('GET', '/api/tasks/'+task['task_id'])
            signature = (current['state'], current['progress'], current['stage'])
            if signature != previous:
                event(case, task_id=task['task_id'], state=current['state'], progress=current['progress'], stage=current['stage'])
                previous = signature
            if current['state'] not in ('RUNNING', 'QUEUED'):
                return current
            if time.monotonic() > deadline:
                queue.cancel(task['task_id'])
                raise TimeoutError('Audit task deadline')
            time.sleep(1)
    try:
        with patch.object(EdgeTTSProvider, '_synthesize_async', new=measured_edge):
            for case in args.cases:
                project = 'audit_'+case
                directory = settings.storage_path/project
                (directory/'source').mkdir(parents=True, exist_ok=bool(args.resume_dir))
                fixture = ([Cue(index=11,start=1,end=2.4,text='请马上过来'), Cue(index=98,start=7,end=10.8,text='明天再见')]
                           if case == 'dense' else
                           [Cue(index=11,start=1,end=4.8,text='你好，我们明天再见'), Cue(index=98,start=7,end=10.8,text='谢谢你的帮助')])
                source = directory/'source/video_source.mp4'
                ass = directory/'source/ground_truth.ass'
                source_cues = [c.model_copy(update={'vi_text':c.text}) for c in fixture]
                ass.write_text(cues_to_ass(source_cues, 640, 360, RenderConfig(font_name='Microsoft YaHei',font_size=28,style='bilingual',margin_v=24), 288), encoding='utf-8')
                escaped = str(ass).replace('\\','/').replace(':','\\:')
                if not source.is_file():
                    subprocess.run([str(get_ffmpeg_bin()),'-v','error','-y','-f','lavfi','-i','testsrc2=s=640x360:r=25:d=13',
                        '-f','lavfi','-i','sine=frequency=220:sample_rate=48000:duration=13',
                        '-vf',f"drawbox=x=0:y=280:w=640:h=80:color=black:t=fill,subtitles='{escaped}'",'-af','volume=0.03',
                        '-c:v','libx264','-preset','veryfast','-c:a','aac',str(source)],check=True,capture_output=True,timeout=60)
                with SessionLocal() as db:
                    resumed = db.get(Project,project) is not None
                    if not resumed:
                        db.add(Project(id=project,title='System audit '+case))
                        db.add(Source(project_id=project,platform='local_file',input_url='Synthetic audit fixture'))
                        db.add(Media(project_id=project,file_path=str(source),duration=13,width=640,height=360,is_valid=True,file_size_bytes=source.stat().st_size))
                        db.add(Job(id='job_'+case,project_id=project,status='DOWNLOADED',current_stage='DOWNLOADED'))
                        db.commit()
                if not resumed:
                    state = PipelineState(current_state=ProjectState.MEDIA_READY)
                    state.set_stage_status('MEDIA_READY','COMPLETED',progress=100,artifacts={'video':'source/video_source.mp4'})
                    orchestrator.save_state(project,state)
                    orchestrator.save_roi_set(project,[ROIBox(x=.02,y=.78,width=.96,height=.21).model_dump()])
                if case != 'normal' and not resumed:
                    translations = (['Tôi muốn nhờ anh hãy đến chỗ tôi ngay bây giờ nhé', 'Tạm biệt, hẹn gặp lại vào ngày mai']
                                    if case == 'dense' else ['Xin chào, hẹn gặp lại ngày mai', 'Cảm ơn bạn đã giúp đỡ'])
                    prepared = [c.model_copy(update={'vi_text':vi,'corrected_text':c.text,'confidence':100,
                                'translation_origin':'ai' if case == 'dense' else 'manual'}) for c,vi in zip(fixture,translations)]
                    orchestrator.save_cues(project,prepared)
                    state = PipelineState(current_state=ProjectState.TRANSLATION_REVIEWED)
                    state.set_stage_status('TRANSLATED','COMPLETED',progress=100,engine_version='controlled-fixture-draft' if case == 'dense' else 'manual-ground-truth')
                    orchestrator.save_state(project,state)
                    request('POST',f'/api/projects/{project}/subtitle-settings', json=RenderConfig(audio_mode='original_only' if case == 'original' else 'mix').model_dump())
                row = {'case':case,'project_id':project,'source_sha256':digest(source), 'controlled_input':case != 'normal', 'resumed':resumed}
                before_requests = len(report['edge_requests'])
                started = time.monotonic()
                try:
                    task = run_task(case,project)
                    row.update(task=task, seconds=round(time.monotonic()-started,3), passed=task['state']=='DONE')
                    cues = orchestrator.get_cues(project)
                    row['cues'] = [c.model_dump() for c in cues]
                    row['shortening_plans'] = [str(p.relative_to(root)) for p in (directory/'audit').glob('timing_shortening_*.json')]
                    if case == 'normal':
                        cn = lambda text: ''.join(re.findall(r'[\u3400-\u9fff]',text))
                        row['ocr_matches_ground_truth'] = [cn(c.effective_source_text) for c in cues] == [cn(c.text) for c in fixture]
                        row['passed'] = row['passed'] and row['ocr_matches_ground_truth']
                    if case == 'dense':
                        row['passed'] = row['passed'] and bool(row['shortening_plans'])
                    if task['state'] == 'DONE':
                        output = directory/'output/video_vi.mp4'
                        row['encoded_sha256'] = digest(output)
                        subprocess.run([str(get_ffmpeg_bin()),'-v','error','-i',str(output),'-f','null','-'],capture_output=True,check=True,timeout=60)
                        response = session.get(endpoint+f'/api/projects/{project}/files/dubbed_video',headers={'Range':'bytes=0-65535'},timeout=10)
                        row['http_range_matches'] = response.status_code==206 and response.content==output.read_bytes()[:65536]
                        with patch.object(EdgeTTSProvider,'synthesize',side_effect=AssertionError('Warm replay called TTS')), \
                             patch.object(BatchTranslator,'_call_gemini_with_rotation',side_effect=AssertionError('Warm replay called Gemini')):
                            replay = run_task(case+'_warm',project)
                        row['warm_replay_done'] = replay['state']=='DONE'
                        row['passed'] = row['passed'] and row['http_range_matches'] and row['warm_replay_done']
                    if case == 'original':
                        row['passed'] = row['passed'] and len(report['edge_requests'])==before_requests
                except Exception as exc:
                    row.update(passed=False,error_type=type(exc).__name__)
                row['actual_edge_requests'] = len(report['edge_requests'])-before_requests
                with SessionLocal() as db:
                    row['actual_gemini_requests'] = db.query(APIRequest).filter_by(video_id=project).count()
                report['cases'].append(row)
                event(case, finished=True, passed=row['passed'])
        if not args.no_browser:
            from playwright.sync_api import sync_playwright
            errors = []
            browser_result = {'passed':False,'javascript_errors':errors}
            try:
                with sync_playwright() as runtime:
                    browser = runtime.chromium.launch(headless=True)
                    page = browser.new_page(viewport={'width':1440,'height':1000})
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    page.goto(endpoint,wait_until='networkidle',timeout=30000)
                    target = 'audit_original' if 'original' in args.cases else 'audit_'+args.cases[0]
                    page.locator(f'tr[data-id="{target}"] .btn-open-project').click()
                    page.wait_for_function('currentProjectId === '+json.dumps(target))
                    page.wait_for_function('document.getElementById("audio-mode").value === '+json.dumps('original_only' if 'original' in args.cases else 'mix'))
                    options = page.evaluate("""()=>{DOM.voicePick.value='vi-VN-NamMinhNeural'; DOM.voiceRate.value='10';
                        document.getElementById('voice-auto-fit').checked=false; document.getElementById('voice-auto-shorten').checked=false;return readVoiceSettings();}""")
                    assert options['voice']=='vi-VN-NamMinhNeural' and options['rate']=='+10%' and not options['fit_to_video'] and not options['shorten_to_fit']
                    actual = page.evaluate("async()=>await API.request('GET','/api/projects/'+currentProjectId+'/verify')")
                    page.evaluate('report=>renderVerifyScorecard(report)',actual)
                    if target == 'audit_original':
                        assert actual['voice_required'] is False
                        assert page.locator('#qa-metric-coverage').get_attribute('class')=='qa-pass'
                    page.locator('[data-studio="verify"]').click()
                    page.wait_for_function('DOM.detailVideo.readyState>=2 && DOM.detailVideo.videoWidth===640', timeout=20000)
                    playback = page.evaluate("""async()=>{
                        const video=DOM.detailVideo; video.muted=true; await video.play();
                        await new Promise(resolve=>{video.addEventListener('seeked',resolve,{once:true});video.currentTime=8;});
                        video.pause(); return {width:video.videoWidth,height:video.videoHeight,time:video.currentTime,
                            output:video.currentSrc.includes('source=dubbed'),error:video.error?.code || null};
                    }""")
                    assert playback['output'] and playback['error'] is None and abs(playback['time']-8)<.1
                    page.screenshot(path=str(root/'browser_desktop.png'),full_page=True)
                    page.set_viewport_size({'width':390,'height':844})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.screenshot(path=str(root/'browser_mobile.png'),full_page=True)
                    browser.close()
                browser_result.update(passed=not errors,voice_options=options,playback=playback)
            except Exception as exc:
                browser_result['error_type'] = type(exc).__name__
            report['browser'] = browser_result
    finally:
        report['user_transcript_db_unchanged'] = original_snapshot == db_snapshot(original_db)
        report['passed'] = bool(report['cases']) and all(c['passed'] for c in report['cases']) and report['user_transcript_db_unchanged'] and (args.no_browser or bool(report['browser'] and report['browser']['passed']))
        save()
        server.should_exit = True
        thread.join(timeout=5)
        session.close()
        print(json.dumps({'report':str(root/'report.json'),'passed':report['passed']},ensure_ascii=True),flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
