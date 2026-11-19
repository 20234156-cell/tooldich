import pytest

from src.database.models import Project
from src.ocr.base import Cue
from src.pipeline.orchestrator import PipelineOrchestrator
from src.pipeline.state_machine import PipelineState, ProjectState


@pytest.mark.parametrize('kinds,correct,expected', [
    (['stt'], True, ['AI đã sửa nguồn STT của 1 cue; cần đối chiếu âm thanh gốc.']),
    (['ocr'], True, ['AI đã sửa nguồn OCR của 1 cue; cần đối chiếu ảnh gốc.']),
    (['stt', 'ocr'], True, ['AI đã sửa nguồn OCR của 1 cue; cần đối chiếu ảnh gốc.',
                           'AI đã sửa nguồn STT của 1 cue; cần đối chiếu âm thanh gốc.']),
    (['stt'], False, []),
])
def test_source_correction_review_uses_actual_evidence_and_keeps_gate(
        tmp_path, db_session, monkeypatch, kinds, correct, expected):
    project_id='correction_provenance'
    db_session.add(Project(id=project_id));db_session.commit()
    runner=PipelineOrchestrator()
    monkeypatch.setattr(runner,'get_project_dir',lambda _:tmp_path)
    cues=[Cue(index=i+1,start=i*2,end=i*2+1,text='你好',source_kind=kind,
              confidence=0 if kind=='stt' else 95) for i,kind in enumerate(kinds)]
    runner.save_state(project_id,PipelineState(current_state=ProjectState.SOURCE_REVIEWED))
    runner.save_cues(project_id,cues)

    class Translator:
        fallback_providers = []
        model='fixture-model'
        used_models={'fixture-model':'fixture-version'}
        source_corrections={c.index for c in cues} if correct else set()
        def __init__(self,**kwargs):pass
        def translate_cues(self,items,**kwargs):
            for item in items:
                item.vi_text='Xin chào'
                if correct:item.corrected_text='你好啊'
            return items

    monkeypatch.setattr('src.pipeline.orchestrator.BatchTranslator',Translator)
    runner.run_translation(project_id)
    state=runner.get_state(project_id)
    stage=state.get_stage('TRANSLATED')
    assert stage.warnings==expected
    assert ('ai_source_corrections_review' in stage.degraded) is correct
    assert state.current_state==ProjectState.TRANSLATED  # never auto-approves a correction
    saved=runner.get_cues(project_id)
    assert [c.source_kind for c in saved]==kinds
    assert [c.confidence for c in saved]==[0 if kind=='stt' else 95 for kind in kinds]
    assert all(c.corrected_text==('你好啊' if correct else None) for c in saved)
