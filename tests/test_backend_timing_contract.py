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
  const data=url==='/openapi.json'?{components:{schemas:{VoiceoverRequest:{properties:supported?{repair_existing:{}}:{}}}}}:url==='/api/system/health'?{timing_repair_version:'source-grounded-recovery-v2'}:{task_id:'new'};
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
