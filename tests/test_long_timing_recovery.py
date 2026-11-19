from src.ocr.base import Cue


def test_long_video_repairs_all_finite_cohorts_without_repeating_same_input(monkeypatch):
    from config.settings import settings
    from src.translation.timing_shortener import repair_candidate_batches
    monkeypatch.setattr(settings,'TIMING_REPAIR_MAX_CUES',2)
    cues=[Cue(index=i,start=i,end=i+1,text='原文',vi_text='Một lời thoại quá dài',translation_origin='ai') for i in range(7)]
    rows=[dict(cue_index=i,raw_duration=3,fit_window=1,tempo_limit=1.3) for i in range(7)]
    attempted=set()
    batches=list(repair_candidate_batches(cues,rows,attempted=attempted))
    assert [list(c) for c,_ in batches]==[[0,1],[2,3],[4,5],[6]]
    for _,identities in batches:attempted.update(identities)
    assert list(repair_candidate_batches(cues,rows,attempted=attempted))==[]
    cues[3].vi_text='Lời đã sửa'
    assert [list(c) for c,_ in repair_candidate_batches(cues,rows,attempted=attempted)]==[[3]]


def test_actual_55_56_votes_are_refused_by_source_guard_not_ai_disagreement():
    from src.ocr.timing_duplicate import assess_pair_votes
    left=Cue(index=55,start=82.267,end=83.266,text='姚辰逸出来一1')
    right=Cue(index=56,start=83.267,end=83.766,text='姚辰逸出来一')
    text='姚辰逸出来一下'
    votes=[dict(decision='same_caption',crops=[text]*6,spoken=text,utterances=1)]*2
    assert assess_pair_votes(votes,['model1','model2'],left,right)==(None,'protected_source_mismatch')
    # Keep the numeric guard; no special case for this video or these IDs.
    left.text=right.text=text
    assert assess_pair_votes(votes,['model1','model2'],left,right)==(text,None)


def test_production_voice_recovery_covers_later_cohorts(db_session,tmp_path,monkeypatch):
    from unittest.mock import patch
    from config.settings import settings
    from tests.test_timing_repair import setup_project,fake_speech
    from src.translation.timing_shortener import TimingShortener
    runner,cues=setup_project(tmp_path,db_session)
    for c in cues:
        c.end=c.start+.5;c.vi_text='Xin chào bạn hôm nay hãy bước nhanh lên';c.translation_origin='ai'
    runner.save_cues('repair_movie',cues)
    monkeypatch.setattr(settings,'TIMING_REPAIR_MAX_CUES',1)
    monkeypatch.setattr(settings,'TIMING_REPAIR_MAX_PASSES',1)
    seen=[]
    def shorten(self,items,*a,**kw):
        seen.append(set(self.constraints))
        return [c.model_copy(update={'vi_text':'Xin chào'}) for c in items if c.index in self.constraints]
    with patch.object(TimingShortener,'shorten',shorten),patch('src.tts.edge.EdgeTTSProvider.synthesize',side_effect=fake_speech):
        result=runner.run_voiceover('repair_movie',auto_fit=True)
    assert len(seen)==2 and set.union(*seen)=={11,98}
    assert result['mean_drift']<=.3 and result['auto_fit_attempts']==1


def test_usage_database_failure_is_not_treated_as_rejected_shortening(db_session,tmp_path):
    import pytest
    from unittest.mock import patch
    from tests.test_timing_repair import setup_project,fake_speech
    from src.translation.timing_shortener import TimingShortener
    from src.ai.usage import UsagePersistenceError
    runner,_=setup_project(tmp_path,db_session)
    with patch.object(TimingShortener,'shorten',side_effect=UsagePersistenceError('usage DB unavailable')),patch('src.tts.edge.EdgeTTSProvider.synthesize',side_effect=fake_speech):
        with pytest.raises(UsagePersistenceError):runner.run_voiceover('repair_movie',auto_fit=True)
