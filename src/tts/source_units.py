"""Recover proven duplicate source fragments independently of unrelated timing."""
import hashlib
import json
from pathlib import Path

from src.translation.memory import atomic_json


def recover_duplicate_units(runner,project_id,cues,affected_ids,source_sha256,
                            models,should_continue=None,max_pairs=3,attempted=None):
    from src.jobs.logger import TaskCancelled,mask_secrets
    from src.ocr.timing_duplicate import candidate_pairs,verify_pair,assess_pair_votes
    from src.ocr import learning
    project=runner.get_project_dir(project_id)
    source=runner.get_source_video_path(project_id)
    roi=runner.get_roi_set(project_id).get_primary_subtitle_roi()
    if roi is None:return []
    records=[];attempted=attempted if attempted is not None else set()
    for _ in range(max_pairs):
        pair=next((p for p in candidate_pairs(cues,set(affected_ids))
                   if (p[0].index,p[1].index) not in attempted
                   and p[0].vi_text.strip()==p[1].vi_text.strip()),None)
        if pair is None:break
        left,right=pair;attempted.add((left.index,right.index))
        if should_continue and not should_continue():raise TaskCancelled('Source unit review cancelled')
        identity=dict(version='source-unit-v1',source_sha256=source_sha256,
            cues=[(c.index,c.start,c.end,c.effective_source_text,c.vi_text) for c in pair],
            roi=roi.model_dump(),models=models)
        key=hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        path=project/'audit/source_units'/(key+'.json')
        evidence=None;cached=False
        if path.is_file():
            saved=json.loads(path.read_text(encoding='utf8'))
            if saved.get('identity')==json.loads(json.dumps(identity)):
                evidence=saved.get('evidence')
                cached=saved.get('completed') is True
        if not cached:
            atomic_json(path,dict(identity=identity,completed=False,status='started'))
            try:
                evidence=verify_pair(source,roi,left,right,models,should_continue)
            except Exception as exc:
                atomic_json(path,dict(identity=identity,completed=False,status='failed',
                    error_type=type(exc).__name__,error_code=getattr(exc,'code',None),error=mask_secrets(str(exc))))
                raise
            atomic_json(path,dict(identity=identity,evidence=evidence,completed=True,
                status='observed' if evidence else 'unresolved',
                reason=None if evidence else 'verifier_returned_no_conclusive_evidence'))
        if not evidence:continue
        text,rejection=assess_pair_votes(evidence.get('votes',[]),evidence.get('models',[]),left,right)
        if rejection or text!=evidence.get('text'):continue
        with source.open('rb') as stream:
            current=hashlib.file_digest(stream,'sha256').hexdigest()
        if current!=source_sha256:raise ValueError('Source changed during duplicate-unit review')
        merged=left.model_copy(update=dict(end=right.end,corrected_text=text,
            voice_audio_path=None,voice_duration=None,tts_rate=None,is_approved=False))
        after=[merged if c.index==left.index else c for c in cues if c.index!=right.index]
        # This is an independently evidenced source correction. Do not roll
        # it back merely because another cluster later fails voice timing.
        with learning.review_context(decided_by='AI',human_confirmed=False,
                reason='Hai model xác nhận sáu ảnh cùng chữ và tiếng nguồn nói đúng một lần.',
                image_features=dict(evidence,source_sha256=source_sha256)):
            runner.save_cues(project_id,after,history=dict(action='GỘP',expected_before=cues,
                                                          cue_ids=[left.index,right.index]))
        records.append(dict(cue_ids=[left.index,right.index],kept_id=left.index,
                            source_sha256=source_sha256,evidence=str(path.relative_to(project))))
        cues=after
    return records
