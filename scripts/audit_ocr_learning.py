"""Real loopback API/browser audit in an isolated, explicitly non-training workspace.

Sources are read-only. This never stops the user's server or mutates their cues.
Synthetic confirmation requests test mechanics, not transcription accuracy.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time


SEED = r'''
import sys,json,cv2
from pathlib import Path
from src.database.connection import init_db,SessionLocal
from src.database.models import Project,Media,Source
from src.ocr.base import Cue
from src.ocr.learning import propose_rule
from src.pipeline.orchestrator import PipelineOrchestrator
from src.pipeline.roi import ROIBox
from src.pipeline.state_machine import PipelineState,ProjectState
init_db();runner=PipelineOrchestrator()
for number,source in enumerate(json.loads(sys.argv[1])):
 ident=f'audit_learning_{number}'
 cap=cv2.VideoCapture(source);fps=cap.get(cv2.CAP_PROP_FPS);duration=cap.get(cv2.CAP_PROP_FRAME_COUNT)/fps
 w,h=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT));cap.release()
 with SessionLocal() as db:
  db.add(Project(id=ident,title='TEST FIXTURE learning '+str(number)))
  db.commit()
  db.add(Media(project_id=ident,file_path=source,duration=duration,width=w,height=h,is_valid=True,file_size_bytes=Path(source).stat().st_size))
  db.add(Source(project_id=ident,platform='local_file',input_url=source,title='TEST FIXTURE'))
  db.commit()
 runner.save_roi_set(ident,[ROIBox(x=.02,y=.78,width=.96,height=.2).model_dump()])
 runner.save_cues(ident,[Cue(index=34,start=2.5,end=3.5,text='测试夹具',confidence=50),Cue(index=90,start=4,end=5,text='第二句',confidence=99)])
 runner.save_state(ident,PipelineState(current_state=ProjectState.OCR_READY))
propose_rule('low_confidence','CẦN NGƯỜI')
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True,action='append',type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    workspace=Path(__file__).resolve().parents[1]
    sources=[str(p.resolve()) for p in args.source]
    if any(not Path(p).is_file() for p in sources):raise ValueError('Source missing')
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    env=os.environ.copy()
    env.update(DATABASE_URL='sqlite:///'+(out/'audit.db').as_posix(),STORAGE_DIR=str(out/'projects'),
               OCR_LEARNING_DIR=str(out/'learning'),OCR_LEARNING_TEST_FIXTURE='1',
               OFFLINE_MODE='true',PERSONAL_MODE='true',SERVER_MODE='false',API_TOKEN='',
               GEMINI_API_KEY='',OCR_DEVICE='cpu',PYTHONIOENCODING='utf-8')
    subprocess.run([sys.executable,'-c',SEED,json.dumps(sources)],cwd=workspace,env=env,check=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    log=(out/'server.log').open('w',encoding='utf-8')
    process=subprocess.Popen([sys.executable,'-m','uvicorn','src.api.app:app','--host','127.0.0.1','--port',str(port)],
        cwd=workspace,env=env,stdout=log,stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    report=dict(test_fixture=True,eligible_for_training=False,user_projects_changed=0,
                source_files=sources,checks=[],page_errors=[],status='running')
    import requests
    client=requests.Session();client.trust_env=False
    url=f'http://127.0.0.1:{port}'
    def call(method,path,body=None,status=200):
        response=client.request(method,url+path,json=body,timeout=60)
        if response.status_code!=status:raise AssertionError((method,path,response.status_code,response.text[:400]))
        return response
    try:
        deadline=time.monotonic()+45
        while time.monotonic()<deadline:
            if process.poll() is not None:raise RuntimeError('Audit server exited; inspect server.log')
            try:
                if client.get(url+'/api/projects',timeout=1).status_code==200:break
            except requests.RequestException:pass
            time.sleep(.25)
        else:raise RuntimeError('Audit server startup timeout')
        base='/api/projects/audit_learning_0'
        evidence=call('GET',base+'/cues/34/review-evidence').json()
        assert evidence['complete'] and len(evidence['frames'])==3
        assert len({f['frame_index'] for f in evidence['frames']})==3
        for frame in evidence['frames']:
            assert call('GET',base+f"/review-evidence/{evidence['token']}/{frame['name']}").headers['content-type']=='image/jpeg'
        report['checks'].append('real source decoding, distinct frame identity and image HTTP response')
        request=dict(token=evidence['token'],action='GIỮ',confirmed=True,reason='TEST FIXTURE mechanical confirmation',
                     cause='TEST FIXTURE, not a human transcription label',answers={'text':'đúng','role':'phụ đề','alignment':'khớp'})
        call('POST',base+'/cues/34/review-decision',dict(request,confirmed=False),409)
        call('POST',base+'/cues/34/review-decision',dict(request,answers={}),409)
        call('PATCH',base+'/cues/34',{'corrected_text':'fixture changed'})
        call('POST',base+'/cues/34/review-decision',request,409)
        report['checks'].append('reject missing confirmation/answers and stale source package')
        evidence=call('GET',base+'/cues/34/review-evidence').json();request['token']=evidence['token']
        event=call('POST',base+'/cues/34/review-decision',request).json()
        assert event['test_fixture'] and not event['eligible_for_recognition_training']
        assert event['review_status']=='người dùng xác nhận'
        assert call('POST',base+'/cues/34/review-decision',request).json()['id']==event['id']
        call('POST',base+f"/cue-history/{event['id']}/undo",status=409) # unchanged GIỮ has no content to undo
        report['checks'].append('idempotent receipt and test confirmations excluded from training')
        event_evidence=call('GET',base+'/cues/34/review-evidence').json()
        edited=call('POST',base+'/cues/34/review-decision',dict(request,token=event_evidence['token'],action='SỬA',text='fixture reviewed')).json()
        call('POST',base+f"/cue-history/{edited['id']}/undo")
        call('POST',base+f"/cue-history/{edited['id']}/undo",status=409)
        report['checks'].append('real DB edit, journal export, undo and refusal to overwrite later state')
        evidence=call('GET',base+'/cues/34/review-evidence?transition=3.0&extra=true').json()
        split=call('POST',base+'/cues/34/review-decision',dict(request,token=evidence['token'],action='CẮT',split_time=3.,text1='fixture one',text2='fixture two')).json()
        assert split['action']=='CẮT' and len(split['after'])==2
        call('POST',base+f"/cue-history/{split['id']}/undo")
        evidence=call('GET',base+'/cues/34/review-evidence?target_id=90').json()
        assert evidence['target'] and len(evidence['target']['frames'])==3
        merged=call('POST',base+'/cues/34/review-decision',dict(request,token=evidence['token'],action='GỘP',target_cue_id=90)).json()
        assert merged['action']=='GỘP' and len(merged['before'])==2
        call('POST',base+f"/cue-history/{merged['id']}/undo")
        report['checks'].append('split needs boundary evidence; merge has both sets of source frames; stable IDs restored')
        journal=out/'learning'/'corrections.jsonl';saved=journal.with_suffix('.saved')
        journal.rename(saved);journal.mkdir()
        try:
            response=call('PATCH',base+'/cues/34',{'corrected_text':'outbox fixture'})
            assert response.headers.get('X-OCR-Learning-Pending')=='1'
        finally:
            journal.rmdir();saved.rename(journal)
        assert call('GET',base+'/learning-review').json()['write_errors']==[]
        report['checks'].append('JSONL failure surfaced while DB history survives; retry exports outbox')
        for i in range(len(sources)):
            sample=call('GET',f'/api/projects/audit_learning_{i}/cues/34/review-evidence').json()
            assert sample['complete']
        report['source_packages_checked']=len(sources)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(headless=True)
            page=browser.new_page(viewport={'width':1466,'height':1000})
            page.on('pageerror',lambda error:report['page_errors'].append(str(error)))
            page.goto(url,wait_until='networkidle')
            page.locator('.btn-open-project[data-id="audit_learning_0"]').click()
            page.locator('[data-studio="ocr"]').click()
            page.locator('.btn-confirm-ocr').first.click()
            dialog=page.locator('.learning-dialog')
            dialog.locator('[data-status]').filter(has_text='Đã tải ảnh thật').wait_for(timeout=30000)
            assert dialog.locator('.learning-evidence img').count()>=6
            dialog.locator('[data-play]').click()
            page.wait_for_function("document.querySelector('[data-player]').readyState >= 2")
            assert 'source=original' in dialog.locator('[data-player]').get_attribute('src')
            page.screenshot(path=str(out/'review_desktop.png'))
            page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(out/'review_mobile.png'))
            dialog.locator('[data-close]').click()
            page.locator('#btn-ocr-history').click();page.locator('.learning-history').first.wait_for()
            page.screenshot(path=str(out/'history.png'))
            browser.close()
        assert not report['page_errors']
        report['checks'].append('real browser loads real API proof images and DB history; desktop/mobile, no mock routes')
        report['status']='passed_mechanical_audit'
    except BaseException as error:
        report.update(status='failed',error=repr(error));raise
    finally:
        (out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        client.close()
        process.terminate()
        try:process.wait(timeout=10)
        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        log.close()
    print(json.dumps(report,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
