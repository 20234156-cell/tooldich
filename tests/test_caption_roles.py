from copy import deepcopy

import pytest

from src.ocr.base import Cue
from src.ocr.caption_roles import binding, coverage, digest, select_candidates, verified_lyrics


def evidence():
    cue = Cue(index=82, start=200., end=202., text='忘了初心常在痛快去', confidence=99,
              text_classification='subtitle')
    vote = dict(frame_reads=[cue.text]*3, visual_role='lyrics', audio_kind='singing',
                has_spoken_dialogue=False, spoken_zh='', decision='subtitle_only',
                uncertain=False, reason='Singing only')
    proof = dict(version='caption-role-v1', binding=binding(cue), source_sha256='a'*64,
                 audio_sha256='b'*64,
                 visual=[dict(frame=i, seconds=200+i*.1, image_sha256=str(i)*64,
                              readings=[[dict(text=cue.text, confidence=99)]]*2)
                         for i in (1, 2, 3)],
                 votes=[dict(status='observed', actual_model=m, vote=deepcopy(vote))
                        for m in ('model-a', 'model-b')])
    proof['sha256'] = digest(proof)
    return cue, proof


def seal(proof):
    proof['sha256'] = digest({k:v for k,v in proof.items() if k != 'sha256'})


def test_exact_evidence_and_union_coverage():
    cue, proof = evidence()
    assert verified_lyrics(cue, proof)
    assert coverage(0, 10, [(1, 4), (2, 6), (8, 9)]) == .6
    selected, deferred = select_candidates([cue.model_copy(update={'index':i}) for i in range(10)], [])
    assert len(selected) == 8 and [r['cue_id'] for r in deferred] == [8, 9]
    # Older scans lack text-layout classification. Strong role proof is still
    # mandatory; a mixed layout is never eligible for this single-line proof.
    assert verified_lyrics(cue.model_copy(update={'text_classification':'unclassified'}),proof)
    assert not verified_lyrics(cue.model_copy(update={'text_classification':'mixed'}),proof)


@pytest.mark.parametrize('change', ['source', 'timing', 'kind', 'hash', 'model', 'read',
                                   'confidence', 'uncertain', 'bool_string', 'dialogue',
                                   'music_only', 'frame', 'stale_audio', 'timeout'])
def test_fail_closed(change):
    cue, proof = evidence()
    if change == 'source': cue.text += '不'
    elif change == 'timing': cue.end += .1
    elif change == 'kind': cue.source_kind = 'stt'
    elif change == 'hash': proof['source_sha256'] = 'unbound'
    elif change == 'model': proof['votes'][1]['actual_model'] = 'model-a'
    elif change == 'read': proof['visual'][1]['readings'][0][0]['text'] = '别的文字'
    elif change == 'confidence': proof['visual'][1]['readings'][0][0]['confidence'] = 89.99
    elif change == 'uncertain': proof['votes'][1]['vote']['uncertain'] = True
    elif change == 'bool_string': proof['votes'][1]['vote']['has_spoken_dialogue'] = 'false'
    elif change == 'dialogue': proof['votes'][1]['vote']['has_spoken_dialogue'] = True
    elif change == 'music_only': proof['votes'][1]['vote']['audio_kind'] = 'music'
    elif change == 'frame': proof['visual'][1]['frame'] = 1
    elif change == 'stale_audio': proof['audio_sha256'] = ''
    elif change == 'timeout': proof['votes'][1] = dict(status='failed', error='timeout')
    seal(proof)
    assert not verified_lyrics(cue, proof)


def test_failed_provider_does_not_override_success_or_disagreement():
    cue, proof = evidence()
    proof['votes'].append(dict(status='failed', error='503'))
    seal(proof)
    assert verified_lyrics(cue, proof)
    contrary = deepcopy(proof['votes'][0])
    contrary['vote'].update(decision='speak', audio_kind='speech', visual_role='dialogue',
                            has_spoken_dialogue=True, spoken_zh=cue.text)
    proof['votes'].append(contrary)
    seal(proof)
    assert not verified_lyrics(cue, proof)


def test_vad_false_negative_is_only_a_candidate_not_an_exemption():
    cue, _ = evidence()
    selected, _ = select_candidates([cue], [])
    assert selected and not verified_lyrics(cue, {'vad_coverage':0})
    for updated in ({'is_approved':True}, {'translation_origin':'manual'},
                    {'confidence':89}, {'corrected_text':'人改过的字幕'}):
        assert select_candidates([cue.model_copy(update=updated)], []) == ([], [])


@pytest.mark.parametrize('bad', [None, [], 'proof', {'votes':['bad']}, {'version':'caption-role-v1'}])
def test_malformed_evidence_never_crashes_or_exempts(bad):
    cue, _ = evidence()
    assert not verified_lyrics(cue,bad)


def test_lyrics_speech_builder_and_cache_bind_proof(tmp_path):
    from src.tts.speech_text import subtitle_only_reason, spoken_text
    from src.pipeline.orchestrator import PipelineOrchestrator
    from src.tts.timeline import AudioTimelineBuilder
    from src.tts.cache import TTSCache
    from unittest.mock import Mock
    import soundfile as sf
    import numpy as np
    cue, proof = evidence()
    cue.caption_role = 'background_lyrics'
    cue.caption_role_proof = proof
    cue.vi_text = 'Chớ quên tấm lòng thuở ban đầu'
    assert subtitle_only_reason(cue) == 'verified_background_lyrics_caption'
    assert spoken_text(cue) == ''
    provider = Mock(sample_rate=24000,max_concurrency=1,batch_size=1)
    provider.name = 'fixture'
    result = AudioTimelineBuilder(provider=provider,cache=TTSCache(tmp_path/'cache')).synthesize_cues(
        [cue],tmp_path,voice='fixture',rate='+0%')
    provider.synthesize.assert_not_called()
    assert result['timeline'] == [] and cue.voice_duration == 0
    assert not np.any(sf.read(tmp_path/cue.voice_audio_path)[0])
    runner = PipelineOrchestrator()
    before = runner._voice_input_hash([cue],None,'+0%','+0Hz')
    cue.caption_role_proof['votes'][1]['actual_model'] = 'same-source-changed'
    assert runner._voice_input_hash([cue],None,'+0%','+0Hz') != before
    assert spoken_text(cue) == cue.vi_text


def test_translation_cannot_rewrite_verified_lyric_source_or_reuse_dialogue_memory():
    import json
    from src.translation.quality import inspect_response
    from src.translation.batch_translator import parse_translation_response
    from src.translation.memory import TranslationMemory
    cue, proof = evidence()
    original = TranslationMemory(None,[cue],'natural',{},['model'],100).entries[cue.index]
    cue.caption_role = 'background_lyrics'
    cue.caption_role_proof = proof
    locked = TranslationMemory(None,[cue],'natural',{},['model'],100).entries[cue.index]
    assert locked != original
    row = dict(id=cue.index,translated='Lời nhạc đầy đủ',source_corrected='不一样的来源')
    valid, issues = inspect_response(json.dumps([row]),[cue],parse_translation_response)
    assert not valid and issues == {cue.index:'verified_source_changed'}
    row['source_corrected'] = cue.text
    assert not inspect_response(json.dumps([row]),[cue],parse_translation_response)[1]


def test_all_additive_default_history_versions_and_meaningful_proof():
    from src.ocr.learning import transcript_hash_matches
    cue, proof = evidence()
    row = cue.model_dump()
    for dropped in ((), ('caption_role',), ('caption_role_proof',), ('caption_role','caption_role_proof')):
        legacy = [{k:v for k,v in row.items() if k not in dropped}]
        expected = digest(legacy)
        # learning.digest uses the same serializer except allow_nan protection.
        from src.ocr.learning import digest as history_digest
        expected = history_digest(legacy)
        assert transcript_hash_matches([row],expected)
        changed = dict(row,caption_role_proof=proof)
        assert not transcript_hash_matches([changed],expected)


def test_hybrid_intent_is_explicit_and_does_not_change_other_modes():
    from src.pipeline.workflow_options import resolve_workflow
    from src.api.schemas import RunAllRequest
    assert RunAllRequest(workflow_mode='hybrid_dub').workflow_mode == 'hybrid_dub'
    plan = resolve_workflow('hybrid_dub')
    assert plan.role_review and plan.audio_mode == 'mix' and plan.extraction_mode == 'auto'
    for mode in ('custom','screen_dub','speech_dub','screen_subtitles','speech_subtitles'):
        assert not resolve_workflow(mode).role_review


def test_real_db_history_cache_and_manual_source_edit(db_session,tmp_path,monkeypatch):
    from unittest.mock import Mock
    from src.database.models import Project, CueReviewRevision
    from src.pipeline.orchestrator import PipelineOrchestrator
    from src.pipeline.state_machine import PipelineState, ProjectState
    from src.pipeline.roi import ROIBox
    from src.ocr import caption_role_review as service
    cue, proof = evidence()
    video = tmp_path/'source.mp4'
    video.write_bytes(b'isolated source identity fixture')
    import hashlib
    source_hash = hashlib.sha256(video.read_bytes()).hexdigest()
    proof['source_sha256'] = source_hash
    seal(proof)
    db_session.add(Project(id='roles'));db_session.commit()
    runner = PipelineOrchestrator()
    runner.get_project_dir = lambda _:tmp_path
    runner.get_source_video_path = lambda _:video
    roi = ROIBox(x=.05,y=.85,width=.9,height=.12)
    runner.get_roi_set = lambda _:Mock(get_primary_subtitle_roi=lambda:roi)
    runner.save_cues('roles',[cue])
    runner.save_state('roles',PipelineState(current_state=ProjectState.SOURCE_REVIEWED))
    after = cue.model_copy(update=dict(caption_role='background_lyrics',caption_role_proof=proof))
    def review(*args,**kwargs):
        directory = args[3];directory.mkdir(parents=True)
        report = dict(source_sha256=source_hash,resolved=1,decisions=[dict(cue_id=82,status='lyrics')],deferred=[])
        (directory/'report.json').write_text('{}')
        return [after],report
    mocked = Mock(side_effect=review)
    monkeypatch.setattr(service,'review',mocked)
    import src.pipeline.orchestrator as module
    monkeypatch.setattr(module.VideoSubFinderAdapter,'_recognizer',lambda *a:Mock())
    monkeypatch.setattr(module.VideoSubFinderAdapter,'release_ocr',lambda *a:None)
    runner.run_caption_role_review('roles')
    assert runner.get_cues('roles')[0].caption_role == 'background_lyrics'
    assert db_session.query(CueReviewRevision).count() == 1
    # Translation/voice publication must not spend another role-review request.
    stored = runner.get_cues('roles');stored[0].vi_text='Dịch đủ lời nhạc';stored[0].translation_origin='ai'
    runner.save_cues('roles',stored)
    assert runner.run_caption_role_review('roles')['status'] == 'cached'
    assert mocked.call_count == 1
    runner.update_cue('roles',82,{'end':201.9})
    changed = runner.get_cues('roles')[0]
    assert changed.caption_role == 'unclassified' and changed.caption_role_proof is None


def test_final_qa_rejects_stale_source_proof(tmp_path):
    from unittest.mock import patch,Mock
    from src.pipeline.verify import verify_project_output
    import json,hashlib
    cue, proof = evidence()
    cue.caption_role='background_lyrics';cue.caption_role_proof=proof;cue.vi_text='Lời nhạc'
    source=tmp_path/'source.mp4';source.write_bytes(b'source')
    output=tmp_path/'output.mp4';output.write_bytes(b'output')
    proof['source_sha256']=hashlib.sha256(source.read_bytes()).hexdigest();seal(proof)
    probe=dict(streams=[{'codec_type':'video'},{'codec_type':'audio'}],format={'duration':'205'})
    def verify(mode='mix'):
        with patch('src.pipeline.verify.run_checked_media',return_value=Mock(stdout=json.dumps(probe),returncode=0)):
            return verify_project_output(tmp_path,source,output,cues_data=[cue.model_dump()],audio_mode=mode)
    assert not any('lời nhạc' in e for e in verify().errors)
    assert any('voice_only' in e for e in verify('voice_only').errors)
    source.write_bytes(b'changed source')
    assert any('lời nhạc' in e for e in verify().errors)


def test_provider_rate_wait_returns_none_but_request_still_runs(monkeypatch):
    from unittest.mock import Mock
    from google import genai
    from src.ocr.caption_role_review import request_vote
    from src.ai.key_pool import key_pool
    import src.ai.usage as usage
    client=Mock()
    context=Mock();context.__enter__=Mock(return_value=client);context.__exit__=Mock(return_value=False)
    monkeypatch.setattr(genai,'Client',Mock(return_value=context))
    monkeypatch.setattr(key_pool,'get_next_key',Mock(return_value=Mock(id='key',key='test-secret')))
    monkeypatch.setattr(key_pool,'wait_translation_turn',Mock(return_value=None))
    monkeypatch.setattr(key_pool,'record_success',Mock())
    monkeypatch.setattr(key_pool,'record_failure',Mock())
    generate=Mock(return_value=Mock(model_version='actual-model',text='{}'))
    monkeypatch.setattr(usage,'generate_logged',generate)
    row=request_vote([], 'requested-model','test-project')
    assert row['status']=='observed' and row['actual_model']=='actual-model'
    assert generate.call_count==1
    key_pool.record_failure.assert_not_called()


def test_production_review_requires_actual_two_votes_and_preserves_input(tmp_path,monkeypatch):
    import numpy as np,hashlib
    from unittest.mock import Mock
    import src.ocr.caption_role_review as service
    from src.pipeline.roi import ROIBox
    cue, expected=evidence()
    video=tmp_path/'source.mp4';video.write_bytes(b'video')
    source_hash=hashlib.sha256(video.read_bytes()).hexdigest()
    monkeypatch.setattr(service,'speech_segments',lambda *a: ([],{'source':source_hash}))
    def audio(video,path,*a):path.write_bytes(b'actual target audio fixture')
    monkeypatch.setattr(service,'_audio',audio)
    class Capture:
        index=0
        def get(self,prop):
            if prop==service.cv2.CAP_PROP_FPS:return 10.
            if prop==service.cv2.CAP_PROP_POS_FRAMES:return self.index+1
            if prop==service.cv2.CAP_PROP_POS_MSEC:return self.index*100.
        def release(self):pass
    capture=Capture()
    monkeypatch.setattr(service.cv2,'VideoCapture',lambda _:capture)
    import src.ocr.frame_reader as reader
    def read(self,index):
        capture.index=index
        return True,np.full((100,200,3),255,dtype=np.uint8)
    monkeypatch.setattr(reader.FrameReader,'read',read)
    votes=iter(expected['votes'])
    monkeypatch.setattr(service,'request_vote',lambda *a:next(votes))
    engine=Mock();engine.read_boxes.return_value=[dict(text=cue.text,confidence=99)]
    after,report=service.review(video,ROIBox(x=0,y=0,width=1,height=1),[cue],tmp_path/'audit',engine,'test')
    assert cue.caption_role=='unclassified' and cue.caption_role_proof is None
    assert report['resolved']==1 and report['provider_calls']==2
    assert verified_lyrics(after[0],after[0].caption_role_proof)


def test_vad_cache_requires_full_scan_identity_and_bounded_chunks(tmp_path,monkeypatch):
    import json
    import numpy as np
    import soundfile as sf
    from unittest.mock import Mock
    import faster_whisper.vad as vad
    from src.ocr import caption_role_review as service
    video=tmp_path/'source.mp4';video.write_bytes(b'source identity')
    def audio(video,path):sf.write(path,np.zeros(int(61.5*16000)),16000)
    monkeypatch.setattr(service,'_audio',audio)
    seen=[]
    def spans(samples,*a,**kw):
        seen.append(len(samples))
        return [{'start':0,'end':len(samples)}]
    monkeypatch.setattr(vad,'get_speech_timestamps',spans)
    first,identity=service.speech_segments(video,tmp_path)
    assert len(seen)==2 and max(seen)<=61*16000
    assert service.speech_segments(video,tmp_path)==(first,identity)
    assert len(seen)==2
    path=tmp_path/'vad.json';saved=json.loads(path.read_text());saved['processed_frames']=0
    path.write_text(json.dumps(saved))
    service.speech_segments(video,tmp_path)
    assert len(seen)==4
    video.write_bytes(b'changed source')
    service.speech_segments(video,tmp_path)
    assert len(seen)==6
    assert not list(tmp_path.glob('source_vad_*.wav'))
