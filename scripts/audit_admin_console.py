"""Exercise the admin console with Chromium on a separate database/server."""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

import requests
from playwright.sync_api import sync_playwright

BASE=Path(__file__).resolve().parents[1]
root=BASE/'storage/audits'/('admin_console_'+time.strftime('%Y%m%dT%H%M%S'))
root.mkdir(parents=True)
with socket.socket() as sock:
    sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
url=f'http://127.0.0.1:{port}'
env=os.environ.copy()
env.update(SERVER_MODE='true',SERVER_AUTO_BACKUP_HOURS='0',DATABASE_URL='sqlite:///'+(root/'app.db').as_posix(),STORAGE_DIR=str(root/'projects'),SERVER_BACKUP_ROOT=str(root/'backups'),SERVER_BACKUP_DIRECTORY=str(root/'replicas'),ALLOWED_HOSTS='127.0.0.1,localhost',API_TOKEN='',RATE_LIMIT_ENABLED='false',PYTHONIOENCODING='utf-8')
report={'production_modified':False,'browser_errors':[],'ui_fixture_tasks_only':True}
log=(root/'server.log').open('w',encoding='utf-8')
proc=subprocess.Popen([sys.executable,'main.py','serve','--host','127.0.0.1','--port',str(port)],cwd=BASE,env=env,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
try:
    for _ in range(150):
        try:
            if requests.get(url+'/api/server/session',timeout=1).status_code==200:break
        except requests.RequestException:pass
        if proc.poll() is not None:raise RuntimeError('Isolated server failed')
        time.sleep(.2)
    password=secrets.token_urlsafe(24)
    requests.post(url+'/api/server/setup',json={'username':'audit_admin','password':password},timeout=10).raise_for_status()
    seed="from src.server import store; import time; " + "; ".join("store.put('task:console_"+state+"',dict(task_id='console_"+state+"',project_id='fixture_"+state+"',action='voiceover',state='"+state+"',owner='audit_admin',progress=20,created_at=time.time(),error_message='Fixture error' if '"+state+"'=='FAILED' else ''))" for state in ('QUEUED','FAILED','COMPLETED'))
    subprocess.run([sys.executable,'-c',seed],cwd=BASE,env=env,check=True,capture_output=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1440,'height':1000})
        page.on('pageerror',lambda error:report['browser_errors'].append(str(error)))
        page.goto(url,wait_until='domcontentloaded')
        page.locator('[name=username]').fill('audit_admin')
        page.locator('[name=password]').fill(password)
        page.locator('.server-login-form button[type=submit]').click()
        page.wait_for_function('window.workspaceServerUser?.role === "admin"')
        page.locator('.server-admin-launch').click()
        page.locator('#server-tab-overview[aria-selected=true]').wait_for()
        assert page.locator('.server-section:visible').count()==1
        assert 'CPU' in page.locator('#server-section-overview').inner_text()
        page.screenshot(path=str(root/'overview_desktop.png'))
        page.locator('#server-tab-storage').click()
        assert page.locator('#server-section-storage meter').count()>=1
        page.screenshot(path=str(root/'resources_desktop.png'))
        page.locator('#server-tab-tasks').click()
        rows=page.locator('.server-task-row:visible')
        assert rows.count()==3
        assert 'Đang chờ' in page.locator('.server-task-row').filter(has_text='fixture_QUEUED').inner_text()
        page.get_by_label('Lọc trạng thái tác vụ',exact=True).select_option('FAILED')
        assert rows.count()==1
        page.get_by_label('Tìm tác vụ',exact=True).fill('no-match')
        assert rows.count()==0
        page.get_by_label('Tìm tác vụ',exact=True).fill('')
        page.get_by_label('Lọc trạng thái tác vụ',exact=True).select_option('all')
        page.locator('#server-tab-accounts').click()
        page.locator('#server-section-accounts').get_by_role('button',name='Chỉnh sửa',exact=True).click()
        assert page.get_by_role('button',name='Lưu thay đổi',exact=True).is_visible()
        page.locator('.server-header-actions').get_by_role('button',name='↻ Làm mới',exact=True).click()
        page.locator('#server-tab-accounts[aria-selected=true]').wait_for()
        assert page.locator('.server-section:visible').count()==1
        for tab in ['sharing','devices','backups','cleanup','audit','overview']:
            page.locator('#server-tab-'+tab).click()
            assert page.locator('#server-section-'+tab).is_visible()
        page.locator('#server-tab-overview').focus()
        page.keyboard.press('ArrowRight')
        assert page.locator('#server-tab-storage').get_attribute('aria-selected')=='true'
        page.set_viewport_size({'width':390,'height':844})
        page.screenshot(path=str(root/'resources_mobile.png'))
        assert page.locator('.server-panel').evaluate('(e)=>e.scrollWidth<=e.clientWidth+1')
        page.locator('#server-tab-tasks').click()
        assert page.locator('.server-panel').evaluate('(e)=>e.scrollWidth<=e.clientWidth+1')
        page.screenshot(path=str(root/'tasks_mobile.png'))
        report.update(tabs=True,task_filter=True,queued_label=True,account_edit=True,refresh_keeps_section=True,keyboard_tabs=True,mobile_no_horizontal_overflow=True)
        browser.close()
    assert not report['browser_errors'],report['browser_errors']
    report['passed']=True
finally:
    proc.terminate()
    try:proc.wait(timeout=15)
    except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
    log.close()
    (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'report':str(root/'report.json'),**report},ensure_ascii=True))
