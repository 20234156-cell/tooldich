import json
from unittest.mock import Mock

import pytest

from config.settings import settings
from src.database.models import Project, CueReviewRevision
from src.jobs.logger import PipelineException, SystemErrorCodes
from src.ocr.base import Cue
from src.pipeline.orchestrator import PipelineOrchestrator
from src.pipeline.state_machine import PipelineState, ProjectState
from src.translation.batch_translator import BatchTranslator
from tests.test_translation_workers import make_pool, payload


@pytest.fixture
def movie(db_session):
    db_session.add(Project(id='skip_movie')); db_session.commit()
    runner = PipelineOrchestrator()
    runner.save_state('skip_movie', PipelineState(current_state=ProjectState.OCR_READY))
    runner.save_cues('skip_movie', [
        Cue(index=7, start=0, end=1, text='正常对白', confidence=99),
        Cue(index=752, start=1, end=2, text='抖首质', confidence=62),
        Cue(index=893, start=2, end=3, text='哎', confidence=57),
        Cue(index=900, start=3, end=4, text='语音', source_kind='stt'),
        Cue(index=1063, start=4, end=5, text='原创书', confidence=99,
            ocr_alternatives=[dict(text='啊', variant='scene_layout_review')]),
        Cue(index=1100, start=5, end=6, text='人工文字', corrected_text='已确认', confidence=50)])
    return runner


def test_skip_preserves_stable_survivors_and_records_omitted_content_in_db(movie, db_session):
    before = movie.get_cues('skip_movie')
    result = movie.skip_uncertain_cues('skip_movie')
    assert result == dict(skipped=3, cue_ids=[752, 893, 1063])
    assert movie.get_cues('skip_movie') == [before[i] for i in (0, 3, 5)]
    revision = db_session.query(CueReviewRevision).one().payload
    assert revision['action'] == 'BỎ QUA'
    assert revision['decided_by'] == 'agent' and revision['review_status'] != 'người dùng xác nhận'
    assert [cue['index'] for cue in revision['before']] == [752, 893, 1063]
    assert movie.skip_uncertain_cues('skip_movie')['skipped'] == 0
    assert 'uncertain_cues_omitted' in movie.get_state('skip_movie').get_stage('OCR_READY').degraded


def test_skip_db_failure_keeps_cues_and_playable_outputs(movie, monkeypatch):
    before = movie.get_cues('skip_movie')
    output = movie.get_project_dir('skip_movie') / 'voiceover/voiceover.wav'
    output.parent.mkdir(); output.write_bytes(b'previous audio')
    monkeypatch.setattr(movie, 'save_cues', Mock(side_effect=RuntimeError('DB failed')))
    with pytest.raises(RuntimeError, match='DB failed'):
        movie.skip_uncertain_cues('skip_movie')
    assert movie.get_cues('skip_movie') == before
    assert output.read_bytes() == b'previous audio'


def test_correction_between_selection_and_omission_is_not_deleted(movie, monkeypatch):
    before = movie.get_cues('skip_movie')
    corrected = [cue.model_copy(deep=True) for cue in before]
    corrected[1].corrected_text = '刚刚确认的文字'
    monkeypatch.setattr(movie, 'get_cues', Mock(side_effect=[before, corrected]))
    save = Mock()
    monkeypatch.setattr(movie, 'save_cues', save)
    with pytest.raises(ValueError, match='đã đổi'):
        movie.skip_uncertain_cues('skip_movie')
    save.assert_not_called()


def test_all_omitted_is_an_authoritative_empty_transcript(movie, monkeypatch):
    movie.omit_cues('skip_movie', [c.index for c in movie.get_cues('skip_movie')], 'Skip all selected')
    monkeypatch.setattr(movie, 'get_source_video_path', lambda _: movie.get_project_dir('skip_movie')/'source.mp4')
    extract = Mock(); monkeypatch.setattr(movie, 'run_ocr', extract)
    with pytest.raises(PipelineException) as raised:
        movie.run_all('skip_movie', skip_uncertain=True)
    assert raised.value.code == SystemErrorCodes.TRANSCRIPT_NOT_FOUND
    extract.assert_not_called()
    assert not movie.get_cues('skip_movie')


@pytest.mark.parametrize('skip', [False, True])
def test_translation_retries_then_only_omits_invalid_sibling(tmp_path, monkeypatch, skip):
    monkeypatch.setattr(settings, 'TRANSLATION_AUTO_GLOSSARY', False)
    cues = [Cue(index=7, start=0, end=1, text='正常对白'),
            Cue(index=50, start=2, end=3, text='另一句话')]
    translator = BatchTranslator(pool=make_pool(tmp_path, 1))
    translator.skip_invalid = skip
    calls = []
    def translate(prompt, **kwargs):
        ids = [row['id'] for row in payload(prompt)['to_translate']]
        calls.append(ids)
        return json.dumps([dict(id=i, text='Xin chào' if i == 7 else '') for i in ids])
    monkeypatch.setattr(translator, '_call_gemini_with_rotation', translate)
    if skip:
        result = translator.translate_cues(cues, tmp_path, publish_artifacts=False)
        assert result[0].vi_text == 'Xin chào' and result[1].vi_text is None
        assert translator.skipped_ids == {50}
        assert not list((tmp_path/'translation_cache').glob('*.json'))
    else:
        with pytest.raises(PipelineException):
            translator.translate_cues(cues, tmp_path, publish_artifacts=False)
    assert calls == [[7, 50], [50], [50]]


def test_translation_service_failure_is_never_treated_as_bad_cues(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'TRANSLATION_AUTO_GLOSSARY', False)
    translator = BatchTranslator(pool=make_pool(tmp_path, 1))
    translator.skip_invalid = True
    monkeypatch.setattr(translator, '_call_gemini_with_rotation', Mock(
        side_effect=PipelineException(SystemErrorCodes.AI_ERROR, 'service unavailable')))
    with pytest.raises(PipelineException):
        translator.translate_cues([Cue(index=7, start=0, end=1, text='正常对白')], tmp_path)
    assert not translator.skipped_ids


@pytest.mark.parametrize('skip,reason', [(True, 'MAX_TOKENS'), (False, 'MAX_TOKENS'),
                                       (True, 'Provider timeout')])
def test_singleton_token_limit_uses_omission_policy_but_timeout_still_fails(
        tmp_path, monkeypatch, skip, reason):
    from src.translation.batch_translator import SplitBatch
    monkeypatch.setattr(settings, 'TRANSLATION_AUTO_GLOSSARY', False)
    translator = BatchTranslator(pool=make_pool(tmp_path, 1))
    translator.skip_invalid = skip
    provider = Mock(side_effect=SplitBatch(reason))
    monkeypatch.setattr(translator, '_call_gemini_with_rotation', provider)
    cues = [Cue(index=50, start=0, end=1, text='这句话太长')]
    if skip and reason == 'MAX_TOKENS':
        result = translator.translate_cues(cues, tmp_path, publish_artifacts=False)
        assert translator.skipped_ids == {50} and result[0].vi_text is None
        assert provider.call_count == 3
        assert not list((tmp_path/'translation_cache').glob('*.json'))
    else:
        with pytest.raises(PipelineException):
            translator.translate_cues(cues, tmp_path, publish_artifacts=False)
        assert not translator.skipped_ids


@pytest.mark.parametrize('fail_db', [False, True])
def test_translation_and_omission_publish_as_one_db_transaction(movie, db_session, monkeypatch, fail_db):
    from types import SimpleNamespace as NS
    from config.settings import settings
    monkeypatch.setattr(settings, 'LOCAL_TRANSLATION_URL', None)
    monkeypatch.setattr(settings, 'AZURE_TRANSLATOR_KEY', None)
    original = [Cue(index=7, start=0, end=1, text='正常对白', confidence=99),
                Cue(index=50, start=1, end=2, text='另一句话', confidence=99)]
    movie.save_cues('skip_movie', original)
    movie.save_state('skip_movie', PipelineState(current_state=ProjectState.SOURCE_REVIEWED))
    def translate(cues, **kwargs):
        cues[0].vi_text = 'Xin chào'
        return cues
    translator = NS(translate_cues=translate, skipped_ids={50}, warnings=[], degraded=[], fallback_providers=[],
                    used_models={'offline': 'test'}, repaired_ids=set(), source_corrections=set(), model='offline')
    monkeypatch.setattr('src.pipeline.orchestrator.BatchTranslator', lambda **kwargs: translator)
    if fail_db:
        monkeypatch.setattr(movie, 'save_cues', Mock(side_effect=RuntimeError('DB failed')))
        with pytest.raises(RuntimeError, match='DB failed'):
            movie.run_translation('skip_movie', skip_invalid=True)
        assert movie.get_cues('skip_movie') == original
        assert not db_session.query(CueReviewRevision).all()
        assert movie.get_state('skip_movie').get_stage('TRANSLATED').status == 'FAILED'
    else:
        result = movie.run_translation('skip_movie', skip_invalid=True)
        assert [(cue.index, cue.vi_text) for cue in result] == [(7, 'Xin chào')]
        revision = db_session.query(CueReviewRevision).one().payload
        assert all(cue['vi_text'] is None for cue in revision['before'])
        assert [cue['index'] for cue in revision['after']] == [7]
        stage = movie.get_state('skip_movie').get_stage('TRANSLATED')
        assert stage.status == 'COMPLETED' and 'invalid_translation_omitted' in stage.degraded


def test_browser_rejects_old_backend_before_sending_skip_or_auto():
    import subprocess
    import shutil
    from pathlib import Path
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    source = Path('web/app.js').read_text(encoding='utf-8').split('const TASK_ACTION_LABELS')[0]
    script = source + """
let supported=false;let posts=0;
global.fetch=async(url,options)=>{
 if(options.method==='POST')posts++;
 const data=url==='/openapi.json'?{components:{schemas:{RunAllRequest:{properties:supported?{skip_uncertain:{}}:{}}}}}:url==='/api/system/health'?{timing_repair_version:'cluster-budget-v3'}:{task_id:'new'};
 return {ok:true,text:async()=>JSON.stringify(data)};
};
(async()=>{
 let blocked=0;
 for(const url of ['/api/projects/p/run-all','/api/projects/p/cues/skip-uncertain','/api/projects/p/auto-workflow/resume']){
  try{await API.request('POST',url,{skip_uncertain:true});}catch(error){if(error.message.includes('Backend'))blocked++;}
 }
 if(posts!==0||blocked!==3)throw Error('unsupported mutation sent');
 supported=true;await API.runAll('p',{});
 if(posts!==1)throw Error('supported Auto not sent');
 console.log('OK');
})();
"""
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, timeout=15, check=True)
    assert result.stdout.strip() == 'OK'
