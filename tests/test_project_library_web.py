"""Offline regression coverage for project search, pagination and batch deletion."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_project_library_search_pagination_and_partial_delete():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    source = (Path(__file__).resolve().parents[1] / "web/app.js").read_text(encoding="utf-8")
    block = source[source.index("const projectList ="):source.index("// System Health Poller")]
    script = r'''
const assert=require('node:assert/strict'), nodes=new Map(), calls=[];
class Control {
 constructor(){this.value='';this.handlers={};this.dataset={};this.classList={toggle(){}};this.disabled=false;}
 addEventListener(name,fn){this.handlers[name]=fn;}
 querySelectorAll(){return [];}
 setAttribute(){} removeAttribute(){}
 showModal(){this.open=true;} close(){this.open=false;}
}
const document={getElementById(id){if(!nodes.has(id))nodes.set(id,new Control());return nodes.get(id);},querySelectorAll(){return [];}};
const DOM={filterStatus:new Control(),projectRows:new Control(),btnRefresh:new Control()};
document.body={append(){}};
const currentProjectId=null,currentProject=null;
const window={dispatchEvent(){},refreshWorkspaceOverview:async()=>{},workspaceToast(){}};
const CustomEvent=class{};
const escapeHtml=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
const openProject=()=>{};
let records=Array.from({length:23},(_,i)=>({id:'proj_'+i,title:i===22?'Phim Đường về':'Video '+i,status:'CREATED'}));
const API={listProjects:async()=>records,deleteProject:async id=>{calls.push(id);if(id==='proj_1')throw Error('Đang chạy');records=records.filter(p=>p.id!==id);}};
''' + block + r'''
(async()=>{
await loadProjects();
assert.equal((DOM.projectRows.innerHTML.match(/<tr data-id=/g)||[]).length,10);
projectList.page=3;renderProjects();
assert.equal((DOM.projectRows.innerHTML.match(/<tr data-id=/g)||[]).length,3);
window.setProjectSearch('duong ve');
assert.match(DOM.projectRows.innerHTML,/proj_22/);
assert.equal((DOM.projectRows.innerHTML.match(/<tr data-id=/g)||[]).length,1);
assert.equal(projectList.page,1);
window.setProjectSearch('absent');assert.match(DOM.projectRows.innerHTML,/Không tìm thấy/);
window.setProjectSearch('');
projectList.selected.add('proj_0');projectList.selected.add('proj_1');
records[2].active_task={state:'QUEUED'};
requestProjectDeletion(['proj_2']);
assert.equal(projectUI('delete-dialog').open,undefined);
requestProjectDeletion([...projectList.selected]);
assert.equal(calls.length,0);assert.equal(projectUI('delete-dialog').open,true);
projectUI('delete-cancel').handlers.click();assert.equal(calls.length,0);
requestProjectDeletion([...projectList.selected]);
await projectUI('delete-confirm').handlers.click();
assert.deepEqual(calls,['proj_0','proj_1']);
assert.equal(projectList.selected.has('proj_0'),false);
assert.equal(projectList.selected.has('proj_1'),true);
assert.match(projectUI('list-feedback').textContent,/Đã xóa 1\/2/);
assert.match(projectUI('list-feedback').textContent,/Đang chạy/);
assert.equal(projectUI('delete-dialog').open,false);
assert.equal(projectList.busy,false);
projectList.page=99;renderProjects();assert.equal(projectList.page,3);
window.setProjectSearch('Video');assert.equal(projectList.selected.size,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True, encoding="utf-8", timeout=15)
