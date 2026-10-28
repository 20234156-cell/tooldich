"""Offline regression for overlapping project filters; no browser or network."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_latest_project_filter_wins_even_when_old_response_finishes_last():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    source = (Path(__file__).resolve().parents[1] / 'web/app.js').read_text(encoding='utf-8')
    function = source[source.index('let projectListGeneration = 0;'):source.index('DOM.btnRefresh.addEventListener("click", async')]
    script = '''
const pending=[];
const API={listProjects:()=>new Promise(resolve=>pending.push(resolve))};
const DOM={filterStatus:{value:''},projectRows:{}};
let rendered;
const renderProjects=rows=>rendered=rows;
const window={dispatchEvent:()=>{}};
const CustomEvent=function(){};
const escapeHtml=String;
''' + function + '''
(async()=>{
 const old=loadProjects(); DOM.filterStatus.value='VERIFIED';const recent=loadProjects();
 pending[1](['new']); await recent;
 pending[0](['old']); await old;
 if(rendered[0]!=='new')throw Error('old response overwrote selected filter');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    subprocess.run([node, '-e', script], check=True, capture_output=True, timeout=10)
