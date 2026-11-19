import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.database.models import CueReviewRevision, Project
from src.ocr.base import Cue
from src.ocr import learning
from src.pipeline.orchestrator import PipelineOrchestrator
from src.pipeline.state_machine import PipelineState, ProjectState


@pytest.fixture
def project(db_session, tmp_path):
    db_session.add(Project(id='learn'));db_session.commit()
    runner=PipelineOrchestrator()
    runner.save_cues('learn',[Cue(index=34,start=0,end=1,text='原文',confidence=50),
                             Cue(index=90,start=1,end=2,text='下一句',confidence=99)])
    runner.save_state('learn',PipelineState(current_state=ProjectState.OCR_READY))
    return runner


def test_empty_memory_is_valid_and_never_implies_training():
    rules,records=learning.read_memory()
    assert rules==records==[]
    report=learning.analyze([Cue(index=1,start=0,end=1,text='啊',confidence=99)])
    assert report['no_learning_history'] and report['actual_changes']==0
    assert report['proposals'][0]['signals']==['short_text']


def legacy_delete_record(project,db_session):
    """An actual pre-caption_role snapshot/hash, not a relaxed current fixture."""
    before=[{k:v for k,v in c.model_dump().items() if k!='caption_role'}
            for c in project.get_cues('learn')]
    project.delete_cue('learn',34)
    row=db_session.query(CueReviewRevision).one()
    payload=dict(row.payload)
    after=[c for c in before if c['index']!=34]
    payload.update(before_hash=learning.digest(before),after_hash=learning.digest(after),
                   before=[c for c in before if c['index']==34],after=[])
    row.payload=payload;db_session.commit()
    return row.id


def test_legacy_revision_undo_survives_new_default_role_field(project,db_session):
    from src.ocr.review_evidence import undo
    revision=legacy_delete_record(project,db_session)
    undo(project,'learn',revision)
    assert [c.index for c in project.get_cues('learn')]==[34,90]


def test_legacy_evidence_hash_is_valid_for_same_source_edit(project,db_session):
    before=[{k:v for k,v in c.model_dump().items() if k!='caption_role'}
            for c in project.get_cues('learn')]
    with learning.review_context(evidence={'transcript_hash':learning.digest(before)}):
        project.update_cue('learn',34,{'corrected_text':'正确'})
    assert project.get_cues('learn')[0].corrected_text=='正确'
    assert db_session.query(CueReviewRevision).count()==1


@pytest.mark.parametrize('change',['source','translation','timing','role'])
def test_legacy_hash_compatibility_never_overwrites_real_later_changes(project,db_session,change):
    from src.ocr.review_evidence import undo
    revision=legacy_delete_record(project,db_session)
    cues=project.get_cues('learn')
    if change=='source':cues[0].text='不同'
    if change=='translation':cues[0].vi_text='Đã sửa tiếp'
    if change=='timing':cues[0].end+=.1
    if change=='role':cues[0].caption_role='nonverbal_laughter'
    project.save_cues('learn',cues)
    with pytest.raises(ValueError,match='thay đổi tiếp theo'):
        undo(project,'learn',revision)


def test_source_edit_and_history_commit_together(project,db_session):
    with learning.review_context(decided_by='người dùng'):
        project.update_cue('learn',34,{'corrected_text':'正确'})
    rows=db_session.query(CueReviewRevision).all()
    assert len(rows)==1 and rows[0].payload['cue_ids']==[34]
    assert rows[0].payload['review_status']=='đề xuất'
    assert rows[0].payload['before'][0]['text']=='原文'
    assert rows[0].payload['after'][0]['corrected_text']=='正确'
    assert learning.export_pending()==1
    assert learning.export_pending()==0
    assert len(learning.read_memory()[1])==1


def test_history_failure_rolls_back_source_not_json_only(project):
    with patch('src.ocr.learning.build_revision',side_effect=OSError('disk full')):
        with pytest.raises(Exception):project.update_cue('learn',34,{'corrected_text':'bad'})
    assert project.get_cues('learn')[0].corrected_text is None


def test_stale_source_edit_refused(project):
    before=project.get_cues('learn')
    project.update_cue('learn',34,{'corrected_text':'new'})
    with pytest.raises(Exception):
        project.save_cues('learn',before,history={'action':'SỬA','expected_before':before})
    assert project.get_cues('learn')[0].corrected_text=='new'


def test_outbox_recovers_append_before_export_marker(project,db_session):
    project.delete_cue('learn',34)
    row=db_session.query(CueReviewRevision).one()
    learning.ensure_files().joinpath('corrections.jsonl').write_text(
        json.dumps(row.payload,ensure_ascii=False)+'\n',encoding='utf-8')
    assert learning.export_pending()==1
    assert len(learning.read_memory()[1])==1


def test_corrupted_journal_not_silently_skipped(project):
    project.delete_cue('learn',34)
    learning.ensure_files().joinpath('corrections.jsonl').write_text('{broken',encoding='utf-8')
    with pytest.raises(ValueError):learning.export_pending()
    assert len(project.get_cues('learn'))==1


def test_delete_undo_restores_stable_id_but_requires_new_approval(project):
    from src.ocr.review_evidence import undo
    project.delete_cue('learn',34)
    from src.database import connection
    with connection.SessionLocal() as db:revision=db.query(CueReviewRevision).one().id
    undo(project,'learn',revision)
    assert [c.index for c in project.get_cues('learn')]==[34,90]
    assert project.get_cues('learn')[0].is_approved is False
    with pytest.raises(ValueError):undo(project,'learn',revision)


def test_undo_will_not_overwrite_a_later_edit(project,db_session):
    from src.ocr.review_evidence import undo
    project.delete_cue('learn',34)
    revision=db_session.query(CueReviewRevision).one().id
    project.update_cue('learn',90,{'corrected_text':'later'})
    with pytest.raises(ValueError):undo(project,'learn',revision)
    assert project.get_cues('learn')[0].corrected_text=='later'


def test_experimental_and_conflicting_rules_never_mutate_cues():
    rule=dict(id='R001',condition='short_text',scope={'source_kind':'ocr'},exceptions=[],
              action='LOẠI',correct_count=0,wrong_count=0,status='thử nghiệm')
    other=dict(rule,id='R002',action='GIỮ')
    path=learning.ensure_files()/'rules.md'
    path.write_text('```json\n'+json.dumps([rule,other],ensure_ascii=False)+'\n```',encoding='utf-8')
    cue=Cue(index=33,start=0,end=1,text='不',confidence=99)
    report=learning.analyze([cue])
    assert cue.text=='不' and report['actual_changes']==report['auto_processed']==0
    assert report['proposals'][0]['conflict']
    assert report['proposals'][0]['action']=='CẦN NGƯỜI'
    assert report['active_rule_ids']==[]
    assert report['experimental_rule_ids']==['R001','R002']


def test_active_rule_requires_recorded_human_approval_and_validation():
    rule=dict(id='R001',condition='short_text',scope={},exceptions=[],action='LOẠI',
              correct_count=3,wrong_count=0,status='đang dùng')
    (learning.ensure_files()/'rules.md').write_text('```json\n'+json.dumps([rule])+'\n```',encoding='utf-8')
    with pytest.raises(ValueError):learning.read_memory()


def test_forged_confirmed_jsonl_is_not_a_training_label():
    record=dict(id='fake',decided_by='người dùng',review_status='người dùng xác nhận',
                evidence={'complete':True},video_hash='fake')
    with pytest.raises(ValueError):learning.active_labels([record])


def test_same_source_memory_is_bound_to_image_and_timing(project, db_session, tmp_path):
    import hashlib
    from src.ocr.source_recheck import uncertain

    source = tmp_path / 'source.mp4'
    source.write_bytes(b'fixture source identity')
    directory = project.get_project_dir('learn') / 'transcript'
    crop = directory / 'cues' / 'cue.jpg'
    crop.parent.mkdir(parents=True)
    crop.write_bytes(b'fixture crop')
    cue = project.get_cues('learn')[0].model_copy(update={'image_path':'cues/cue.jpg'})
    cue_hash = hashlib.sha256(crop.read_bytes()).hexdigest()
    token = 'a' * 32
    evidence = learning.ensure_files() / 'evidence' / token
    evidence.mkdir(parents=True)
    frames = []
    for index in range(3):
        name = f'frame_{index}.jpg'
        pixels = f'frame {index}'.encode()
        (evidence / name).write_bytes(pixels)
        frames.append({'name':name,'sha256':hashlib.sha256(pixels).hexdigest()})
    record = {
        'id':'fixture-review', 'video_hash':learning.video_identity(source),
        'created_at':1, 'action':'SỬA', 'review_status':'người dùng xác nhận',
        'decided_by':'người dùng', 'evidence':{'token':token,'complete':True,'frames':frames},
        'before':[cue.model_dump()],
        'after':[cue.model_copy(update={'corrected_text':'正确'}).model_dump()],
        'cue_images':[{'sha256':cue_hash,'path':'evidence/old.jpg','cue_id':cue.index}],
    }
    (learning.root() / 'evidence' / 'old.jpg').write_bytes(crop.read_bytes())
    db_session.add(CueReviewRevision(id=record['id'],project_id='learn',created_at=1,payload=record))
    db_session.commit()
    matches = learning.flag_known_conflicts([cue.model_copy(update={'index':999})],directory,source)
    assert matches[0].ocr_alternatives[0]['variant']=='human_review_memory'
    assert matches[0].text==cue.text and not uncertain(matches[0])
    with pytest.raises(Exception, match='OCR mơ hồ'):
        project._validate_source_review(matches)
    assert learning.flag_known_conflicts([cue.model_copy(update={'start':.1})],directory,source)[0].ocr_alternatives==[]
    source.write_bytes(b'different source identity')
    assert learning.flag_known_conflicts([cue],directory,source)[0].ocr_alternatives==[]


@pytest.mark.parametrize('operation',['split','merge','mixed'])
def test_structural_edit_keeps_source_kind(operation):
    runner=PipelineOrchestrator()
    cues=[Cue(index=7,start=0,end=2,text='语音',source_kind='stt'),
          Cue(index=30,start=2,end=4,text='来源',source_kind='ocr' if operation=='mixed' else 'stt')]
    with patch.object(runner,'get_cues',return_value=cues),patch.object(runner,'save_cues'), \
         patch.object(runner,'get_state',return_value=PipelineState()),patch.object(runner,'save_state'), \
         patch.object(runner,'_remove_stale_artifacts'):
        if operation=='split':
            result=runner.split_cue('p',7,1,'语','音')
            assert [c.source_kind for c in result]==['stt','stt','stt']
        elif operation=='merge':
            assert runner.merge_cues('p',7,30).source_kind=='stt'
        else:
            with pytest.raises(Exception):runner.merge_cues('p',7,30)


@pytest.mark.parametrize('confirmed,reused', [('同一行字幕', True), ('已经确认的其他字', False)])
def test_confirmed_merge_keeps_one_caption_and_only_reuses_matching_translation(confirmed, reused):
    runner = PipelineOrchestrator()
    cues = [Cue(index=8, start=10, end=11, text='同一行字幕', vi_text='Một dòng phụ đề',
                translation_origin='manual', is_approved=True, voice_audio_path='old.wav'),
            Cue(index=9, start=11, end=12, text='同一行字', vi_text='Một dòng'),
            Cue(index=10, start=12, end=13, text='下一行')]
    with patch.object(runner, 'get_cues', return_value=cues), patch.object(runner, 'save_cues') as save, \
         patch.object(runner, 'get_state', return_value=PipelineState()), patch.object(runner, 'save_state'), \
         patch.object(runner, '_remove_stale_artifacts'):
        merged = runner.merge_cues('p', 8, 9, confirmed_text=confirmed)
    assert (merged.index, merged.start, merged.end, merged.effective_source_text) == (8, 10, 12, confirmed)
    assert merged.text == confirmed
    assert merged.vi_text == ('Một dòng phụ đề' if reused else None)
    assert merged.is_approved is reused and not merged.voice_audio_path
    assert save.call_args.args[1] == [merged, cues[2]]
