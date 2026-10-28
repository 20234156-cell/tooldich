"""Isolated real HTTP/Range, OS lock, process-crash, admin UI and restore audit."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time

BASE=Path(__file__).resolve().parents[1];sys.path.insert(0,str(BASE))
parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path);parser.add_argument('--crash',action='store_true');parser.add_argument('--batch');parser.add_argument('--candidate');args=parser.parse_args()
root=(args.root or BASE/'storage/audits'/('phase4_'+time.strftime('%Y%m%dT%H%M%S'))).resolve();root.mkdir(parents=True,exist_ok=True)
os.environ.update(DATABASE_URL='sqlite:///'+(root/'app.db').as_posix(),STORAGE_DIR=str(root/'projects'),SERVER_MODE='true',SERVER_AUTO_BACKUP_HOURS='0',
    SERVER_BACKUP_ROOT=str(Path('D:/DuanLayLinkBackups')/('validation_'+root.name)/'snapshots'),SERVER_BACKUP_DIRECTORY=str(Path('D:/DuanLayLinkBackups')/('validation_'+root.name)),SERVER_BACKUP_ALLOW_SAME_DISK='true',GEMINI_API_KEY='',API_TOKEN='',RATE_LIMIT_ENABLED='false')
from config.settings import settings
settings.BASE_DIR=root/'application'
from src.database.connection import init_db
from src.database.models import Project,Source,Media,Transcript,UploadSession,FileLifecycle
from src.server import cleanup,readers,remediation,store,backups,backup_remote
from src.pipeline.render import get_ffmpeg_bin
init_db()
if args.crash:
    cleanup.after_mark=lambda _:os._exit(74)
    cleanup.execute(args.batch,[args.candidate],'audit_admin');raise RuntimeError('Crash seam not reached')

report=dict(production_projects_modified=False,external_provider_calls=0,checks={},browser_errors=[])
server=None;log=None
try:
    source=settings.storage_path/'audit_project/source/video.mp4';source.parent.mkdir(parents=True)
    subprocess.run([str(get_ffmpeg_bin()),'-hide_banner','-loglevel','error','-y','-f','lavfi','-i','testsrc=size=320x180:rate=15','-t','1.2','-c:v','libx264','-pix_fmt','yuv420p',str(source)],check=True,timeout=30)
    with store.transaction() as db:
        db.add(Project(id='audit_project',title='Audit failed voice',status='FAILED'));db.flush()
        db.add(Source(project_id='audit_project',platform='local_file',input_url=str(source)))
        db.add(Media(project_id='audit_project',file_path=str(source),is_valid=True))
        db.add(Transcript(project_id='audit_project',segments=[dict(index=3,start=.1,end=.9,text='fixture',vi_text='Giữ nguyên chữ',voice_audio_path='voiceover/clips/missing.wav',voice_duration=.5)]))
    state=source.parent.parent/'pipeline_state.json';state.write_text(json.dumps(dict(current_state='TRANSLATION_REVIEWED',stages={'VOICE_READY':dict(status='FAILED')})))
    planned=remediation.preview('audit_project','audit_admin');reset=remediation.execute(planned['preview_id'],'audit_admin',True)
    report['checks']['audited_reset_preserves_text_timing']=reset['text_and_timing_unchanged'] and reset['count']==1
    Path(settings.SERVER_BACKUP_DIRECTORY).mkdir(parents=True,exist_ok=True)
    backup=backups.create();report['checks']['reset_allows_real_media_backup']=backup['status']=='VALIDATED_PASS' and backup['replica_status']=='LOCAL_REPLICA_VERIFIED'
    restored=backup_remote.drill(backup['name'],root/'restored');report['checks']['audit_evidence_restored']=restored['status']=='LOCAL_RESTORE_DRILL_PASS' and (root/'restored/audits/stage_resets'/Path(reset['audit_path']).name).is_file()
    from src.server.uploads import root as upload_root
    def abandoned(identifier):
        directory=upload_root(identifier);directory.mkdir(parents=True);shutil.copyfile(source,directory/'payload.part')
        with store.transaction() as db:db.add(UploadSession(upload_id=identifier,owner_username='audit_admin',filename='fixture.mp4',total_size=source.stat().st_size,chunk_size=8192,status='ABORTED',created_at=time.time()-200000,updated_at=time.time()-200000,expires_at=time.time()-100000))
        return directory
    def candidate(identifier):
        batch=cleanup.preview('audit_admin');item=next(item for item in batch['candidates'] if item['owner_id']==identifier);return batch,item
    identifier='a'*32;directory=abandoned(identifier);batch,item=candidate(identifier)
    child=subprocess.run([sys.executable,__file__,'--root',str(root),'--crash','--batch',batch['batch_id'],'--candidate',item['candidate_id']],cwd=BASE,timeout=30,capture_output=True)
    report['crash_exit_code']=child.returncode
    with store.transaction() as db:report['checks']['native_crash_leaves_durable_deleting']=child.returncode==74 and db.get(FileLifecycle,str(directory)).state=='DELETING' and directory.exists()
    cleanup.recover();report['checks']['recovery_finishes_delete']=not directory.exists()
    locked=abandoned('b'*32);batch,item=candidate('b'*32)
    with (locked/'payload.part').open('rb'):
        result=cleanup.execute(batch['batch_id'],[item['candidate_id']],'audit_admin')
        report['checks']['actual_os_lock_returns_delete_failed']=result['results'][0]['result']=='DELETE_FAILED' and locked.exists()
    batch,item=candidate('b'*32);cleanup.execute(batch['batch_id'],[item['candidate_id']],'audit_admin')
    report['checks']['explicit_retry_after_unlock']=not locked.exists()
    pending=abandoned('c'*32)
    password=secrets.token_urlsafe(24);store.save_user('audit_admin',password,'admin',first=True)
    with socket.socket() as listener:listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
    url=f'http://127.0.0.1:{port}';env=os.environ.copy();env['SERVER_BACKUP_DIRECTORY']=''
    log=(root/'server.log').open('w',encoding='utf-8')
    server=subprocess.Popen([sys.executable,'main.py','serve','--host','127.0.0.1','--port',str(port)],cwd=BASE,env=env,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    import requests
    for _ in range(150):
        try:
            if requests.get(url+'/api/server/session',timeout=1).status_code==200:break
        except requests.RequestException:pass
        assert server.poll() is None
        time.sleep(.2)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1000});page.on('pageerror',lambda exc:report['browser_errors'].append(str(exc)))
        page.goto(url,wait_until='domcontentloaded');form=page.locator('.server-login-form');form.locator('[name=username]').fill('audit_admin');form.locator('[name=password]').fill(password);form.locator('[type=submit]').click();page.wait_for_function('window.workspaceServerUser?.role==="admin"')
        report['checks']['real_range_206']=page.evaluate("async()=>{const r=await fetch('/api/projects/audit_project/preview',{headers:{Range:'bytes=0-99'}});return r.status===206&&(await r.arrayBuffer()).byteLength===100}")
        page.locator('.server-admin-launch').click();page.get_by_role('link',name='Dọn dữ liệu',exact=True).click()
        section=page.locator('#server-section-cleanup');section.wait_for();section.get_by_role('button',name='Xem trước dữ liệu có thể dọn',exact=True).click()
        check=section.locator('input[type=checkbox]:enabled');check.first.wait_for()
        report['checks']['preview_does_not_delete']=pending.exists()
        check.first.check();page.on('dialog',lambda dialog:dialog.accept())
        section.get_by_role('button',name='Xóa các mục đã chọn',exact=True).click()
        page.wait_for_function("!document.querySelector('#server-section-cleanup input[type=checkbox]')")
        report['checks']['explicit_ui_selection_deletes_only_draft']=not pending.exists() and source.is_file()
        report['checks']['empty_selection_stays_disabled']=section.get_by_role('button',name='Xóa các mục đã chọn',exact=True).is_disabled()
        report['checks']['audit_document_readable_by_admin']=page.evaluate("async(id)=>(await fetch('/api/admin/stage-reset/audits/'+id)).status",reset['audit_id'])==200
        section.scroll_into_view_if_needed();section.screenshot(path=str(root/'cleanup_desktop.png'))
        page.set_viewport_size({'width':390,'height':844});section.scroll_into_view_if_needed();section.screenshot(path=str(root/'cleanup_mobile.png'))
        browser.close()
    assert all(report['checks'].values()),report['checks']
    assert not report['browser_errors']
    report['success']=True
finally:
    if server:server.terminate();server.wait(timeout=15)
    if log:log.close()
    (root/'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps(dict(report=str(root/'report.json'),success=report.get('success',False)),ensure_ascii=True))
