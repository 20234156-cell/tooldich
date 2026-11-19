import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

from src.ocr.base import Cue
from src.tts.budget_recovery import recover_measured
from src.tts.checkpoint import create_voice_manifest,voice_checkpoint_matches


def measured(tmp_path, seconds=.8):
    root=tmp_path/'staging';clips=root/'voiceover/clips';clips.mkdir(parents=True)
    cues=[Cue(index=1,start=1,end=2,text='你好',vi_text='Xin chào'),
          Cue(index=2,start=3,end=4,text='再见',vi_text='Tạm biệt')]
    rows=[]
    sr=22050
    for cue,duration in zip(cues,(seconds,.4)):
        path=clips/f'{cue.index}.wav'
        sf.write(path,.1*np.sin(np.arange(round(duration*sr))*.1),sr,subtype='PCM_16')
        cue.voice_audio_path=path.relative_to(root).as_posix()
        rows.append(dict(cue_index=cue.index,start=cue.start,end=cue.end,
            clip_path=cue.voice_audio_path,synthesis_rate='+15%',raw_duration=99,
            clip_duration=99,drift=99,tempo=1.,tempo_limit=1.13))
    sf.write(root/'voiceover/voiceover.wav',np.zeros(sr*5),sr)
    return root,cues,rows


def test_schedule_precedes_any_additional_tempo_and_manifest_binds_report(tmp_path):
    root,cues,rows=measured(tmp_path)
    with patch('src.tts.budget_recovery.apply_atempo',side_effect=AssertionError('unnecessary tempo')):
        result=recover_measured(root,tmp_path/'project',cues,rows)
    assert result['status']=='candidate_meets_timing'
    assert len(result['report']['phases'])==1
    assert result['timeline'][0]['clip_duration']==.8
    assert result['timeline'][0]['actual_start']==1
    manifest=create_voice_manifest(root,cues,'input')
    assert 'voiceover/budget_report.json' in manifest['files']
    (root/'voiceover/manifest.json').write_text(json.dumps(manifest),encoding='utf8')
    assert voice_checkpoint_matches(root,cues,'input')
    (root/'voiceover/budget_report.json').write_text('{}',encoding='utf8')
    assert not voice_checkpoint_matches(root,cues,'input')


def test_speed_steps_measure_real_wav_then_stop_at_first_complete_schedule(tmp_path):
    root,cues,rows=measured(tmp_path,1.08)
    result=recover_measured(root,tmp_path/'project',cues,rows)
    assert result['status']=='candidate_meets_timing'
    assert result['report']['phases'][0]['result']['status']=='source_window_infeasible'
    assert len(result['report']['phases'])>1
    assert result['timeline'][0]['tempo']>1
    assert all(1.15*r['tempo']<=1.3 for r in result['timeline'])
    master,sr=sf.read(root/'voiceover/voiceover.wav')
    for cue,row in zip(cues,result['timeline']):
        audio,_=sf.read(root/row['clip_path'])
        start=round(row['actual_start']*sr)
        np.testing.assert_array_equal(master[start:start+len(audio)],audio)
        assert len(audio)/sr==row['clip_duration']
        assert cue.vi_text in ('Xin chào','Tạm biệt')


def test_failed_input_reuses_measured_tempo_cache_and_preserves_staged_audio(tmp_path):
    root,cues,rows=measured(tmp_path,2.4)
    master=root/'voiceover/voiceover.wav'
    before=hashlib.sha256(master.read_bytes()).hexdigest()
    clip_before=[(root/r['clip_path']).read_bytes() for r in rows]
    first=recover_measured(root,tmp_path/'project',cues,rows)
    assert first['status']=='source_window_infeasible'
    attempts=first['report']['clusters'][0]['attempts']['1']
    assert len(attempts)==3
    assert attempts[-1]['total_speed']>=1.29  # do not get stuck retrying the rounded25% level
    assert not (root/'voiceover/budget_report.json').exists()
    assert hashlib.sha256(master.read_bytes()).hexdigest()==before
    assert [(root/r['clip_path']).read_bytes() for r in rows]==clip_before
    with patch('src.tts.budget_recovery.apply_atempo',side_effect=AssertionError('repeated failed variant')):
        second=recover_measured(root,tmp_path/'project',cues,rows)
    assert second['status']=='source_window_infeasible'
    assert all(a['cache_hit'] for c in second['report']['clusters']
               for attempts in c['attempts'].values() for a in attempts)


def test_actual_source_end_is_a_hard_boundary(tmp_path):
    root,cues,rows=measured(tmp_path)
    import pytest
    with pytest.raises(ValueError,match='utterances'):
        recover_measured(root,tmp_path/'project',cues,rows,source_duration_seconds=3.5)


def test_independent_original_block_stays_placed_when_other_block_fails(tmp_path):
    root,cues,rows=measured(tmp_path,2.4)
    with patch('src.tts.budget_recovery.verified_source_gaps',
            side_effect=lambda *args,**kwargs: {} if kwargs.get('speech_windows') else [(44100,66150)]):
        result=recover_measured(root,tmp_path/'project',cues,rows)
    assert result['status']=='source_window_infeasible'
    assert result['timeline'][1]['schedule_status']=='placed'
    assert result['timeline'][1]['actual_start']==3
    assert result['report']['clusters'][1]['trang_thai']=='dat'
    assert not (root/'voiceover/budget_report.json').exists()


def test_each_verified_borrow_is_logged_with_measured_extent(tmp_path):
    root,cues,rows=measured(tmp_path,1.08)
    with patch('src.tts.budget_recovery.verified_source_gaps',
            side_effect=lambda *args,**kwargs: {1:[(11025,55125)]} if kwargs.get('speech_windows') else []):
        result=recover_measured(root,tmp_path/'project',cues,rows)
    assert result['status']=='candidate_meets_timing'
    intervals=result['report']['clusters'][0]['muon_ngoai_cua_so']
    assert intervals and {x['cue_id'] for x in intervals}=={1}
    assert all(x['source_audio']=='verified_same_speaker' for x in intervals)
    assert abs(sum(x['milliseconds'] for x in intervals)-80)<.1
    assert result['timeline'][0]['tempo']==1


def test_production_voice_entry_uses_budget_path_without_text_repair(tmp_path,db_session):
    import io
    from tests.test_timing_repair import setup_project
    from src.database.models import Media
    from src.translation.timing_shortener import TimingShortener
    runner,cues=setup_project(tmp_path,db_session)
    source=tmp_path/'source.wav'
    sf.write(source,np.zeros(22050*8),22050)
    db_session.add(Media(project_id='repair_movie',file_path=str(source),duration=8))
    db_session.commit()
    cues[0].start=1;cues[0].end=2
    cues[1].start=3;cues[1].end=4
    runner.save_cues('repair_movie',cues)
    def speech(text,**kwargs):
        duration=1.08 if len(text.split())>4 else .4
        buffer=io.BytesIO()
        sf.write(buffer,.1*np.sin(np.arange(round(duration*24000))*.1),24000,format='WAV')
        return buffer.getvalue(),duration
    with patch('src.tts.edge.EdgeTTSProvider.synthesize',side_effect=speech), \
         patch.object(TimingShortener,'shorten',side_effect=AssertionError('unnecessary text edit')):
        result=runner.run_voiceover('repair_movie',rate='+15%',auto_fit=True,
                                   budgeted=True,shorten_to_fit=False)
    assert result['max_drift']<=1.5 and result['mean_drift']<=.3
    assert all(row['schedule_policy']=='cluster-budget-v1' for row in result['timeline'])
    assert runner.get_cues('repair_movie')[0].vi_text==cues[0].vi_text
    assert (tmp_path/'voiceover/budget_report.json').is_file()
    assert (tmp_path/'audit/voice_budget/latest.json').is_file()


def test_stale_or_unverified_source_evidence_cannot_authorize_borrowing(tmp_path):
    import pytest
    from src.tts.budget_recovery import verified_source_gaps
    source=tmp_path/'source';source.mkdir()
    path=source/'timing_evidence.json'
    path.write_text(json.dumps(dict(version=1,source_sha256='old',intervals=[])),encoding='utf8')
    with pytest.raises(ValueError,match='stale'):
        verified_source_gaps(tmp_path,'current',22050)
    path.write_text(json.dumps(dict(version=1,source_sha256='current',intervals=[dict(
        start=1,end=2,speech_absent=True,image_checked=False,protected_audio_absent=True)])),encoding='utf8')
    assert verified_source_gaps(tmp_path,'current',22050)==[]


def test_rejected_compact_variant_does_not_prevent_other_distinct_strategies(tmp_path,db_session):
    from tests.test_timing_repair import setup_project,fake_speech
    from src.database.models import Media
    from src.translation.timing_shortener import TimingShortener
    runner,cues=setup_project(tmp_path,db_session)
    source=tmp_path/'source.wav';sf.write(source,np.zeros(22050*8),22050)
    db_session.add(Media(project_id='repair_movie',file_path=str(source),duration=8));db_session.commit()
    tiers=[]
    def shorten(self,items,*args,**kwargs):
        tier=next(iter(self.constraints.values()))['variant_strategy']['tier'];tiers.append(tier)
        return [] if tier!='alternative_expression' else [items[0].model_copy(update={'vi_text':'Xin chào'})]
    with patch('src.tts.edge.EdgeTTSProvider.synthesize',side_effect=fake_speech), \
         patch.object(TimingShortener,'shorten',shorten), \
         patch('src.tts.source_units.recover_duplicate_units',return_value=[]), \
         patch('src.tts.source_windows.review_source_windows') as review_windows:
        result=runner.run_voiceover('repair_movie',auto_fit=True,budgeted=True,auto_merge_timing=True)
    review_windows.assert_not_called()
    assert tiers==['compact','restructure','alternative_expression']
    assert result['auto_fit_attempts']==3
    assert result['max_drift']<=1.5 and result['mean_drift']<=.3
    review=json.loads((tmp_path/'audit/meaning_review.json').read_text(encoding='utf8'))
    assert review['current'][0]['strong_shortening'] is True
    assert review['current'][0]['training_label'] is False
    assert (tmp_path/'audit/meaning_review.md').is_file()
