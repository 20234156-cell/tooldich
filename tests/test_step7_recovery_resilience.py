"""Offline regression gates for shared translation/voice timing recovery."""
import json
from unittest.mock import patch
import pytest
from src.ocr.base import Cue
from src.translation.preflight import soft_budget
from src.translation.batch_translator import BatchTranslator
from src.translation.memory import TranslationMemory
from src.translation.timing_shortener import TimingShortener, MeaningVerifier
from src.translation.meaning_policy import get_meaning_policy
from src.jobs.logger import PipelineException, SystemErrorCodes
from tests.test_timing_repair import setup_project, fake_speech


def test_soft_budget_respects_speech_rate_and_dense_dialogue(tmp_path):
    cue = Cue(index=1,start=0,end=1.8,text='你好')
    following = Cue(index=2,start=1.85,end=3,text='再见')
    assert soft_budget(cue,4.6)['target_units'] > soft_budget(cue,4.)['target_units']
    assert soft_budget(cue,4.6,density_tight=True)['target_units'] < soft_budget(cue,4.6)['target_units']
    translator = BatchTranslator(speech_rate='+15%')
    payload = json.loads(translator._build_batches([cue,following],'natural')[0][2].split('sang tiếng Việt:\n')[1])
    budget = payload['to_translate'][0]['speech_budget']
    assert budget == soft_budget(cue,4.6,density_tight=True)
    args = (tmp_path,[cue,following],'natural',{},['fixture'],100)
    assert TranslationMemory(*args,speech_budget_rate=4.6).entries != TranslationMemory(*args).entries


def test_timing_shortener_retains_verified_batches_on_partial_verifier_failure(tmp_path):
    cues=[Cue(index=i,start=i*2,end=i*2+1,text='你好',vi_text='Xin chào bạn thân mến',translation_origin='ai') for i in range(1,7)]
    shorter=[c.model_copy(update={'vi_text':'Xin chào'}) for c in cues]
    def verify(self,*args,**kwargs):
        self.on_partial({c.index:{'translated':c.vi_text} for c in shorter[:3]})
        raise PipelineException(SystemErrorCodes.AI_ERROR,'503 / 504')
    with patch.object(BatchTranslator,'translate_cues',return_value=shorter), patch.object(MeaningVerifier,'translate_cues',verify):
        shortener=TimingShortener({c.index:{} for c in cues})
        result=shortener.shorten(cues,tmp_path)
    assert [c.index for c in result]==[1,2,3]
    assert shortener.warnings
    assert all(c.vi_text=='Xin chào bạn thân mến' for c in cues)


def test_voiceover_recovery_respects_wall_clock_budget(tmp_path,db_session,monkeypatch):
    runner,cues=setup_project(tmp_path,db_session)
    clock=[0.]
    monkeypatch.setattr('src.pipeline.orchestrator.time.monotonic',lambda:clock[0])
    calls=[]
    def slow(self,*args,**kwargs):
        calls.append(1)
        clock[0]+=91
        kwargs['should_continue']()
    with patch.object(TimingShortener,'shorten',slow), patch('src.tts.edge.EdgeTTSProvider.synthesize',side_effect=fake_speech):
        with pytest.raises(PipelineException) as exc:
            runner.run_voiceover('repair_movie',auto_fit=True)
    assert exc.value.code==SystemErrorCodes.TIMING_REVIEW_REQUIRED
    assert len(calls)==1
    assert any('ngân sách' in w for w in runner.get_state('repair_movie').get_stage('VOICE_READY').warnings)


@pytest.mark.parametrize('source,previous,candidate,negative',[
    ('不少','Có không ít người','Có nhiều người',False),
    ('没错','Không sai','Đúng vậy',False),
    ('没什么','Không sao','Có gì đâu',False),
    ('不得不走','Không thể không đi','Đành phải đi',False),
    ('不要去','Đừng đi','Đi đi',True),
    ('不是他','Không phải hắn','Là hắn',True),
    ('没有钱','Không có tiền','Có tiền',True),
    ('不能去','Không thể đi','Đi được',True),
    ('不会来','Không đến','Sẽ đến',True),
])
def test_meaning_policy_allows_idiomatic_non_negatives(source,previous,candidate,negative):
    assert ('negation' in get_meaning_policy('zh','vi').issues(source,previous,candidate))==negative
