"""Real isolated HTTP and Chromium audit of server invitations/login."""
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
root=BASE/'storage/audits'/('server_invitation_'+time.strftime('%Y%m%dT%H%M%S'))
root.mkdir(parents=True)
with socket.socket() as sock:
    sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
url=f'http://127.0.0.1:{port}'
env=os.environ.copy()
env.update(SERVER_MODE='true',SERVER_AUTO_BACKUP_HOURS='0',DATABASE_URL='sqlite:///'+(root/'app.db').as_posix(),STORAGE_DIR=str(root/'projects'),ALLOWED_HOSTS=','.join(['127.0.0.1','localhost',*socket.gethostbyname_ex(socket.gethostname())[2]]),API_TOKEN='',RATE_LIMIT_ENABLED='false',PYTHONIOENCODING='utf-8')
report={'production_modified':False,'real_http':True,'browser_errors':[]}
log=(root/'server.log').open('w',encoding='utf-8')
proc=subprocess.Popen([sys.executable,'main.py','serve','--host','127.0.0.1','--port',str(port)],cwd=BASE,env=env,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
try:
    for _ in range(150):
        try:
            if requests.get(url+'/api/server/session',timeout=1).status_code==200:break
        except requests.RequestException:pass
        if proc.poll() is not None:raise RuntimeError('Audit server failed')
        time.sleep(.2)
    password=secrets.token_urlsafe(24)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        admin=browser.new_page(viewport={'width':1440,'height':1000})
        admin.on('pageerror',lambda e:report['browser_errors'].append(str(e)))
        admin.goto(url,wait_until='domcontentloaded')
        admin.screenshot(path=str(root/'login_desktop.png'))
        form=admin.locator('.server-login-form')
        form.locator('input[name=username]').fill('audit_admin')
        form.locator('input[name=password]').fill(password)
        form.locator('input[autocomplete=new-password]').last.fill(password)
        form.locator('button[type=submit]').click()
        admin.wait_for_function('window.workspaceServerUser?.role === "admin"')
        report['setup_ui']=True
        admin.locator('.server-admin-launch').click()
        admin.locator('#server-section-sharing').wait_for()
        form=admin.locator('.server-invite-form')
        if not form.locator('button[type=submit]').is_enabled():
            raise RuntimeError('No LAN interface discovered for invitation UI audit')
        form.locator('input').fill('invited_reader')
        form.locator('select').select_option('viewer')
        form.locator('button[type=submit]').click()
        output=admin.locator('.server-invite-result input')
        output.wait_for()
        from urllib.parse import urlsplit,parse_qs
        issued=output.input_value();fragment=parse_qs(urlsplit(issued).fragment)
        invitation={'token':fragment['invite'][0],'username':fragment['username'][0]}
        report['admin_creates_invitation_in_ui']=True
        report['sharing_has_lan_candidate']=True
        from urllib.parse import urlencode
        invite_url=url+'/#'+urlencode({'invite':invitation['token'],'username':invitation['username']})
        guest=browser.new_page(viewport={'width':390,'height':844})
        guest.on('pageerror',lambda e:report['browser_errors'].append(str(e)))
        guest.goto(invite_url,wait_until='domcontentloaded')
        guest.locator('.server-login-form').wait_for()
        guest.screenshot(path=str(root/'login_mobile.png'))
        report['fragment_removed']=not guest.evaluate('location.hash')
        form=guest.locator('.server-login-form')
        report['invited_name_readonly']=form.locator('[name=username]').get_attribute('readonly') is not None
        form.locator('[name=password]').fill(password)
        form.locator('.server-password-toggle').click()
        report['show_password']=form.locator('[name=password]').get_attribute('type')=='text'
        form.locator('input[autocomplete=new-password]').last.fill(password+'x')
        form.locator('button[type=submit]').click()
        guest.wait_for_function("document.querySelector('#server-panel-message').textContent.length>0")
        report['confirmation_mismatch_blocked']=guest.evaluate('!window.workspaceServerUser')
        form.locator('input[autocomplete=new-password]').last.fill(password)
        form.locator('button[type=submit]').click()
        guest.wait_for_function('window.workspaceServerUser?.role === "viewer"')
        report['invitation_real_activation']=True
        guest.locator('.server-admin-launch').click()
        guest.locator('.server-section-nav').wait_for()
        report['viewer_has_no_admin_sections']=guest.locator('#server-section-accounts').count()==0
        report['mobile_no_horizontal_overflow']=guest.evaluate('document.documentElement.scrollWidth<=innerWidth')
        guest.screenshot(path=str(root/'viewer_mobile.png'))
        guest.locator('#server-panel-close').click()
        response=guest.evaluate("""async()=>{const s=await(await fetch('/api/server/session')).json();return (await fetch('/api/server/logout',{method:'POST',headers:{'X-CSRF-Token':s.csrf}})).status}""")
        report['logout']=response==200
        guest.reload(wait_until='domcontentloaded')
        guest.locator('.server-login-form').wait_for()
        form=guest.locator('.server-login-form');form.locator('[name=username]').fill('invited_reader');form.locator('[name=password]').fill(password);form.locator('button[type=submit]').click()
        guest.wait_for_function('window.workspaceServerUser?.role === "viewer"')
        report['regular_login_after_activation']=True
        report['success']=all(v for k,v in report.items() if isinstance(v,bool) and k!='production_modified') and not report['browser_errors']
        browser.close()
except Exception as exc:
    report['success']=False;report['error']=str(exc)
finally:
    proc.terminate()
    try:proc.wait(15)
    except subprocess.TimeoutExpired:proc.kill();proc.wait(5)
    log.close()
    (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'report':str(root/'report.json'),**report},ensure_ascii=True))
if not report.get('success'):sys.exit(1)
