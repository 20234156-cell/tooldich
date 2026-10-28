import json
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize('mode', ['detail','dashboard','new','deleted','network','invalid','blocked'])
def test_reload_restores_only_saved_workspace(mode):
    node=shutil.which('node')
    if not node:
        pytest.skip('Node required')
    source=(Path(__file__).resolve().parents[1]/'web/app.js').read_text(encoding='utf-8')
    block=source[source.index('const workspaceLocationKey'):source.index('function showView(')]
    script='''
const calls=[];let currentProjectId=null,currentProject={};
const DOM={detailError:{textContent:''}};
let stored=JSON.stringify({view:['dashboard','new'].includes(MODE)?MODE:'detail',projectId:MODE==='invalid'?'../bad':'proj_test'});
const sessionStorage={getItem(){if(MODE==='blocked')throw Error('denied');return stored;},setItem(k,v){stored=v;}};
const showView=name=>{calls.push(name);rememberWorkspace(name);};
const stopPolling=()=>calls.push('stop');
const openProject=async id=>{currentProjectId=id;showView('detail');calls.push(['open',id]);if(MODE==='deleted'||MODE==='network')throw Object.assign(Error('failed'),{code:MODE==='deleted'?'HTTP_404':'NETWORK_ERROR'});};
''' .replace('MODE', json.dumps(mode)) + block + '''
restoreWorkspace().then(()=>console.log(JSON.stringify({calls,error:DOM.detailError.textContent,stored:JSON.parse(stored)})));
'''
    result=subprocess.run([node,'-e',script],check=True,capture_output=True,text=True,encoding='utf-8',timeout=10)
    d=json.loads(result.stdout)
    if mode=='detail':
        assert d['calls']==['detail',['open','proj_test']]
        assert d['stored']['projectId']=='proj_test'
    elif mode=='deleted':
        assert d['calls'][-1]=='dashboard'
        assert d['stored']['projectId'] is None
    elif mode=='network':
        assert d['error'] and 'dashboard' not in d['calls']
        assert d['stored']['projectId']=='proj_test'
    else:
        assert d['calls']==['new' if mode=='new' else 'dashboard']
    assert 'restoreWorkspace();' in source
