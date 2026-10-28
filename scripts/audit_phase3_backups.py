"""Isolated real-media/HTTP/browser backup and same-disk D: restore drill."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
root=BASE/'storage/audits'/('phase3_'+time.strftime('%Y%m%dT%H%M%S'));root.mkdir(parents=True)
replica=Path('D:/DuanLayLinkBackups')/('validation_'+root.name);replica.mkdir(parents=True)
os.environ.update(DATABASE_URL='sqlite:///'+(root/'app.db').as_posix(),STORAGE_DIR=str(root/'projects'),SERVER_MODE='true',SERVER_AUTO_BACKUP_HOURS='0',
    SERVER_BACKUP_DIRECTORY=str(replica),SERVER_BACKUP_ALLOW_SAME_DISK='true',GEMINI_API_KEY='',API_TOKEN='',RATE_LIMIT_ENABLED='false')
from config.settings import settings
settings.BASE_DIR=root/'application'  # No real secrets/environment copied in this audit.
from src.database.connection import init_db
from src.database.models import Project,Source,Media,Transcript,Job,ProcessingAttempt,ArtifactPointer
from src.server import backups,backup_remote,store
from src.pipeline.render import get_ffmpeg_bin

report={'production_projects_modified':False,'external_provider_calls':0,'checks':{},'browser_errors':[]}
server=None;log=None
try:
    init_db();folder=settings.storage_path/'audit_backup';folder.mkdir(parents=True)
    video=folder/'source/video.mp4';video.parent.mkdir()
    subprocess.run([str(get_ffmpeg_bin()),'-hide_banner','-loglevel','error','-y','-f','lavfi','-i','testsrc=size=320x180:rate=15','-f','lavfi','-i','sine=frequency=240:sample_rate=24000','-t','1.2','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(video)],check=True,timeout=30)
    directory=folder/'attempts/voice/voiceover';directory.mkdir(parents=True)
    voice=directory/'clip.wav'
    subprocess.run([str(get_ffmpeg_bin()),'-hide_banner','-loglevel','error','-y','-f','lavfi','-i','sine=frequency=440:sample_rate=24000','-t','0.8',str(voice)],check=True,timeout=30)
    name=voice.relative_to(folder).as_posix()
    with store.transaction() as db:
        db.add(Project(id='audit_backup'));db.flush()
        db.add(Source(project_id='audit_backup',platform='local_file',input_url=str(video)))
        db.add(Media(project_id='audit_backup',file_path=str(video),file_size_bytes=video.stat().st_size,is_valid=True))
        db.add(Job(id='job_backup',project_id='audit_backup',status='COMPLETED'))
        db.add(Transcript(project_id='audit_backup',segments=[dict(index=1,start=0,end=.8,text='fixture',vi_text='Mẫu kiểm tra',voice_audio_path=name)]))
        db.add(ProcessingAttempt(id='voice',project_id='audit_backup',job_id='job_backup',kind='voiceover',number=1,fence=1,fingerprint='fixture',inputs={},options={},directory='attempts/voice',state='PUBLISHED',created_at=time.time(),
              manifest={'files':{name:dict(sha256=backups.checksum(voice),size=voice.stat().st_size,verification='VERIFIED_AT_CREATION')}}))
        db.add(ArtifactPointer(project_id='audit_backup',kind='voiceover',attempt_id='voice',directory='attempts/voice/voiceover'))
    result=backups.create();backup=backups.backup_root()/result['name']
    report['backup']=result
    report['checks']['real_media_package_validated']=backups.verify(backup)['status']=='VALIDATED_PASS'
    report['checks']['same_physical_disk_correctly_labeled']=result.get('replica_status')=='LOCAL_REPLICA_VERIFIED'
    drill=backup_remote.drill(result['name'],root/'restored')
    report['drill']=drill
    report['checks']['all_required_restored']=drill['status']=='LOCAL_RESTORE_DRILL_PASS' and drill['required_files']==2
    report['checks']['same_disk_never_enables_cleanup']=not backup_remote.deletion_allowed(video)
    record=store.get('backup:remote:'+result['name']);packed=Path(record['path']);good_sha=backups.checksum(packed)
    # A byte changed BEFORE backup must be compared to its original baseline.
    raw=voice.read_bytes();voice.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
    try:backups.create()
    except backups.BackupSourceError as exc:report['checks']['preexisting_byte_corruption_detected']=exc.status=='FAILED_CORRUPT_SOURCE'
    voice.write_bytes(raw)
    video.rename(video.with_suffix('.held'))
    try:backups.create()
    except backups.BackupSourceError as exc:report['checks']['missing_required_detected']=exc.status=='FAILED_INCOMPLETE'
    video.with_suffix('.held').rename(video)
    report['checks']['last_good_replica_unchanged']=backups.checksum(packed)==good_sha
    password=secrets.token_urlsafe(24);store.save_user('audit_admin',password,'admin',first=True)
    with socket.socket() as listener:listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
    env=os.environ.copy();env['SERVER_BACKUP_DIRECTORY']='';log=(root/'server.log').open('w',encoding='utf-8')
    server=subprocess.Popen([sys.executable,'main.py','serve','--host','127.0.0.1','--port',str(port)],cwd=BASE,env=env,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    import requests
    url=f'http://127.0.0.1:{port}'
    for _ in range(120):
        try:
            if requests.get(url+'/api/server/session',timeout=1).status_code==200:break
        except requests.RequestException:pass
        assert server.poll() is None
        time.sleep(.25)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1000});page.on('pageerror',lambda exc:report['browser_errors'].append(str(exc)))
        page.goto(url,wait_until='domcontentloaded');form=page.locator('.server-login-form');form.locator('[name=username]').fill('audit_admin');form.locator('[name=password]').fill(password);form.locator('[type=submit]').click()
        page.wait_for_function('window.workspaceServerUser?.role==="admin"');page.locator('.server-admin-launch').click()
        page.get_by_role('link',name='Sao lưu',exact=True).click()
        section=page.locator('#server-section-backups');section.wait_for()
        report['checks']['ui_local_replica_label']=section.get_by_text('Bản sao cục bộ · đã phục hồi thử',exact=True).count()==1
        report['checks']['ui_failed_backups_not_success']=section.get_by_text('FAILED_INCOMPLETE',exact=True).count()==1 and section.get_by_text('FAILED_CORRUPT_SOURCE',exact=True).count()==1
        section.scroll_into_view_if_needed();section.screenshot(path=str(root/'backup_admin.png'))
        page.set_viewport_size({'width':390,'height':844});section.scroll_into_view_if_needed();section.screenshot(path=str(root/'backup_mobile.png'))
        browser.close()
    assert all(report['checks'].values()),report['checks']
    assert not report['browser_errors']
    report['success']=True
finally:
    if server:
        server.terminate();server.wait(timeout=15)
    if log:log.close()
    (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'report':str(root/'report.json'),'success':report.get('success',False)},ensure_ascii=True))
