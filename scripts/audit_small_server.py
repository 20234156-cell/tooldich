"""Isolated two-client HTTP/browser/server-restart audit; no production changes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

import requests

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    url=f'http://127.0.0.1:{port}'
    env=os.environ.copy()
    env.update(SERVER_MODE='true',SERVER_AUTO_BACKUP_HOURS='0',DATABASE_URL='sqlite:///'+(root/'app.db').as_posix(),
               STORAGE_DIR=str(root/'projects'),ALLOWED_HOSTS='127.0.0.1,localhost',GEMINI_API_KEY='',API_TOKEN='',
               PYTHONIOENCODING='utf-8',RATE_LIMIT_ENABLED='false')
    process=None
    log=(root/'server.log').open('a',encoding='utf-8')
    report={'production_modified':False,'network_scope':'loopback only; not a physical two-PC LAN certification'}
    def start():
        proc=subprocess.Popen([sys.executable,'main.py','serve','--host','127.0.0.1','--port',str(port)],
            cwd=BASE,env=env,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        for _ in range(120):
            if proc.poll() is not None:raise RuntimeError('Audit server startup failed; inspect log')
            try:
                if requests.get(url+'/api/server/session',timeout=1).status_code==200:return proc
            except requests.RequestException:pass
            time.sleep(.25)
        proc.terminate();proc.wait(10)
        raise RuntimeError('Audit server startup timeout')
    def authenticate(client,username,password,setup=False):
        r=client.post(url+('/api/server/setup' if setup else '/api/server/login'),json={'username':username,'password':password},timeout=10)
        r.raise_for_status()
        client.headers.update({'X-CSRF-Token':r.json()['csrf'],'X-Workspace-Client':secrets.token_hex(12)})
    try:
        process=start()
        password=secrets.token_urlsafe(24)
        admin=requests.Session();operator=requests.Session()
        authenticate(admin,'audit_admin',password,True)
        admin.put(url+'/api/server/users',json={'username':'audit_operator','password':password,'role':'operator'},timeout=10).raise_for_status()
        authenticate(operator,'audit_operator',password)
        report['two_authenticated_clients']=len(admin.get(url+'/api/server/status',timeout=5).json()['sessions'])==2
        from src.pipeline.render import get_ffmpeg_bin
        sample=root/'sample.mp4'
        subprocess.run([str(get_ffmpeg_bin()),'-hide_banner','-loglevel','error','-y','-f','lavfi','-i','testsrc=size=320x180:rate=10',
                        '-f','lavfi','-i','sine=frequency=440:sample_rate=24000','-t','3','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(sample)],check=True,timeout=30)
        response=operator.post(url+'/api/server/uploads',params={'filename':'sample.mp4'},data=sample.read_bytes(),headers={'Content-Type':'application/octet-stream'},timeout=30)
        response.raise_for_status();project=response.json()['project_id']
        own=operator.post(url+'/api/server/lease/'+project,timeout=5);own.raise_for_status()
        report['other_writer_blocked']=admin.post(url+'/api/server/lease/'+project,timeout=5).status_code==409
        download=operator.post(url+'/api/projects/'+project+'/download',headers={'If-Match':str(own.json()['revision'])},json={},timeout=10)
        download.raise_for_status();task_id=download.json()['task_id']
        for _ in range(120):
            task=operator.get(url+'/api/tasks/'+task_id,timeout=5).json()
            if task['state'] not in {'RUNNING','QUEUED'}:break
            time.sleep(.25)
        if task['state']!='DONE':raise RuntimeError('Import audit did not complete: '+str(task.get('error_message')))
        report['real_upload_and_import']=True
        detail=operator.get(url+'/api/projects/'+project,timeout=5).json()
        video=operator.get(url+'/api/projects/'+project+'/preview',headers={'Range':'bytes=0-1023'},timeout=10)
        report['authenticated_range']={'status':video.status_code,'bytes':len(video.content),'content_range':video.headers.get('Content-Range')}
        report['unauthenticated_media_blocked']=requests.get(url+'/api/projects/'+project+'/preview',timeout=5).status_code==401
        report['media']=detail.get('media')
        actual=root/'projects'/project/'source/video.mp4'
        report['isolated_media_verified']=actual.is_file() and hashlib.sha256(actual.read_bytes()).hexdigest()==detail['media']['checksum_sha256'] and not (BASE/'storage/projects'/project).exists()
        history=admin.get(url+'/api/server/status',timeout=5).json()['tasks']
        report['task_owner_persisted']=next(item for item in history if item['task_id']==task_id)['owner']=='audit_operator'
        operator.delete(url+'/api/server/lease/'+project,timeout=5).raise_for_status()
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            pages=[];errors=[]
            for name in ['audit_admin','audit_operator']:
                context=browser.new_context();page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(url,wait_until='domcontentloaded')
                form=page.locator('#server-panel-body form');form.locator('input').nth(0).fill(name);form.locator('input').nth(1).fill(password);form.locator('button[type=submit]').click()
                page.wait_for_function('window.workspaceServerUser?.username === '+json.dumps(name))
                page.wait_for_function("typeof openProject === 'function'")
                page.evaluate('openProject('+json.dumps(project)+')')
                page.wait_for_function('currentProject?.id === '+json.dumps(project))
                pages.append(page)
                if name=='audit_admin':
                    page.wait_for_function("document.getElementById('server-access-status').style.display==='none'")
                else:
                    page.wait_for_function("document.getElementById('server-access-status').style.display==='block'")
            report['two_browser_contexts_loaded_project']=True
            report['browser_errors']=errors
            pages[0].screenshot(path=str(root/'admin_browser.png'))
            browser.close()
        # Stop only the isolated child created above; production process is untouched.
        process.terminate();process.wait(15);process=start()
        restored=admin.get(url+'/api/tasks/'+task_id,timeout=5);restored.raise_for_status()
        report['completed_task_survives_restart']=restored.json()['state']=='DONE'
        report['session_survives_restart']=admin.get(url+'/api/projects',timeout=5).status_code==200
        report['success']=all([report['two_authenticated_clients'],report['other_writer_blocked'],report['unauthenticated_media_blocked'],report['completed_task_survives_restart'],report['session_survives_restart'],report['isolated_media_verified'],report['task_owner_persisted'],not report['browser_errors'],video.status_code==206])
    except Exception as exc:
        report.update(success=False,error=type(exc).__name__+': '+str(exc))
        raise
    finally:
        if process and process.poll() is None:process.terminate();process.wait(15)
        log.close()
        (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=True))


if __name__=='__main__':main()
