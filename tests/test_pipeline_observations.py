import pytest
from sqlalchemy.exc import SQLAlchemyError

from src.database.models import PipelineObservation, Project
from src.pipeline.observations import capture, summarize
from src.pipeline.orchestrator import PipelineOrchestrator
from src.pipeline.state_machine import PipelineState, ProjectState


def flagged():
    state = PipelineState(current_state=ProjectState.TRANSLATED)
    state.set_stage_status('TRANSLATED', 'COMPLETED',
                           warnings=['needs semantic check'],
                           degraded=['auto_translation_shortened'])
    state.stages['TRANSLATED'].started_at = 100.0
    return state


def test_healthy_project_creates_no_observations(db_session):
    state = PipelineState()
    state.set_stage_status('OCR_READY', 'COMPLETED')
    capture(db_session, 'p', state)
    db_session.commit()
    assert db_session.query(PipelineObservation).count() == 0


def test_idempotent_across_restart_and_progress_not_new_evidence(isolated_env):
    sessions = isolated_env['SessionLocal']
    state = flagged()
    with sessions() as db:
        capture(db, 'p', state)
        capture(db, 'p', state)
        db.commit()
    state.updated_at += 100
    state.stages['TRANSLATED'].progress = 40
    with sessions() as db:
        capture(db, 'p', state)
        db.commit()
        assert db.query(PipelineObservation).count() == 1


def test_new_attempt_and_input_are_distinct(db_session):
    state = flagged()
    capture(db_session, 'p', state)
    state.stages['TRANSLATED'].started_at = 200
    capture(db_session, 'p', state)
    state.stages['TRANSLATED'].input_hash = 'different-input'
    capture(db_session, 'p', state)
    db_session.commit()
    assert db_session.query(PipelineObservation).count() == 3


def test_project_status_and_evidence_share_transaction(isolated_env, monkeypatch):
    sessions = isolated_env['SessionLocal']
    with sessions() as db:
        db.add(Project(id='p', title='keep', status='CREATED'))
        db.commit()
    import src.pipeline.observations as observations
    def fail(db, project_id, state):
        raise SQLAlchemyError('simulated write failure')
    monkeypatch.setattr(observations, 'capture', fail)
    with pytest.raises(SQLAlchemyError):
        PipelineOrchestrator().save_state('p', flagged())
    with sessions() as db:
        assert db.get(Project, 'p').status == 'CREATED'
        assert db.query(PipelineObservation).count() == 0
    # Existing state-file publication precedes DB commit: not claimed atomic.


def test_orchestrator_records_warning_without_claiming_verified_label(isolated_env):
    sessions = isolated_env['SessionLocal']
    with sessions() as db:
        db.add(Project(id='p', title='keep', status='CREATED'))
        db.commit()
    state = flagged()
    before = state.model_dump()
    PipelineOrchestrator().save_state('p', state)
    with sessions() as db:
        row = db.query(PipelineObservation).one()
        assert row.payload['root_cause'] is None
        assert row.payload['assessment'] == 'unverified_observation'
        assert db.get(Project, 'p').status == 'TRANSLATED'
    assert state.model_dump() == before


def test_report_half_open_window_and_no_fabricated_error_rate(db_session):
    for project, created in [('a', 10), ('b', 20), ('c', 30)]:
        capture(db_session, project, flagged())
        db_session.query(PipelineObservation).filter_by(project_id=project).one().created_at = created
    db_session.commit()
    report = summarize(db_session, 10, 30)
    assert report['observation_count'] == 2
    assert report['affected_projects'] == 2
    assert report['groups'][0]['signal'] == 'auto_translation_shortened'
    assert 'error_rate' not in report
    with pytest.raises(ValueError):
        summarize(db_session, 30, 10)


def test_failed_stage_without_messages_is_retained(db_session, monkeypatch):
    monkeypatch.setattr('src.pipeline.observations.time.time', lambda: 100.0)
    state = PipelineState()
    state.set_stage_status('VOICE_READY', 'FAILED')
    capture(db_session, 'p', state)
    db_session.commit()
    assert summarize(db_session)['status_counts'] == {'FAILED': 1}


def test_observations_mask_provider_secrets(db_session):
    state = flagged()
    state.stages['TRANSLATED'].errors = ['api_key=do-not-persist', 'Cookie: session=private']
    capture(db_session, 'p', state)
    db_session.commit()
    payload = str(db_session.query(PipelineObservation).one().payload)
    assert 'do-not-persist' not in payload
    assert 'session=private' not in payload
    assert 'REDACTED' in payload
