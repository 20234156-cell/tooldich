import json
from pathlib import Path
import shutil
import subprocess

import pytest


def test_audio_and_auto_skip_roi_without_faking_database_state():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for actual JS workflow test')
    source = Path('web/app.js').read_text(encoding='utf-8')
    functions = source[source.index('function stageToStepNumber('):source.index('function setEnabled(')]
    script = """
let mode='stt';
const document={getElementById:()=>({value:mode})};
const STAGE_ORDER=['CREATED','SOURCE_READY','MEDIA_READY','ROI_READY','OCR_READY','SOURCE_REVIEWED'];
const currentWorkflow=null;
""" + functions + """
const results=[];
for(mode of ['stt','auto','hardsub']) {
 results.push([mode,stageToStepNumber('MEDIA_READY'),isStepComplete(3,'MEDIA_READY'),stageToStepNumber('SOURCE_READY')]);
}
console.log(JSON.stringify(results));
"""
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, check=True, timeout=10)
    assert json.loads(result.stdout) == [['stt', 4, True, 2], ['auto', 4, True, 2], ['hardsub', 3, False, 2]]
