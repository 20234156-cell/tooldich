import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_actual_js_refuses_unsupported_ai_option_before_submitting_job():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    source = Path('web/app.js').read_text(encoding='utf-8').split('const TASK_ACTION_LABELS')[0]
    script = source + r'''
let supported=false;const sent=[];
global.fetch=async(url,options)=>{
  sent.push([url,options.method]);
  const data=url==='/openapi.json'?{components:{schemas:{RunAllRequest:{properties:{skip_uncertain:{}}},VoiceoverRequest:{properties:supported?{repair_existing:{}}:{}}}}}:url==='/api/system/health'?{timing_repair_version:'cluster-budget-v3'}:{task_id:'new'};
  return {ok:true,text:async()=>JSON.stringify(data)};
};
(async()=>{
  let blocked=false;
  try{await API.runStep('p','voiceover',{repair_existing:true});}catch(e){blocked=e.message.includes('Backend');}
  const before=sent.filter(x=>x[1]==='POST').length;
  supported=true;
  await API.runAll('p',{repair_existing:true});
  console.log(JSON.stringify({blocked,before,posts:sent.filter(x=>x[1]==='POST').length}));
})();
'''
    run = subprocess.run([node, '-e', script], capture_output=True, text=True, encoding='utf-8', timeout=10, check=True)
    assert json.loads(run.stdout) == dict(blocked=True, before=0, posts=1)


@pytest.mark.parametrize('version,expected_posts',[(None,0),('source-grounded-recovery-v2',0),('cluster-budget-v1',0),('cluster-budget-v2',0),('cluster-budget-v3',1)])
def test_plain_auto_checks_loaded_budget_backend_even_without_repair_option(version,expected_posts):
    node=shutil.which('node')
    if not node:pytest.skip('Node required')
    source=Path('web/app.js').read_text(encoding='utf8').split('const TASK_ACTION_LABELS')[0]
    script=source+'\nconst version='+json.dumps(version)+r''';
let posts=0;
global.fetch=async(url,options)=>{
 if(options.method==='POST')posts++;
 const data=url==='/api/system/health'?{timing_repair_version:version}:{task_id:'new'};
 return {ok:true,text:async()=>JSON.stringify(data)};
};
(async()=>{
 let blocked=false;
 try{await API.runAll('p',{skip_uncertain:false});}catch(error){blocked=error.message.includes('Backend');}
 console.log(JSON.stringify({posts,blocked}));
})();
'''
    result=subprocess.run([node,'-e',script],capture_output=True,text=True,encoding='utf8',check=True,timeout=10)
    assert json.loads(result.stdout)==dict(posts=expected_posts,blocked=expected_posts==0)
