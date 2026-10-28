"""Create first admin locally, with a private password file; never print secrets."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reset-admin',metavar='USERNAME',help='Explicit local password recovery; revokes existing sessions')
    args=parser.parse_args()
    os.chdir(BASE)
    from config.settings import settings
    from src.database.connection import init_db
    from src.server import store
    init_db()
    admins=[u for _,u in store.rows('user:') if u['enabled'] and u['role']=='admin']
    if admins and not args.reset_admin:
        print(json.dumps({'ready':True,'admins':[u['username'] for u in admins],'message':'Existing admin retained; no password changed.'}))
        return
    username=args.reset_admin or 'server_admin'
    if not args.reset_admin and store.get('user:'+username):
        username='server_admin_'+secrets.token_hex(3)
    password=secrets.token_urlsafe(24)
    root=settings.storage_path.parent/'private'
    root.mkdir(parents=True,exist_ok=True)
    file=root/(f'server_login_{time.strftime("%Y%m%dT%H%M%S")}_{secrets.token_hex(3)}.txt')
    try:
        with file.open('x',encoding='utf-8') as stream:
            stream.write(f'username={username}\npassword={password}\n\nOpen the server application, log in, and store this password securely.\nDo not paste this file into chat or upload it to Git.\n')
        if os.name=='nt':
            sid=subprocess.run(['powershell.exe','-NoProfile','-Command','[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value'],check=True,capture_output=True,text=True).stdout.strip()
            if not sid.startswith('S-1-'):raise RuntimeError('Cannot determine Windows file owner')
            subprocess.run(['icacls',str(file),'/inheritance:r','/grant:r','*'+sid+':(F)'],check=True,capture_output=True)
        else:
            file.chmod(0o600)
        user=store.save_user(username,password,'admin')
        store.audit(username,'local-admin-preparation')
        print(json.dumps({'ready':True,'username':user['username'],'private_login_file':str(file.resolve()),'password_printed':False},ensure_ascii=True))
    except Exception:
        # If DB creation succeeded, retain the only copy of the initial credential.
        current=store.get('user:'+username)
        if not current or not store.password_matches(password,current['password']):file.unlink(missing_ok=True)
        raise


if __name__=='__main__':main()
