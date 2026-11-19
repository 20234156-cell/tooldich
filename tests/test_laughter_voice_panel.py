"""Read-only API + actual JS voice panel must not count a silent placeholder."""
import json
import shutil
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient
from src.api.app import app
from src.database.models import Project, Transcript
from src.ocr.base import Cue
from src.pipeline.orchestrator import PipelineOrchestrator


def items():
    return [dict(index=1,start=0,end=1,text='你好',confidence=99,vi_text='Xin chào',
                 voice_audio_path='voiceover/clips/clip_0001.wav',voice_duration=.8),
            dict(index=88,start=2,end=3,text='嘿嘿嘿',confidence=96,vi_text='Hì hì hì',
                 text_classification='subtitle',caption_role='nonverbal_laughter',
                 voice_audio_path='voiceover/clips/clip_0088.wav',voice_duration=0)]


def test_cue_api_discloses_subtitle_only_without_mutating_db(db_session):
    db_session.add(Project(id='role_view'));db_session.commit()
    PipelineOrchestrator().save_cues('role_view',[Cue(**c) for c in items()])
    before=db_session.query(Transcript).filter_by(project_id='role_view').one().segments
    response=TestClient(app).get('/api/projects/role_view/cues')
    assert response.status_code==200
    result=response.json()
    assert result[1]['subtitle_only_reason']=='verified_nonverbal_laughter_caption'
    assert not result[0].get('subtitle_only_reason')
    db_session.expire_all()
    assert db_session.query(Transcript).filter_by(project_id='role_view').one().segments==before


def test_voice_panel_counts_real_voice_and_hides_laughter_controls():
    source=(Path(__file__).resolve().parents[1]/'web/app.js').read_text(encoding='utf8')
    block=source[source.index('function renderVoiceTable('):source.index('async function runTranslateTask(')]
    data=items();data[1]['subtitle_only_reason']='verified_nonverbal_laughter_caption'
    script='''const currentProjectId='role_view';
const DOM={voiceCuesList:{innerHTML:'',querySelectorAll:()=>[]},voiceSummaryText:{textContent:''}};
const cuePage=()=>currentCues.map((c,i)=>({c,i}));
const formatDuration=x=>String(x);const escapeHtml=x=>String(x);
const currentCues='''+json.dumps(data)+ ';\n'+block+'''
renderVoiceTable();console.log(JSON.stringify({summary:DOM.voiceSummaryText.textContent,html:DOM.voiceCuesList.innerHTML}));'''
    result=subprocess.run([shutil.which('node'),'--eval',script],capture_output=True,text=True,encoding='utf8',check=True,timeout=10)
    rendered=json.loads(result.stdout)
    assert 'Đã tạo giọng 1/1' in rendered['summary']
    assert '1 chú thích chỉ hiện phụ đề' in rendered['summary']
    assert rendered['html'].count('btn-play-voice')==1
    assert rendered['html'].count('btn-regen-voice')==1
    assert 'Chỉ hiện phụ đề' in rendered['html']
