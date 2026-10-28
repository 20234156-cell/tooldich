from unittest.mock import patch

from config.settings import settings
from src.translation.timing_shortener import TimingShortener, MeaningVerifier
from tests.test_translation_workers import make_pool, source_cues, payload, reply
from tests.test_timing_repair import setup_project, fake_speech


def test_multi_cue_repair_fails_over_model_before_splitting_same_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'TRANSLATION_MODEL', 'primary')
    monkeypatch.setattr(settings, 'TRANSLATION_REPAIR_MODEL', 'backup')
    monkeypatch.setattr(settings, 'TRANSLATION_FALLBACK_MODELS', 'backup')
    pool = make_pool(tmp_path, 1)
    monkeypatch.setattr(pool, 'wait_translation_turn', lambda *args: None)
    cues = source_cues(2)
    constraints = {c.index: {'current_vi': 'long draft', 'max_words': 2} for c in cues}
    seen = []
    def generate(**request):
        rows = payload(request['contents'])['to_translate']
        seen.append((request['model'], len(rows)))
        if request['model'] == 'primary':
            raise TimeoutError('504 timeout')
        return reply(rows)
    with patch('google.genai.Client') as factory:
        factory.return_value.models.generate_content.side_effect = generate
        result = TimingShortener(constraints, pool=pool).translate_cues(cues, tmp_path)
    assert len(result) == 2 and seen == [('primary', 2), ('backup', 2)]
    assert all(call.kwargs['http_options']['timeout'] <= 15000 for call in factory.call_args_list)
    assert MeaningVerifier({}, pool=pool).model == 'primary'
    assert MeaningVerifier({}, pool=pool).prefer_model_failover is True


def test_repair_progress_never_rewinds_or_claims_99_during_first_assembly(tmp_path, db_session):
    runner, original = setup_project(tmp_path, db_session)
    proposal = original[0].model_copy(update={'vi_text': 'Xin chào'})
    events = []
    with patch.object(TimingShortener, 'shorten', return_value=[proposal]), \
         patch('src.tts.edge.EdgeTTSProvider.synthesize', side_effect=fake_speech):
        runner.run_voiceover('repair_movie', auto_fit=True, progress_callback=lambda p, m: events.append((p,m)))
    percentages = [p for p, _ in events]
    assert percentages == sorted(percentages)
    assert all(p < 99 for p, message in events if 'chờ kiểm tra timing' in message)
