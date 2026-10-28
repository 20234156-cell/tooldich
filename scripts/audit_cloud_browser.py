"""Chromium + real local API/queue/SQLite; simulated Google, isolated projects."""
import hashlib
import json
import threading
import time
from pathlib import Path


def main():
    from config.settings import settings
    root=Path('storage/audits/cloud_browser').resolve()
    root.mkdir(parents=True,exist_ok=True)
    settings.DATABASE_URL='sqlite:///'+(root/'audit.db').as_posix()
    settings.STORAGE_DIR=str(root/'projects')
    settings.GEMINI_API_KEY=''
    settings.API_TOKEN=''
    settings.RATE_LIMIT_ENABLED=False
    from src.api.app import app
    from src.cloud import archive,drive
    from src.database.connection import SessionLocal
    from src.database.models import Project,CloudConnection,CloudUpload
    from src.pipeline.state_machine import PipelineState,save_pipeline_state
    with SessionLocal() as db:
        db.query(CloudConnection).delete()
        db.query(CloudUpload).delete()
        if not db.get(Project,'cloud_audit'):
            db.add(Project(id='cloud_audit',title='Video kiểm tra <script>'))
        db.commit()
    path=settings.storage_path/'cloud_audit/output/video_vi.mp4'
    path.parent.mkdir(parents=True,exist_ok=True)
    original=b'isolated audit fixture - not a real video'*10000
    path.write_bytes(original)
    state=PipelineState();state.set_stage_status('VERIFIED','COMPLETED')
    save_pipeline_state(path.parents[1],state)
    credential=root/'synthetic_oauth.json'
    credential.write_text(json.dumps({'installed':{'client_id':'audit.apps.googleusercontent.com','client_secret':'synthetic-audit-secret'}}),encoding='utf-8')
    class FakeDrive:
        data=b''
        def new_id(self): return 'isolated_remote'
        def exists(self,_): return bool(self.data)
        def begin(self,_): return 'https://www.googleapis.com/upload/drive/v3/files?upload_id=synthetic'
        def send(self,session,total,offset=None,chunk=b''):
            if offset is not None:self.data+=chunk
            return len(self.data)
        def metadata(self,remote): return {'id':remote,'size':str(len(self.data)),'md5Checksum':hashlib.md5(self.data).hexdigest(),'trashed':False}
    fake=FakeDrive()
    archive.Drive=lambda:fake
    def forbid(*a,**kw): raise AssertionError('External provider traffic is forbidden in this UI audit')
    drive.request=forbid
    import uvicorn
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=8098,lifespan='off',log_level='error'))
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    errors=[]
    from playwright.sync_api import sync_playwright
    try:
        for _ in range(100):
            if server.started:break
            time.sleep(.1)
        with sync_playwright() as runtime:
            browser=runtime.chromium.launch(headless=True)
            page=browser.new_page(viewport={'width':1440,'height':1000})
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto('http://127.0.0.1:8098',wait_until='networkidle')
            page.locator('#btn-open-cloud').click()
            page.wait_for_function("document.getElementById('cloud-account').textContent.includes('Chưa kết nối')")
            assert page.locator('#cloud-connect').is_disabled()
            assert page.locator('#cloud-setup').get_attribute('open') is not None
            page.locator('#cloud-config-file').set_input_files(str(credential))
            page.locator('#cloud-import').click()
            page.wait_for_function("!document.getElementById('cloud-connect').disabled")
            assert 'synthetic-audit-secret' not in page.locator('#cloud-modal').inner_text()
            page.screenshot(path=str(root/'setup.png'))
            cfg=drive.config();cfg.update(account_id='audit_account',refresh_token='synthetic-refresh',email='audit@example.test')
            drive.save_config(cfg)
            page.locator('#cloud-refresh').click()
            page.wait_for_function("!document.getElementById('cloud-upload').disabled")
            page.locator('#cloud-project').select_option('cloud_audit')
            page.locator('#cloud-upload').click()
            page.wait_for_function("document.getElementById('cloud-library').textContent.includes('Đã xác minh tệp')",timeout=20000)
            assert fake.data==original and path.read_bytes()==original
            assert page.locator('#cloud-library script').count()==0
            page.screenshot(path=str(root/'desktop.png'))
            page.set_viewport_size({'width':390,'height':844})
            page.screenshot(path=str(root/'mobile.png'))
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.once('dialog',lambda dialog:dialog.dismiss())
            page.locator('[data-cloud-remove]').click()
            assert path.exists()
            page.once('dialog',lambda dialog:dialog.accept())
            page.locator('[data-cloud-remove]').click()
            page.wait_for_function("document.getElementById('cloud-library').textContent.includes('Đã dọn MP4')",timeout=20000)
            assert not path.exists()
            page.locator('#cloud-close').click()
            assert page.locator('#cloud-modal').is_hidden()
            browser.close()
        assert not errors,errors
        report={'passed':True,'javascript_errors':errors,
            'scope':'Real Chromium/local HTTP/SQLite/queue and Windows DPAPI. Simulated Google transport and QA fixture; no actual Google login, upload, or user project deletion.',
            'checks':['disconnected setup','import Desktop JSON','redaction','upload through actual queue','byte equality and checksum state','desktop/mobile','dismiss deletion','explicit isolated MP4 cleanup','close']}
        (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=True))
    finally:
        server.should_exit=True
        thread.join(timeout=10)


if __name__=='__main__':main()
