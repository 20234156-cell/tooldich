"""Isolated real-browser >100MB resume audit. No provider or production writes."""
import hashlib,json,os,secrets,socket,struct,subprocess,sys,time
from pathlib import Path
import requests
from playwright.sync_api import sync_playwright
BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
root=BASE/'storage/audits'/('resumable_'+time.strftime('%Y%m%dT%H%M%S'));root.mkdir(parents=True)
with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
url=f'http://127.0.0.1:{port}'
env=os.environ.copy();env.update(SERVER_MODE='true',SERVER_AUTO_BACKUP_HOURS='0',DATABASE_URL='sqlite:///'+(root/'app.db').as_posix(),STORAGE_DIR=str(root/'projects'),ALLOWED_HOSTS='127.0.0.1,localhost',API_TOKEN='',RATE_LIMIT_ENABLED='false',PYTHONIOENCODING='utf-8')
log=(root/'server.log').open('w',encoding='utf-8');proc=None
report={'production_modified':False,'network_scope':'loopback; not an external-network/tunnel certification','fixture':'2s real H264/AAC MP4 plus standards-compliant free box to exercise >100MB transport','browser_errors':[]}
try:
    from src.pipeline.render import get_ffmpeg_bin
    sample=root/'sample.mp4'
    subprocess.run([str(get_ffmpeg_bin()),'-hide_banner','-loglevel','error','-y','-f','lavfi','-i','testsrc=size=320x180:rate=10','-f','lavfi','-i','sine=frequency=440:sample_rate=24000','-t','2','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(sample)],check=True,timeout=30)
    with sample.open('ab') as f:
        size=105*1024*1024;f.write(struct.pack('>I4s',size,b'free'))
        block=bytes(1024*1024);remaining=size-8
        while remaining:chunk=block[:min(remaining,len(block))];f.write(chunk);remaining-=len(chunk)
    data=sample.read_bytes();expected=hashlib.sha256(data).hexdigest()
    fingerprint=hashlib.sha256(data[:1024*1024]+data[-1024*1024:]+str(len(data)).encode()).hexdigest()
    proc=subprocess.Popen([sys.executable,'main.py','serve','--host','127.0.0.1','--port',str(port)],cwd=BASE,env=env,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    for _ in range(150):
        try:
            if requests.get(url+'/api/server/session',timeout=1).status_code==200:break
        except requests.RequestException:pass
        if proc.poll() is not None:raise RuntimeError('Audit server failed')
        time.sleep(.2)
    password=secrets.token_urlsafe(24);client=requests.Session()
    r=client.post(url+'/api/server/setup',json={'username':'audit_admin','password':password},timeout=10);r.raise_for_status();client.headers['X-CSRF-Token']=r.json()['csrf']
    r=client.post(url+'/api/uploads/init',json={'filename':sample.name,'total_size':len(data),'fingerprint':fingerprint,'expected_sha256':expected},timeout=10);r.raise_for_status();session=r.json();identifier=session['upload_id'];part=data[:session['chunk_size']]
    r=client.patch(url+f'/api/uploads/{identifier}/chunk?expected_offset=0',data=part,headers={'X-Chunk-SHA256':hashlib.sha256(part).hexdigest()},timeout=15);r.raise_for_status()
    report['initial_bytes_acknowledged']=len(part)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1000})
        page.on('pageerror',lambda e:report['browser_errors'].append(str(e)))
        page.goto(url,wait_until='domcontentloaded')
        form=page.locator('.server-login-form');form.locator('[name=username]').fill('audit_admin');form.locator('[name=password]').fill(password);form.locator('[type=submit]').click()
        page.wait_for_function('window.workspaceServerUser?.role === "admin"')
        page.locator('.sidebar-nav [data-view=new]').click()
        row=page.locator('.server-pending-uploads article').filter(has_text='sample.mp4');row.wait_for()
        dropped={'value':False}
        def drop_once(route):
            if not dropped['value']:
                route.fetch(timeout=30000);dropped['value']=True;route.abort('failed')
            else:route.continue_()
        page.route('**/api/uploads/*/chunk?*',drop_once)
        with page.expect_file_chooser() as choice:row.get_by_role('button',name='Chọn lại tệp để tiếp tục').click()
        choice.value.set_files(str(sample))
        page.wait_for_function("document.querySelector('.server-upload-panel').textContent.includes('Video đã được kiểm định và tạo dự án.')",timeout=120000)
        final=client.get(url+f'/api/uploads/{identifier}/status',timeout=15).json()
        report.update(bytes=len(data),over_100mb=len(data)>100*1024*1024,dropped_response_after_server_commit=dropped['value'],browser_resumed_existing_session=final['status']=='COMPLETED',project_id=final['project_id'])
        items=client.get(url+'/api/projects',timeout=10).json();report['only_one_project']=len(items if isinstance(items,list) else items['projects'])==1
        page.screenshot(path=str(root/'browser.png'));page.evaluate("async(id)=>{await fetch('/api/server/lease/'+id,{method:'DELETE'})}",final['project_id']);browser.close()
    payload=root/'uploads/sessions'/identifier/(identifier+'.mp4')
    report['final_sha256_matches']=hashlib.sha256(payload.read_bytes()).hexdigest()==expected
    complete=client.post(url+f'/api/uploads/{identifier}/complete',timeout=10)
    report['complete_retry_same_project']=complete.status_code==200 and complete.json()['project_id']==final['project_id']
    # Import the actual source through the existing pipeline, not just a new label.
    own=client.post(url+'/api/server/lease/'+final['project_id'],timeout=10);own.raise_for_status()
    download=client.post(url+'/api/projects/'+final['project_id']+'/download',json={},headers={'If-Match':str(own.json()['revision'])},timeout=10);download.raise_for_status()
    task_id=download.json()['task_id']
    for _ in range(180):
        task=client.get(url+'/api/tasks/'+task_id,timeout=10).json()
        if task['state'] not in ['QUEUED','RUNNING']:break
        time.sleep(.25)
    report['existing_import_pipeline_passed']=task['state']=='DONE'
    report['success']=all(report.get(k) for k in ['over_100mb','dropped_response_after_server_commit','browser_resumed_existing_session','only_one_project','final_sha256_matches','complete_retry_same_project','existing_import_pipeline_passed']) and not report['browser_errors']
except Exception as exc:report['success']=False;report['error']=str(exc)
finally:
    if proc:
        proc.terminate()
        try:proc.wait(15)
        except subprocess.TimeoutExpired:proc.kill();proc.wait(5)
    log.close();(root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'report':str(root/'report.json'),**report},ensure_ascii=True))
if not report.get('success'):sys.exit(1)
