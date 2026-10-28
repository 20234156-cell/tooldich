"""Isolated local-WAV/FFmpeg, crash-before-publish and browser recovery audit."""
import argparse,hashlib,io,json,os,shutil,socket,subprocess,sys,time
from pathlib import Path
BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
parser=argparse.ArgumentParser();parser.add_argument('--crash',action='store_true');parser.add_argument('--root',type=Path);args=parser.parse_args()
root=args.root or BASE/'storage/audits'/('phase2_'+time.strftime('%Y%m%dT%H%M%S'))
root=root.resolve();root.mkdir(parents=True,exist_ok=True)
os.environ.update(SERVER_MODE='true',SERVER_AUTO_BACKUP_HOURS='0',DATABASE_URL='sqlite:///'+(root/'app.db').as_posix(),STORAGE_DIR=str(root/'projects'),ALLOWED_HOSTS='127.0.0.1,localhost',API_TOKEN='',GEMINI_API_KEY='',RATE_LIMIT_ENABLED='false',PYTHONIOENCODING='utf-8')
from config.settings import settings
from src.database.connection import init_db
init_db()
from src.database.models import Project,Job,Source,Media,Transcript,ProcessingAttempt
from src.server import store
from src.jobs.queue import JobQueue
from src.pipeline.orchestrator import orchestrator
from src.pipeline.attempts import artifact_path,build_area,recover_attempts
from src.pipeline.render import RenderConfig,get_ffmpeg_bin
from src.pipeline.roi import ROIBox
from src.pipeline.state_machine import PipelineState,ProjectState
from src.ocr.base import Cue
project='phase2_audit';folder=settings.storage_path/project
cfg=RenderConfig(audio_mode='voice_only',use_nvenc=False)
if args.crash:
    def abandoned(cfg=None):
        with build_area(folder,'output',{'cfg':cfg.model_dump()}) as area:
            target=area.directory/'output/video_vi.mp4';target.parent.mkdir()
            shutil.copy2(artifact_path(folder,'output/video_vi.mp4'),target)
            with target.open('r+b') as stream:stream.flush();os.fsync(stream.fileno())
            os._exit(74)
    queue=JobQueue(1);task=queue.submit(project,'render',abandoned,cfg=cfg);task.future.result(30);sys.exit(2)

report={'production_modified':False,'provider_calls':0,'audio_fixture':'local synthetic non-silent WAV; no speech-quality claim'}
queue=None;server=None;log=None
try:
    folder.mkdir(parents=True,exist_ok=True);video=folder/'source/video.mp4';video.parent.mkdir()
    subprocess.run([str(get_ffmpeg_bin()),'-hide_banner','-loglevel','error','-y','-f','lavfi','-i','testsrc=size=320x180:rate=15','-f','lavfi','-i','sine=frequency=240:sample_rate=24000','-t','2.4','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(video)],check=True,timeout=30)
    from src.media.validator import MediaValidator
    meta=MediaValidator().validate(video)
    with store.transaction() as db:
        db.add(Project(id=project,title='Phase2 isolated audit'));db.add(Job(id='job_phase2_audit',project_id=project))
        db.add(Source(project_id=project,platform='local_file',input_url=str(video)))
        db.add(Media(project_id=project,file_path=str(video),checksum_sha256=hashlib.sha256(video.read_bytes()).hexdigest(),file_size_bytes=video.stat().st_size,duration=meta.duration,width=meta.width,height=meta.height,is_valid=True))
    orchestrator.save_roi_set(project,[ROIBox(x=.05,y=.8,width=.9,height=.15).model_dump()])
    orchestrator.save_cues(project,[Cue(index=1,start=.1,end=1.05,text='你好',vi_text='Xin chào'),Cue(index=2,start=1.25,end=2.2,text='谢谢',vi_text='Cảm ơn')])
    orchestrator.save_state(project,PipelineState(current_state=ProjectState.TRANSLATION_REVIEWED))
    legacy=folder/'voiceover/voiceover.wav';legacy.parent.mkdir(exist_ok=True);legacy.write_bytes(b'legacy delivery remains untouched')
    from unittest.mock import patch
    from src.tts.edge import EdgeTTSProvider
    import numpy as np,soundfile as sf
    def local_wav(*args,**kwargs):
        buffer=io.BytesIO();sf.write(buffer,np.sin(np.arange(int(.9*24000))*.1)*.15,24000,format='WAV');return buffer.getvalue(),.9
    queue=JobQueue(1)
    with patch.object(EdgeTTSProvider,'synthesize',side_effect=local_wav):
        task=queue.submit(project,'voiceover',orchestrator.run_voiceover,project,inject_cancel=True,voice='vi-VN-NamMinhNeural');task.future.result(60)
    if task.state!='DONE':raise RuntimeError('Voice: '+str(task.error_message))
    voice=artifact_path(folder,'voiceover/voiceover.wav');report['voice_published_in_attempt']='attempts' in voice.parts and voice.is_file()
    report['legacy_master_unchanged']=legacy.read_bytes()==b'legacy delivery remains untouched'
    task=queue.submit(project,'render',orchestrator.run_render,project,cfg=cfg,inject_cancel=True);task.future.result(120)
    if task.state!='DONE':raise RuntimeError('Render: '+str(task.error_message))
    master=artifact_path(folder,'output/video_vi.mp4');report['render_published_in_attempt']='attempts' in master.parts and master.is_file()
    report['master_sha256']=hashlib.sha256(master.read_bytes()).hexdigest()
    qa=orchestrator.verify(project);report['technical_qa_passed']=qa.passed
    if not qa.passed:report['qa_errors']=qa.errors
    queue.shutdown();queue=None
    child=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--crash','--root',str(root)],cwd=BASE,env=os.environ.copy(),capture_output=True,text=True,timeout=60)
    report['crash_exit_code']=child.returncode
    if child.returncode!=74:raise RuntimeError('Crash fixture did not reach boundary: '+child.stderr[-1000:])
    from src.api.app import _recover_interrupted_jobs
    from src.server.tasks import recover
    _recover_interrupted_jobs();recover();recover_attempts()
    with store.transaction() as db:
        report['job_interrupted_after_crash']=db.get(Job,'job_phase2_audit').status=='INTERRUPTED'
        report['uncommitted_attempt_detected']=db.query(ProcessingAttempt).filter_by(project_id=project,state='UNCOMMITTED_ATTEMPT').count()>0
        before_count=db.query(ProcessingAttempt).count()
    report['previous_delivery_unchanged_after_crash']=hashlib.sha256(artifact_path(folder,'output/video_vi.mp4').read_bytes()).hexdigest()==report['master_sha256']
    import requests,secrets
    with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    url=f'http://127.0.0.1:{port}';log=(root/'server.log').open('w',encoding='utf-8')
    server=subprocess.Popen([sys.executable,'main.py','serve','--host','127.0.0.1','--port',str(port)],cwd=BASE,env=os.environ.copy(),stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    for _ in range(150):
        try:
            if requests.get(url+'/api/server/session',timeout=1).status_code==200:break
        except requests.RequestException:pass
        if server.poll() is not None:raise RuntimeError('Audit HTTP server failed')
        time.sleep(.2)
    password=secrets.token_urlsafe(24);requests.post(url+'/api/server/setup',json={'username':'phase2_admin','password':password},timeout=10).raise_for_status()
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(url,wait_until='domcontentloaded');form=page.locator('.server-login-form');form.locator('[name=username]').fill('phase2_admin');form.locator('[name=password]').fill(password);form.locator('[type=submit]').click();page.wait_for_function('window.workspaceServerUser?.role === "admin"')
        page.evaluate('(id)=>openProject(id)',project)
        panel=page.locator('.recovery-panel');panel.wait_for(timeout=15000)
        with store.transaction() as db:report['opening_project_did_not_start_work']=db.query(ProcessingAttempt).count()==before_count
        report['recovery_ui_visible']=True;page.set_viewport_size({'width':390,'height':844});panel.scroll_into_view_if_needed();bounds=panel.bounding_box();report['mobile_recovery_within_viewport']=bool(bounds and bounds['x']>=0 and bounds['x']+bounds['width']<=390);panel.screenshot(path=str(root/'interrupted_mobile.png'));page.set_viewport_size({'width':1440,'height':1000});panel.locator('select').select_option('previous')
        page.on('dialog',lambda dialog:dialog.accept())
        panel.get_by_role('button',name='Kiểm tra và tiếp tục').click()
        page.wait_for_function('currentProject?.task?.state === "DONE"',timeout=120000)
        report['browser_resume_done']=True;report['browser_errors']=errors
        page.screenshot(path=str(root/'recovered.png'));browser.close()
    report['success']=all(report.get(k) for k in ['voice_published_in_attempt','legacy_master_unchanged','render_published_in_attempt','technical_qa_passed','job_interrupted_after_crash','uncommitted_attempt_detected','previous_delivery_unchanged_after_crash','opening_project_did_not_start_work','recovery_ui_visible','mobile_recovery_within_viewport','browser_resume_done']) and not report.get('browser_errors')
except Exception as exc:report['success']=False;report['error']=str(exc)
finally:
    if queue:queue.shutdown()
    if server:
        server.terminate()
        try:server.wait(15)
        except subprocess.TimeoutExpired:server.kill();server.wait(5)
    if log:log.close()
    (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'report':str(root/'report.json'),**report},ensure_ascii=True))
if not report.get('success'):sys.exit(1)
