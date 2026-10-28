import pytest
import json
import sys
from unittest.mock import Mock, MagicMock
import requests

from scripts import apply_cue_review
from scripts.apply_cue_review import validate_review_texts


@pytest.mark.parametrize('field,text', [
    ('vi_text', 'D? li?u cho th?y,'),
    ('vi_text', 'L\ufffdu thiếu'),
    ('corrected_text', '每?人'),
])
def test_review_rejects_encoding_loss_before_mutation(field, text):
    with pytest.raises(ValueError, match='encoding loss'):
        validate_review_texts({'patches': {'2996': {'vi_text': 'Valid target', field: text}}})


def test_review_accepts_unicode_and_sentence_questions():
    validate_review_texts({'patches': {
        '2996': {'vi_text': 'Dữ liệu cho thấy,', 'corrected_text': '数据显示'},
        '3000': {'vi_text': 'Giờ giải quyết thế nào?'},
    }})


def test_corrupted_apply_plan_never_opens_api_session(tmp_path, monkeypatch):
    path = tmp_path / 'review.json'
    path.write_text(json.dumps({'patches': {'2996': {'vi_text': 'D? li?u'}}}), encoding='utf-8')
    monkeypatch.setattr(sys, 'argv', ['apply_cue_review.py', str(path), '--apply'])
    def unexpected_session():
        pytest.fail('Invalid review input must fail before any HTTP session or DB write')
    monkeypatch.setattr(apply_cue_review.requests, 'Session', unexpected_session)
    with pytest.raises(ValueError, match='encoding loss'):
        apply_cue_review.main()


@pytest.mark.parametrize('patch', [{}, {'corrected_text': 'source'},
                                  {'vi_text': None}, {'vi_text': ' '}])
def test_incomplete_review_plan_cannot_apply_source_then_fail(tmp_path, monkeypatch, patch):
    path = tmp_path / 'review.json'
    path.write_text(json.dumps({'project_id': 'audit-project', 'expected_cue_count': 1,
                               'patches': {'1': patch}}), encoding='utf-8')
    monkeypatch.setattr(sys, 'argv', ['apply_cue_review.py', str(path), '--apply'])
    def forbidden_session():
        pytest.fail('Incomplete plan must be rejected before any mutation')
    monkeypatch.setattr(apply_cue_review.requests, 'Session', forbidden_session)
    with pytest.raises(ValueError, match='reviewed vi_text'):
        apply_cue_review.main()


def test_review_retries_explicit_throttling_with_same_payload(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(apply_cue_review.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(apply_cue_review.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0]+seconds))
    throttled = Mock(status_code=429, headers={'Retry-After': '2'})
    success = Mock(status_code=200)
    session = Mock()
    session.patch.side_effect = [throttled, success]
    payload = {'vi_text': 'Reviewed target'}
    assert apply_cue_review.patch_review(session, 'http://localhost/cue/1', payload) is success
    assert clock[0] == 2
    assert session.patch.call_count == 2
    assert all(call.kwargs['json'] == payload for call in session.patch.call_args_list)
    throttled.close.assert_called_once()


def test_review_does_not_retry_ambiguous_network_failure():
    session = Mock()
    session.patch.side_effect = requests.Timeout('unknown write result')
    with pytest.raises(requests.Timeout):
        apply_cue_review.patch_review(session, 'http://localhost/cue/1', {'vi_text': 'target'})
    assert session.patch.call_count == 1


def test_review_does_not_wait_past_throttle_deadline(monkeypatch):
    monkeypatch.setattr(apply_cue_review.time, 'monotonic', lambda: 0)
    session = Mock()
    session.patch.return_value = Mock(status_code=429, headers={'Retry-After': '120'})
    with pytest.raises(TimeoutError, match='budget'):
        apply_cue_review.patch_review(session, 'http://localhost/cue/1', {'vi_text': 'target'})
    assert session.patch.call_count == 1


def test_review_resume_skips_matching_cues_without_invalidating_voice(tmp_path, monkeypatch):
    path = tmp_path / 'review.json'
    path.write_text(json.dumps({'project_id': 'audit-project', 'expected_cue_count': 1,
                               'patches': {'1': {'vi_text': 'Reviewed target'}}}), encoding='utf-8')
    cue = {'index': 1, 'vi_text': 'Reviewed target', 'tts_rate': '+30%',
           'voice_duration': 1.2, 'voice_audio_path': 'voiceover/clip.wav'}
    response = Mock()
    response.json.return_value = [cue]
    session = MagicMock()
    session.__enter__.return_value = session
    session.get.return_value = response
    monkeypatch.setattr(apply_cue_review.requests, 'Session', lambda: session)
    monkeypatch.setattr(sys, 'argv', ['apply_cue_review.py', str(path), '--apply'])
    apply_cue_review.main()
    session.patch.assert_not_called()
    record = json.loads(next(tmp_path.glob('review.applied.*.json')).read_text(encoding='utf-8'))
    assert record['already_matching_ids'] == [1]
    assert record['after'][0]['tts_rate'] == '+30%'
