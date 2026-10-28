"""Single-instance Windows/LAN launcher. Never kills a process occupying the port."""
import argparse
import ipaddress
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import json
import webbrowser
from urllib.request import Request, build_opener, ProxyHandler

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))


def private_addresses():
    import psutil
    addresses=[]
    for entries in psutil.net_if_addrs().values():
        for entry in entries:
            if entry.family==socket.AF_INET:
                address=ipaddress.ip_address(entry.address)
                if address.is_private and not address.is_loopback and not address.is_link_local:
                    addresses.append(entry.address)
    return sorted(set(addresses))


def port_available(host,port):
    with socket.socket() as sock:
        try:sock.bind((host,port))
        except OSError:return False
    return True


def inspect_server(port):
    """Read-only local readiness plus process identity; never trust just an open port."""
    import psutil
    result={'url':f'http://127.0.0.1:{port}/','state':'unavailable','port':port}
    try:
        listeners=[c for c in psutil.net_connections(kind='tcp')
                   if c.status==psutil.CONN_LISTEN and c.laddr.port==port]
        for connection in listeners:
            if not connection.pid:continue
            try:
                process=psutil.Process(connection.pid)
                command=process.cmdline()
                if Path(process.cwd()).resolve()!=BASE or not any(Path(arg).name=='main.py' for arg in command) or 'serve' not in command:
                    continue
                result.update(backend_pid=process.pid,launcher_pid=process.ppid(),bind=connection.laddr.ip)
                break
            except (psutil.Error,OSError):continue
        if 'backend_pid' not in result:
            result['state']='other_service' if listeners else 'unavailable'
            return result
        opener=build_opener(ProxyHandler({}))
        with opener.open(Request(result['url']+'api/server/session',headers={'Accept':'application/json'}),timeout=3) as response:
            data=json.loads(response.read(65536))
        if data.get('enabled') is True and isinstance(data.get('setup_required'),bool):
            result.update(state='ready',setup_required=data['setup_required'])
        else:result['state']='incompatible_backend'
    except Exception as exc:
        result.update(state='not_ready',error=type(exc).__name__)
    return result


def existing_server(port,open_browser=False):
    status=inspect_server(port)
    print(json.dumps(status,ensure_ascii=True),flush=True)
    if status['state']=='ready':
        print('Server is already ready. Opening the existing application; no second server is needed.',flush=True)
        if open_browser:webbrowser.open(status['url'])
        return 0
    print('Launcher lock is held, but a ready server from this workspace could not be verified. It may still be starting.',flush=True)
    print('Wait briefly and run --status. Inspect storage/maintenance/small_server.log and server_launcher_errors.log. No process or lock was removed.',flush=True)
    return 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8088)
    parser.add_argument('--local',action='store_true',help='Bootstrap/test on localhost only')
    parser.add_argument('--allow-sleep',action='store_true',help='Do not request Windows to stay awake')
    parser.add_argument('--status',action='store_true',help='Read-only readiness and process identity; never starts a server')
    parser.add_argument('--open',action='store_true',help='Open an already running verified server in the browser')
    args=parser.parse_args()
    if not 1<=args.port<=65535:parser.error('Invalid port')
    os.chdir(BASE)
    if args.status:
        status=inspect_server(args.port)
        print(json.dumps(status,ensure_ascii=True),flush=True)
        return 0 if status['state']=='ready' else 1
    from config.settings import settings
    from src.database.connection import init_db
    from src.server import store
    init_db()
    folder=settings.storage_path.parent/'maintenance'
    folder.mkdir(parents=True,exist_ok=True)
    lockfile=(folder/'small_server.lock').open('a+b')
    try:
        lockfile.seek(0)
        if os.name=='nt':
            import msvcrt
            if lockfile.read(1)==b'':lockfile.write(b'0');lockfile.flush()
            lockfile.seek(0);msvcrt.locking(lockfile.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(lockfile,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        lockfile.close()
        return existing_server(args.port,args.open)
    admins=[u for _,u in store.rows('user:') if u['role']=='admin' and u['enabled']]
    host='127.0.0.1' if args.local or not admins else '0.0.0.0'
    addresses=private_addresses()
    environment=os.environ.copy()
    environment.update(SERVER_MODE='true',ALLOWED_HOSTS=','.join(['127.0.0.1','localhost',socket.gethostname(),*addresses]),PYTHONIOENCODING='utf-8')
    print(f'Open http://127.0.0.1:{args.port}/ on this server.',flush=True)
    if not admins:
        print('Create the first administrator locally. Restart this launcher afterwards to serve LAN clients.',flush=True)
    elif host=='0.0.0.0':
        for address in addresses:print(f'LAN address: http://{address}:{args.port}/',flush=True)
        print('Allow this Python/port only on the trusted private network in Windows Firewall; no Internet port forwarding.',flush=True)
    child=None
    try:
        if os.name=='nt' and not args.allow_sleep:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
        while True:
            if not port_available(host,args.port):
                print('Port occupied. No process was killed; stop your existing server or choose another port.',flush=True)
                return 1
            logpath=folder/'small_server.log'
            if logpath.exists() and logpath.stat().st_size>10*1024**2:
                # Bounded operational log, not an archive of project/audit data.
                logpath.write_text('',encoding='utf-8')
            with logpath.open('a',encoding='utf-8') as log:
                child=subprocess.Popen([sys.executable,'main.py','serve','--host',host,'--port',str(args.port)],
                    cwd=BASE,env=environment,stdout=log,stderr=log,
                    creationflags=getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0))
                started=time.monotonic()
                code=child.wait()
            if code==0:return 0
            if time.monotonic()-started<10:
                print('Startup failed; inspect storage/maintenance/small_server.log. No restart loop.',flush=True)
                return code
            print('Server exited unexpectedly; restarting in 5 seconds.',flush=True)
            time.sleep(5)
    except KeyboardInterrupt:
        if child and child.poll() is None:
            try:
                child.send_signal(signal.CTRL_BREAK_EVENT if os.name=='nt' else signal.SIGINT)
                child.wait(timeout=30)
            except (OSError,subprocess.TimeoutExpired):
                child.terminate();child.wait(timeout=10)
        return 0
    finally:
        if os.name=='nt' and not args.allow_sleep:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
        lockfile.close()


if __name__=='__main__':
    raise SystemExit(main())
