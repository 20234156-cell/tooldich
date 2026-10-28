from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from src.ai.usage import generate_logged, dashboard, UsagePersistenceError
from src.database import connection
from src.database.models import APIRequest, Project, Transcript


def key(name='key_1'):
    return NS(id=name, key='secret-not-for-logs-O5hw', project_id='shared-project')


def response(**tokens):
    return NS(text='[]', candidates=[], usage_metadata=NS(**tokens))


def test_persisted_usage_shared_project_and_missing_tokens(isolated_env):
    budget = {}
    generate_logged(lambda **kw: response(prompt_token_count=12, candidates_token_count=0),
                    key=key(), model='model', budget=budget, video_id='video')
    generate_logged(lambda **kw: response(), key=key('key_2'), model='model', budget=budget, video_id='video')
    connection.engine.dispose()  # New connections must retain durable data.
    data = dashboard()
    assert len(data['groups']) == 1
    assert data['total']['requests'] == 2
    assert data['total']['retries'] == 1
    assert data['total']['input_tokens'] == 12
    assert data['total']['output_tokens'] == 0
    assert data['total']['cached_tokens'] is None
    assert data['total']['input_tokens_reported_requests'] == 1
    assert data['quota_remaining'] is None and not data['quota_verified']
    assert key().key not in str(data)


@pytest.mark.parametrize('status', [429,503,504])
def test_failure_status_no_secret_and_provider_retry_delay(status):
    def fail(**kw):
        raise RuntimeError(f"{status} {key().key} 'retryDelay': '32s'")
    with pytest.raises(RuntimeError):
        generate_logged(fail, key=key(), model='m', budget={})
    row = dashboard()['requests'][0]
    assert row['http_status'] == status
    assert row['retry_after_s'] == 32
    assert row['outcome'] == 'error'
    assert row['input_tokens'] is None
    assert key().key not in str(row)


def test_database_reservation_failure_does_not_call_provider(monkeypatch):
    import src.ai.usage as usage
    def unavailable():
        raise RuntimeError('database unavailable')
    monkeypatch.setattr(connection, 'SessionLocal', unavailable)
    calls = []
    with pytest.raises(UsagePersistenceError):
        usage.generate_logged(lambda **kw: calls.append(kw), key=key(), model='m', budget={})
    assert calls == []


def test_finalization_failure_leaves_pending_attempt(monkeypatch):
    import src.ai.usage as usage
    commit = usage._commit
    calls = []
    def write(row):
        if calls:
            raise UsagePersistenceError('disk full')
        calls.append(row.id)
        commit(row)
    monkeypatch.setattr(usage, '_commit', write)
    with pytest.raises(UsagePersistenceError):
        generate_logged(lambda **kw: response(), key=key(), model='m', budget={})
    assert dashboard()['total']['pending'] == 1


def test_usage_endpoint_empty_authoritative_transcript_and_filters(isolated_env, monkeypatch):
    from src.api.app import app, orchestrator
    from src.pipeline.state_machine import PipelineState
    with connection.SessionLocal() as db:
        db.add(Project(id='video', title='<title>'))
        db.add(Transcript(project_id='video', segments=[]))
        db.commit()
    monkeypatch.setattr(orchestrator, 'get_state', lambda _: PipelineState())
    generate_logged(lambda **kw: response(), key=key(), model='m', budget={}, video_id='video')
    client = TestClient(app)
    data = client.get('/api/usage?video_id=video&model=m').json()
    assert data['total']['requests'] == 1
    assert data['video']['total_cues'] == 0
    assert data['video']['missing_translation'] == 0
    assert data['video']['technical_qa'] is None
    assert client.get('/api/usage?model=other').json()['total']['requests'] == 0
    assert client.get('/api/usage?video_id=missing').status_code == 404
    assert client.get('/api/usage?offset=-1').status_code == 422


def test_provider_tokens_survive_truncated_response():
    generate_logged(lambda **kw: NS(text='partial', candidates=[NS(finish_reason='MAX_TOKENS')],
        usage_metadata=NS(prompt_token_count=10,candidates_token_count=20)), key=key(), model='m', budget={})
    assert dashboard()['total']['failed'] == 1
    assert dashboard()['total']['output_tokens'] == 20
