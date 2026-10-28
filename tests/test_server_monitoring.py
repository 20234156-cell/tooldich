from types import SimpleNamespace
from src.server import monitoring


def test_monitoring_reports_disk_failure_and_ram_pressure(monkeypatch,tmp_path):
    monkeypatch.setattr(monitoring.psutil,'virtual_memory',lambda:SimpleNamespace(total=8*1024**3,available=1024**3,percent=87.5))
    monkeypatch.setattr(monitoring.psutil,'cpu_percent',lambda interval:23)
    monkeypatch.setattr(monitoring.psutil,'cpu_count',lambda:8)
    monkeypatch.setattr(monitoring,'gpu_status',lambda:{'available':False,'devices':[],'message':'Unavailable'})
    def unavailable(path):raise OSError('not mounted')
    monkeypatch.setattr(monitoring.shutil,'disk_usage',unavailable)
    result=monitoring.host_status()
    assert result['cpu_percent']==23
    assert result['ram']['available_bytes']==1024**3
    assert result['disks'] and all(disk['free_bytes'] is None for disk in result['disks'])
    assert any(alert['section']=='tasks' for alert in result['alerts'])
    assert any(alert['section']=='storage' for alert in result['alerts'])
    assert len(result['disks'])==len({disk['root'] for disk in result['disks']})


def test_gpu_measurement_handles_timeout_and_caches(monkeypatch):
    calls=[]
    monkeypatch.setattr(monitoring,'_gpu_cache',(0,None))
    def timeout(*args,**kwargs):
        calls.append(kwargs)
        raise monitoring.subprocess.TimeoutExpired('nvidia-smi',2)
    monkeypatch.setattr(monitoring.subprocess,'run',timeout)
    first=monitoring.gpu_status()
    assert first['available'] is False
    assert monitoring.gpu_status()==first
    assert len(calls)==1
    assert calls[0]['timeout']==2


def test_gpu_csv_and_low_disk_alerts(monkeypatch):
    monkeypatch.setattr(monitoring,'_gpu_cache',(0,None))
    monkeypatch.setattr(monitoring.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=0,stdout='"GPU, Example", 4096, 512, 17\n'))
    result=monitoring.gpu_status()
    assert result['available']
    assert result['devices'][0]['name']=='GPU, Example'
    monkeypatch.setattr(monitoring.shutil,'disk_usage',lambda p:SimpleNamespace(total=100*1024**3,free=1024**3,used=99*1024**3))
    assert any(alert['section']=='cleanup' for alert in monitoring.host_status()['alerts'])


def test_admin_task_label_distinguishes_queued_from_running():
    import json
    import shutil
    import subprocess
    from pathlib import Path
    source=(Path(__file__).resolve().parents[1]/'web/server-client.js').read_text(encoding='utf-8')
    start=source.index("const state=task.state==='RUNNING'")
    expression=source[start:source.index(';info.append',start)]
    script="const values=[];for(const task of [{state:'QUEUED'},{state:'RUNNING'}]){"+expression+";values.push(state);}console.log(JSON.stringify(values));"
    result=subprocess.run([shutil.which('node'),'-e',script],capture_output=True,text=True,encoding='utf-8',check=True,timeout=10)
    assert json.loads(result.stdout)==['Đang chờ','Đang chạy']
