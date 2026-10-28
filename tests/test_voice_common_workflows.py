import io
import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch

import numpy as np
import pytest
import soundfile as sf
from src.ocr.base import Cue
from src.tts.cache import TTSCache
from src.tts.timeline import AudioTimelineBuilder
from src.tts.vieneu import VOICE_ID

@pytest.mark.parametrize('count', [200, 500])
def test_many_cues_resume_edit_one_change_rate_and_corrupt_cache(tmp_path, count):
    output = io.BytesIO()
    sf.write(output, np.sin(np.arange(4800)*.1)*.2, 48000, format='WAV')
    class Local:
        name = 'vieneu'
        sample_rate = 48000
        max_concurrency = 1
        cache_version = 'fixture-model-reference'
        calls = 0
        def synthesize(self, text, **kwargs):
            self.calls += 1
            return output.getvalue(), .1
    local = Local()
    cues = [Cue(index=3+i*7, start=i*.5, end=i*.5+.4, vi_text=f'Câu số {i+1}') for i in range(count)]
    cache = TTSCache(tmp_path/'cache')
    with patch('src.tts.registry.provider_for_voice', return_value=local):
        builder = AudioTimelineBuilder(cache=cache, max_concurrency=8)
        def run(name, **kwargs):
            result = builder.synthesize_cues(cues, tmp_path/name, voice=VOICE_ID, **kwargs)
            assert [r['cue_index'] for r in result['timeline']] == [c.index for c in cues]
            assert len(list((tmp_path/name/'voiceover/clips').glob('*.wav'))) == count
        run('cold')
        assert local.calls == count
        run('resume')
        assert local.calls == count
        cues[count//2].vi_text = 'Đây là câu vừa sửa lại'
        run('one_edit')
        assert local.calls == count+1
        run('rate_change', rate='+10%')
        assert local.calls == 2*count+1
        # Corrupt a matching active-rate entry, never trust a nonempty file by path.
        from src.tts.normalizer_vi import normalize_vietnamese_text
        from src.tts.speech_text import spoken_text
        key = cache.compute_key('vieneu', VOICE_ID, normalize_vietnamese_text(spoken_text(cues[0])), '+10%', '+0Hz', 48000, local.cache_version)
        cache.put(key, b'broken audio')
        run('corruption_recovery', rate='+10%')
        assert local.calls == 2*count+2

@pytest.mark.parametrize('mode', ['success','http_error','unavailable'])
def test_voice_selector_actual_js_preserves_choice_and_no_duplicates(mode):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    source = (Path(__file__).resolve().parents[1]/'web/voices.js').read_text(encoding='utf-8')
    script = '''
const vm=require('vm');
const select={value:'vi-VN-HoaiMyNeural',options:[{value:'vi-VN-NamMinhNeural'},{value:'vi-VN-HoaiMyNeural'}],appendChild(o){this.options.push(o);}};
const document={getElementById:()=>select,createElement:()=>({})};
const errors=[];const console={error:(...args)=>errors.push(args)};
const mode=MODE;
const fetch=async()=>({ok:mode!=='http_error',status:503,json:async()=>({voices:mode==='unavailable'?[]:[{id:'vieneu:ngoc_huyen_review_phim',name:'Ngọc Huyền Review Phim',provider:'vieneu'}]})});
const context=vm.createContext({document,console,fetch});
(async()=>{await vm.runInContext(SOURCE,context);await vm.runInContext(SOURCE,context);
if(select.value!=='vi-VN-HoaiMyNeural')throw Error('selection reset');
if(select.options.length!==(mode==='success'?3:2))throw Error('unready/duplicate voices');
if(mode==='success'&&!select.options[2].textContent.includes('Ngọc Huyền'))throw Error('wrong label');
if(mode==='http_error'&&errors.length!==2)throw Error('API failure hidden');
})().catch(e=>{process.stderr.write(e.stack);process.exitCode=1;});
'''.replace('MODE', json.dumps(mode)).replace('SOURCE', json.dumps(source))
    subprocess.run([node, '-e', script], check=True, capture_output=True, text=True, encoding='utf-8', timeout=10)

@pytest.mark.parametrize('save_fails', [False, True])
def test_single_local_cue_persists_db_and_keeps_previous_audio_on_db_failure(db_session, monkeypatch, save_fails):
    from src.database.models import Project
    from src.pipeline.orchestrator import PipelineOrchestrator
    from unittest.mock import Mock
    runner = PipelineOrchestrator()
    db_session.add(Project(id='local_one_cue'))
    db_session.commit()
    cues = [Cue(index=7,start=0,end=2,vi_text='Câu cần đọc'), Cue(index=83,start=3,end=5,vi_text='Giữ câu khác')]
    runner.save_cues('local_one_cue', cues)
    root = runner.get_project_dir('local_one_cue')
    clips = root/'voiceover'/'clips'
    clips.mkdir(parents=True)
    old = clips/'clip_0007.wav'
    old.write_bytes(b'previous playable content')
    other = clips/'clip_0083.wav'
    other.write_bytes(b'other clip unchanged')
    before = [c.model_dump() for c in runner.get_cues('local_one_cue')]
    payload = io.BytesIO()
    sf.write(payload, np.sin(np.arange(4800)*.1)*.2, 48000, format='WAV')
    provider = Mock()
    provider.synthesize.return_value = (payload.getvalue(), .1)
    monkeypatch.setattr('src.tts.registry.provider_for_voice', lambda voice: provider)
    if save_fails:
        monkeypatch.setattr(runner, 'save_cues', Mock(side_effect=RuntimeError('database unavailable')))
        with pytest.raises(RuntimeError, match='database unavailable'):
            runner.synthesize_single_cue('local_one_cue', 7, voice=VOICE_ID)
        assert old.read_bytes() == b'previous playable content'
        assert [c.model_dump() for c in runner.get_cues('local_one_cue')] == before
    else:
        runner.synthesize_single_cue('local_one_cue', 7, voice=VOICE_ID)
        persisted = runner.get_cues('local_one_cue')
        assert persisted[0].voice_duration == .1
        assert persisted[0].voice_audio_path.endswith('clip_0007.wav')
        assert persisted[1].model_dump() == before[1]
        assert sf.info(old).samplerate == 48000
    assert other.read_bytes() == b'other clip unchanged'

@pytest.mark.parametrize('sample', [0., float('nan'), float('inf')])
def test_single_cue_rejects_silent_or_nonfinite_audio_before_publication(tmp_path, sample):
    from src.pipeline.orchestrator import PipelineOrchestrator
    from src.jobs.logger import PipelineException
    from unittest.mock import Mock
    runner = PipelineOrchestrator()
    payload = io.BytesIO()
    sf.write(payload, np.full(4800, sample), 48000, format='WAV', subtype='FLOAT')
    provider = Mock()
    provider.synthesize.return_value = (payload.getvalue(), .1)
    clips = tmp_path/'voiceover'/'clips'
    clips.mkdir(parents=True)
    old = clips/'clip_0007.wav'
    old.write_bytes(b'keep previous clip')
    with patch.object(runner,'get_cues',return_value=[Cue(index=7,start=0,end=2,vi_text='Xin chào')]), \
         patch.object(runner,'get_project_dir',return_value=tmp_path), \
         patch.object(runner,'save_cues') as save, \
         patch('src.tts.registry.provider_for_voice',return_value=provider):
        with pytest.raises(PipelineException):
            runner.synthesize_single_cue('invalid_audio',7,voice=VOICE_ID)
    save.assert_not_called()
    assert old.read_bytes() == b'keep previous clip'
