import importlib.util
from pathlib import Path
from types import SimpleNamespace
import json
import pytest


def launcher():
    spec=importlib.util.spec_from_file_location('server_launcher_test',Path(__file__).parents[1]/'scripts/run_small_server.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_duplicate_ready_opens_existing_without_starting(monkeypatch,capsys):
    module=launcher()
    monkeypatch.setattr(module,'inspect_server',lambda port:{'state':'ready','url':f'http://127.0.0.1:{port}/'})
    opened=[]
    monkeypatch.setattr(module.webbrowser,'open',opened.append)
    monkeypatch.setattr(module.subprocess,'Popen',lambda *a,**k: (_ for _ in ()).throw(AssertionError('Must not spawn')))
    assert module.existing_server(8088,True)==0
    assert opened==['http://127.0.0.1:8088/']
    assert 'already ready' in capsys.readouterr().out


def test_duplicate_unhealthy_never_opens_unverified_service(monkeypatch,capsys):
    module=launcher()
    monkeypatch.setattr(module,'inspect_server',lambda port:{'state':'other_service'})
    monkeypatch.setattr(module.webbrowser,'open',lambda *a: (_ for _ in ()).throw(AssertionError('Unverified service')))
    assert module.existing_server(8088,True)==1
    assert 'No process or lock was removed' in capsys.readouterr().out


def test_status_does_not_initialize_database_or_start_server(monkeypatch,capsys):
    module=launcher()
    monkeypatch.setattr(module.sys,'argv',['launcher','--status'])
    monkeypatch.setattr(module,'inspect_server',lambda port:{'state':'ready'})
    import src.database.connection as database
    monkeypatch.setattr(database,'init_db',lambda: (_ for _ in ()).throw(AssertionError('Status must be read-only')))
    assert module.main()==0


@pytest.mark.parametrize('matching,enabled,state',[(True,True,'ready'),(True,False,'incompatible_backend'),(False,True,'other_service')])
def test_inspection_requires_workspace_process_and_server_protocol(monkeypatch,matching,enabled,state):
    import psutil
    import io
    module=launcher()
    connection=SimpleNamespace(status=psutil.CONN_LISTEN,laddr=SimpleNamespace(port=8088,ip='0.0.0.0'),pid=123)
    monkeypatch.setattr(psutil,'net_connections',lambda **kwargs:[connection])
    process=SimpleNamespace(cmdline=lambda:['python','main.py','serve'],cwd=lambda:str(module.BASE if matching else module.BASE.parent),pid=123,ppid=lambda:122)
    monkeypatch.setattr(psutil,'Process',lambda pid:process)
    class Opener:
        def open(self,*args,**kwargs):
            assert matching,'Other workspace must not be probed/opened'
            return io.BytesIO(json.dumps({'enabled':enabled,'setup_required':False}).encode())
    monkeypatch.setattr(module,'build_opener',lambda *args:Opener())
    assert module.inspect_server(8088)['state']==state
