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


def test_studio_open_routes_fresh_project_state_and_active_render():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    source = (Path(__file__).resolve().parents[1]/'web/app.js').read_text(encoding='utf-8')
    functions = source[source.index('async function openProject('):source.index('function startPolling(')]
    functions += source[source.index('function stageToStepNumber('):source.index('function subtitleConfigured(')]
    functions += source[source.index('const NEXT_ACTIONS'):source.index('function updateNextAction(')]
    harness = """
let currentProjectId, currentProject, ocrReviewFilter, currentROI, currentCues, currentWorkflow, lastHydratedStage;
const cuePages = {}, DOM = {detailVideo:{dataset:{}, style:{}}, detailTitle:{}, detailError:{},
  detailBar:{style:{}}, previewEmpty:{style:{}}, roiCanvas:{style:{}}};
const window = {dispatchEvent(){}};
const CustomEvent = function(){};
const showView = () => {};
const stopPolling = () => {};
const startPolling = () => {};
const loadSubtitleSettings = async () => {};
const updateWorkflowUI = () => {};
const switchStudioTab = tab => tabs.push(tab);
const tabs = [];
const projects = {done:{stage:'VERIFIED'}, voice:{stage:'VOICE_READY'}, ocr:{stage:'OCR_READY'},
  rendering:{stage:'TRANSLATION_REVIEWED', task:{state:'RUNNING',action:'render'}}};
const subtitleConfigured = () => true;
const ocrNeedsReview = cue => cue.pending;
const renderCuesTable = () => tabs.push(ocrReviewFilter);
const document = {getElementById:() => ({value:'original_only'})};
const refreshProjectData = async () => {
  currentProject = projects[currentProjectId];
  currentCues = [{pending: currentProjectId === 'ocr'}];
};
"""
    run = """
(async () => {
  for (const id of Object.keys(projects)) await openProject(id);
  console.log(JSON.stringify(tabs));
})().catch(e => { console.error(e); process.exitCode = 1; });
"""
    result = subprocess.run([node, '-e', harness+functions+run], check=True, capture_output=True,
                            text=True, encoding='utf-8', timeout=10)
    assert json.loads(result.stdout) == ['workflow', 'verify', 'workflow', 'render',
                                         'workflow', 'ocr', 'pending', 'workflow', 'render']
