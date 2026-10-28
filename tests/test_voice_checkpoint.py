import json

import pytest

from src.ocr.base import Cue
from src.tts.checkpoint import create_voice_manifest, voice_checkpoint_matches
from tests.test_timing_policy import speech


@pytest.mark.parametrize('changed', ['none', 'master', 'clip', 'timeline', 'settings', 'unfinished'])
def test_voice_resume_requires_complete_matching_content(tmp_path, changed):
    (tmp_path/'voiceover/clips').mkdir(parents=True)
    cues = [Cue(index=7,start=0,end=1,vi_text='Xin chào',voice_audio_path='voiceover/clips/clip.wav')]
    (tmp_path/'voiceover/voiceover.wav').write_bytes(speech(1)[0])
    (tmp_path/'voiceover/clips/clip.wav').write_bytes(speech(1)[0])
    (tmp_path/'voiceover/timeline.json').write_text('[{"cue_index":7,"actual_start":0,"actual_end":1}]')
    manifest = create_voice_manifest(tmp_path, cues, 'current-input')
    if changed != 'unfinished':
        (tmp_path/'voiceover/manifest.json').write_text(json.dumps(manifest))
    if changed in {'master', 'clip', 'timeline'}:
        name = {'master':'voiceover/voiceover.wav','clip':'voiceover/clips/clip.wav','timeline':'voiceover/timeline.json'}[changed]
        (tmp_path/name).write_bytes(b'nonempty corrupted artifact')
    identity = 'other-settings' if changed == 'settings' else 'current-input'
    assert voice_checkpoint_matches(tmp_path, cues, identity) == (changed == 'none')


@pytest.mark.parametrize('record', [None, [], 'invalid', 42,
    {'version': 1, 'input_hash': 'current-input', 'cue_ids': [7], 'files': []}])
def test_malformed_voice_manifest_is_a_cache_miss(tmp_path, record):
    (tmp_path/'voiceover').mkdir()
    (tmp_path/'voiceover/manifest.json').write_text(json.dumps(record))
    cues = [Cue(index=7, start=0, end=1, vi_text='Xin chào',
                voice_audio_path='voiceover/clips/clip.wav')]
    assert voice_checkpoint_matches(tmp_path, cues, 'current-input') is False
