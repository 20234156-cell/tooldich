from unittest.mock import Mock, patch
import json
import subprocess
import shutil
from pathlib import Path
import numpy as np
import pytest
from fastapi.testclient import TestClient
from src.api.schemas import RunAllRequest
from src.pipeline.workflow_options import resolve_workflow
from src.ocr.rapidocr_engine import RapidOCREngine
from src.ocr.base import Cue
from src.jobs.logger import PipelineException


@pytest.mark.parametrize('mode,source,audio', [
    ('screen_subtitles','hardsub','original_only'), ('speech_subtitles','stt','original_only'),
    ('screen_dub','hardsub','mix'), ('speech_dub','stt','mix')])
def test_explicit_intent_cannot_silently_change_source_or_generate_unwanted_voice(mode,source,audio):
    plan=resolve_workflow(mode,'fast',extraction_mode='auto',audio_mode='voice_only')
    assert (plan.extraction_mode,plan.audio_mode,plan.encoder_preset)==(source,audio,'p4')


@pytest.mark.parametrize('profile,preset',[('fast','p4'),('balanced','p5'),('quality','p6'),(None,None)])
def test_profile_only_selects_encoding_not_qa(profile,preset):
    assert resolve_workflow(processing_profile=profile).encoder_preset==preset
    assert resolve_workflow(audio_mode='voice_only').audio_mode=='voice_only'


def test_reject_unknown_profile():
    with pytest.raises(ValueError):RunAllRequest(processing_profile='unsafe')
    with pytest.raises(ValueError):resolve_workflow('unknown')


def make_runner(db_session,tmp_path,kind='ocr'):
    from src.database.models import Project
    from src.pipeline.orchestrator import PipelineOrchestrator
    from src.pipeline.state_machine import PipelineState,ProjectState
    db_session.add(Project(id='profiles'));db_session.commit()
    runner=PipelineOrchestrator();runner.get_project_dir=lambda _:tmp_path
    runner.get_source_video_path=lambda _:tmp_path/'source.mp4'
    runner.save_state('profiles',PipelineState(current_state=ProjectState.TRANSLATION_REVIEWED))
    runner.save_cues('profiles',[Cue(index=1,start=0,end=1,text='你好',confidence=99,source_kind=kind,vi_text='Xin chào')])
    runner.run_voiceover=Mock();runner.run_render=Mock()
    report=Mock(passed=True);report.to_dict.return_value={'passed':True}
    runner.verify=Mock(return_value=report)
    return runner


def test_subtitles_skip_tts_and_profile_reaches_saved_renderer(db_session,tmp_path,monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings,'PERSONAL_MODE',True)
    runner=make_runner(db_session,tmp_path)
    runner.run_all('profiles',workflow_mode='screen_subtitles',processing_profile='quality')
    runner.run_voiceover.assert_not_called()
    cfg=runner.run_render.call_args.kwargs['cfg']
    assert cfg.audio_mode=='original_only' and cfg.encoder_preset=='p6'
    assert runner.get_subtitle_settings('profiles')['encoder_preset']=='p6'


def test_switching_source_does_not_reuse_wrong_cues_or_overwrite_them(db_session,tmp_path):
    runner=make_runner(db_session,tmp_path)
    before=runner.get_cues('profiles')
    with pytest.raises(PipelineException,match='khác chế độ'):
        runner.run_all('profiles',workflow_mode='speech_subtitles')
    assert runner.get_cues('profiles')==before
    runner.run_render.assert_not_called()


@pytest.mark.parametrize('old_kind,new_mode', [('ocr','stt'),('stt','hardsub')])
def test_custom_source_switch_blocks_before_approval(db_session,tmp_path,old_kind,new_mode):
    runner=make_runner(db_session,tmp_path,kind=old_kind)
    before=runner.get_cues('profiles')
    runner.approve_source_cues=Mock()
    with pytest.raises(PipelineException,match='khác chế độ'):
        runner.run_all('profiles',workflow_mode='custom',extraction_mode=new_mode)
    assert runner.get_cues('profiles')==before
    runner.approve_source_cues.assert_not_called()
    runner.run_voiceover.assert_not_called()
    runner.run_render.assert_not_called()


def test_queue_and_adapter_preserve_profile_for_resume(db_session):
    import src.api.app as api
    from src.database.models import Project
    from src.jobs.queue import QueuedTask
    from src.pipeline import auto_workflow
    db_session.add(Project(id='profiles_api'));db_session.commit()
    with patch.object(api.queue,'submit',return_value=QueuedTask('profile_task','profiles_api',None,'run-all')) as submit:
        response=TestClient(api.app).post('/api/projects/profiles_api/run-all',json={
            'workflow_mode':'speech_subtitles','processing_profile':'quality'})
    assert response.status_code==200
    assert submit.call_args.kwargs['workflow_mode']=='speech_subtitles'
    assert auto_workflow.read('profiles_api')['options']['processing_profile']=='quality'
    with patch('src.pipeline.orchestrator.orchestrator.run_all',return_value={}) as run:
        api._run_fast_hardsub_pipeline('profiles_api',workflow_mode='speech_subtitles',processing_profile='quality')
    assert run.call_args.kwargs['processing_profile']=='quality'


def box(x1,y1,x2,y2,text):
    return [[[x1,y1],[x2,y1],[x2,y2],[x1,y2]],text,.99]


@pytest.mark.parametrize('rows,suspicious',[
    ([box(5,5,45,25,'招牌'),box(80,5,135,25,'你好')],True),
    ([box(80,2,135,18,'你好'),box(80,30,140,46,'再见')],True),
    ([box(2,5,35,25,'招牌')],True),
    ([box(70,10,130,30,'你好')],False),
    ([box(60,10,95,30,'你'),box(98,10,140,30,'好')],False),
])
def test_same_size_scene_text_cannot_pass_from_high_confidence_alone(rows,suspicious):
    engine=RapidOCREngine();engine._engine=Mock(return_value=(rows,None))
    text,confidence=engine.recognize(np.zeros((50,200,3),dtype=np.uint8))
    assert text==''.join(r[1] for r in rows) and confidence==99
    assert bool(engine.last_alternatives)==suspicious
    cue=Cue(index=1,start=0,end=1,text=text,confidence=confidence,ocr_alternatives=engine.last_alternatives)
    from src.pipeline.orchestrator import PipelineOrchestrator
    if suspicious:
        with pytest.raises(PipelineException):PipelineOrchestrator._validate_source_review([cue])
    else:PipelineOrchestrator._validate_source_review([cue])


def test_browser_options_reach_both_single_and_batch():
    root=Path(__file__).resolve().parents[1]
    source=(root/'web/app.js').read_text(encoding='utf8')
    node=shutil.which('node')
    if not node:pytest.skip('Node required')
    subprocess.run([node,'--check',str(root/'web/app.js')],check=True,capture_output=True)
    fn=source[source.index('function readWorkflowOptions('):source.index('DOM.formNew.addEventListener("submit"')]
    script='const document={getElementById:id=>({value:id.endsWith("workflow-mode")?"speech_subtitles":id.endsWith("corner-logo")?"blur":"quality"})};'+fn+'console.log(JSON.stringify(readWorkflowOptions()));'
    result=subprocess.run([node,'-e',script],capture_output=True,text=True,check=True)
    assert json.loads(result.stdout)=={'workflow_mode':'speech_subtitles','processing_profile':'quality','corner_logo':'blur'}
    assert source.count('...readWorkflowOptions(),')==2


def test_reading_order_keeps_adjacent_words_in_order_despite_detector_jitter():
    from src.ocr.text_regions import reading_order
    rows=[box(100,9,140,29,'好'),box(50,10,90,30,'你'),
          box(100,39,140,59,'见'),box(50,40,90,60,'再')]
    assert [r[1] for r in reading_order(rows)]==['你','好','再','见']
    assert [r[1] for r in rows]==['好','你','见','再']


@pytest.mark.parametrize('fault',[None,'two_anchors','duplicate_anchors','caption_visible','centered','another_word','extra_box','low','over_band','manual','missing_view'])
def test_recurring_title_removal_requires_source_consensus_not_high_confidence(fault):
    from src.ocr.source_recheck import recurring_scene_label
    import copy
    cue=Cue(index=762,start=10,end=11,text='《武者惊天》',confidence=98.8,
            ocr_alternatives=[dict(variant='scene_layout_review',text='《武者惊天》')])
    row=dict(text=cue.text,confidence=99,box=[[2,2],[45,2],[45,12],[2,12]])
    observed=[dict(width=200,full_height=50,focused=[[],[]],full=[[copy.deepcopy(row)],[copy.deepcopy(row)]]) for _ in range(3)]
    verified=dict(decisions=[dict(cue_id=i,action='recovered',text='觉醒能力',scene_texts=[cue.text]) for i in [1,2,3]])
    if fault=='two_anchors':verified['decisions'].pop()
    if fault=='duplicate_anchors':verified['decisions'][2]['cue_id']=1
    if fault=='caption_visible':observed[0]['focused'][1]=[row]
    if fault=='centered':observed[0]['full'][0][0]['box']=[[80,2],[120,2],[120,12],[80,12]]
    if fault=='another_word':observed[1]['full'][0][0]['text']='字幕'
    if fault=='extra_box':observed[2]['full'][1].append(row)
    if fault=='low':observed[0]['full'][1][0]['confidence']=89
    if fault=='over_band':observed[0]['full'][0][0]['box']=[[2,25],[45,25],[45,35],[2,35]]
    if fault=='manual':cue.corrected_text='已确认'
    if fault=='missing_view':observed[0]['full'].pop()
    assert recurring_scene_label(cue,observed,(.4,.9),verified)==(fault is None)
    assert cue.confidence==98.8 and cue.text=='《武者惊天》'


def test_known_mixed_crops_do_not_crowd_out_single_line_anchor_budget(tmp_path,monkeypatch):
    from src.ocr.source_recheck import learn_band
    import cv2
    mixed=[Cue(index=i,start=i,end=i+1,text='混合文字',confidence=99,
               text_classification='mixed',image_path=f'{i}.png') for i in range(1,20)]
    clean=[Cue(index=i,start=i,end=i+1,text=f'字幕{i}',confidence=99,image_path=f'{i}.png') for i in range(20,23)]
    reads=[]
    def image(path):
        reads.append(Path(path).stem)
        return np.full((100,200,3),int(Path(path).stem),dtype=np.uint8)
    monkeypatch.setattr(cv2,'imread',image)
    engine=Mock();engine.read_boxes.side_effect=lambda image,**kw:[dict(text=f'字幕{int(image[0,0,0])}',confidence=99,box=[[50,30],[150,30],[150,70],[50,70]])]
    band,anchors=learn_band(mixed+clean,tmp_path,engine,lambda:None)
    assert band is not None and [a['cue_id'] for a in anchors]==[20,21,22]
    assert reads==['20','21','22']


@pytest.mark.parametrize('preset,expected',[('p4',('veryfast','21')),('p5',('fast','20')),('p6',('medium','18'))])
def test_cpu_profiles_change_real_encoder_arguments(tmp_path,monkeypatch,preset,expected):
    import src.pipeline.render as renderer
    from src.pipeline.roi import ROIBox
    source=tmp_path/'source.mp4';source.write_bytes(b'isolated media identity')
    monkeypatch.setattr(renderer,'get_ffmpeg_bin',lambda:Path('ffmpeg'))
    monkeypatch.setattr(renderer,'get_ffprobe_bin',lambda:Path('ffprobe'))
    probe=Mock(stdout=json.dumps({'streams':[{'codec_type':'video','width':200,'height':100}], 'format':{'duration':1}}))
    monkeypatch.setattr(renderer.subprocess,'run',Mock(return_value=probe))
    launch=Mock(side_effect=RuntimeError('captured encoder command'))
    monkeypatch.setattr(renderer.subprocess,'Popen',launch)
    with pytest.raises(RuntimeError,match='captured encoder'):
        renderer.render_one_pass(source,None,ROIBox(x=0,y=.7,width=1,height=.2),[],tmp_path/'out.mp4',
            renderer.RenderConfig(use_nvenc=False,audio_mode='original_only',encoder_preset=preset))
    command=launch.call_args.args[0]
    assert (command[command.index('-preset')+1],command[command.index('-crf')+1])==expected


def test_high_average_cannot_hide_one_uncertain_negation_box():
    engine=RapidOCREngine()
    rows=[([[10+i*20,10],[28+i*20,10],[28+i*20,30],[10+i*20,30]],
           text, .2 if i==2 else .99) for i,text in enumerate(['我','并','不','同','意'])]
    engine._engine=Mock(return_value=(rows,None))
    text,confidence=engine.recognize(np.zeros((50,140,3),dtype=np.uint8))
    assert text=='我并不同意' and confidence>70
    assert any(a.get('variant')=='low_confidence_box' for a in engine.last_alternatives)
    cue=Cue(index=203,start=0,end=1,text=text,confidence=confidence,ocr_alternatives=engine.last_alternatives)
    from src.pipeline.orchestrator import PipelineOrchestrator
    with pytest.raises(PipelineException):PipelineOrchestrator._validate_source_review([cue])


def test_nonempty_low_confidence_preprocessing_keeps_raw_disagreement():
    engine=RapidOCREngine()
    def read(image):
        return ([([[30,10],[100,10],[100,40],[30,40]],
                  '本真' if image.ndim==2 else '草率了', .605 if image.ndim==2 else .999)],None)
    engine._engine=Mock(side_effect=read)
    text,confidence=engine.recognize(np.zeros((72,180,3),dtype=np.uint8))
    assert text=='本真' and confidence==60.5
    assert any(a.get('variant')=='raw_low_confidence' and a['text']=='草率了' for a in engine.last_alternatives)
    assert engine._engine.call_count==2
