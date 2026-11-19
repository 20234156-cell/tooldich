"""Immutable source-frame packages and explicit reviewed decisions (no AI labels)."""
import hashlib
import json
import math
from pathlib import Path
import time
import uuid

from src.ocr.base import Cue
from src.ocr import learning
from src.pipeline.state_machine import InvalidationTrigger, invalidate_pipeline


def prepare(runner, project_id, cue_id, extra=False, target_id=None, transition=None):
    import cv2
    cues = runner.get_cues(project_id)
    cue = next((c for c in cues if c.index == cue_id), None)
    if cue is None or cue.duration <= 0:
        raise ValueError('Không có câu/mốc thời gian hợp lệ để lấy ảnh.')
    if target_id is not None:
        target = next((c for c in cues if c.index == target_id), None)
        if target is None or target.duration <= 0 or target_id == cue_id:
            raise ValueError('Câu gộp không hợp lệ.')
    source = runner.get_source_video_path(project_id)
    video_hash = learning.video_identity(source, fresh=True)
    token = uuid.uuid4().hex
    folder = learning.ensure_files() / 'evidence' / token
    folder.mkdir(parents=True)
    cap = cv2.VideoCapture(str(source))
    frames = []
    try:
        if not cap.isOpened():
            raise ValueError('Không mở được video nguồn.')
        fps = cap.get(cv2.CAP_PROP_FPS)
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError('Không xác minh được FPS nguồn.')
        first = math.ceil(cue.start * fps)
        last = math.ceil(cue.end * fps) - 1
        indices = {first, (first+last)//2, last}
        transitions = any(a.get('variant') == 'temporal_vote' for a in cue.ocr_alternatives)
        if extra or transitions:
            indices.update(round(first+(last-first)*f) for f in (.1,.25,.75,.9))
            for alternative in cue.ocr_alternatives:
                if alternative.get('variant') == 'temporal_vote':
                    for key in ('start','end'):
                        value = alternative.get(key)
                        if isinstance(value,(float,int)) and math.isfinite(value):
                            boundary = round(value*fps)
                            indices.update((boundary-1,boundary,boundary+1))
        if transition is not None:
            if not math.isfinite(transition) or not cue.start < transition < cue.end:
                raise ValueError('Mốc chuyển phải nằm trong câu.')
            index=round(transition*fps)
            indices.update((index-1,index,index+1))
        roi = runner.get_roi_set(project_id).get_primary_subtitle_roi()
        for number, index in enumerate(sorted(i for i in indices if first <= i <= last)[:15]):
            cap.set(cv2.CAP_PROP_POS_FRAMES, index)
            ok, image = cap.read()
            actual_index = int(cap.get(cv2.CAP_PROP_POS_FRAMES))-1
            seconds = cap.get(cv2.CAP_PROP_POS_MSEC)/1000
            if (not ok or actual_index != index or not math.isfinite(seconds)
                    or not cue.start <= seconds < cue.end):
                raise ValueError('Không xác minh được khung hình đúng trong câu; chưa cho xác nhận.')
            name = f'frame_{number}.jpg'
            if not cv2.imwrite(str(folder / name), image, [cv2.IMWRITE_JPEG_QUALITY,95]):
                raise OSError('Không lưu được bằng chứng khung hình.')
            row = dict(name=name, frame_index=index, seconds=seconds,
                       frame_size=[image.shape[1],image.shape[0]],
                       sha256=hashlib.sha256((folder/name).read_bytes()).hexdigest())
            if roi:
                from src.ocr.source_recheck import source_pixel_rect
                x,y,w,h = source_pixel_rect(roi,image.shape[1],image.shape[0])
                crop_name = f'crop_{number}.jpg'
                if not cv2.imwrite(str(folder/crop_name),image[y:y+h,x:x+w]):
                    raise OSError('Không lưu được ảnh crop bằng chứng.')
                row.update(crop_name=crop_name, pixel_rect=[x,y,w,h],
                           crop_sha256=hashlib.sha256((folder/crop_name).read_bytes()).hexdigest())
            frames.append(row)
    finally:
        cap.release()
    if learning.digest(learning.normalized(runner.get_cues(project_id))) != learning.digest(learning.normalized(cues)):
        raise ValueError('Transcript đổi trong lúc lấy ảnh; hãy lấy lại bằng chứng.')
    target_evidence = prepare(runner,project_id,target_id,extra=extra) if target_id is not None else None
    record = dict(token=token, project_id=project_id, cue_id=cue_id, video_hash=video_hash,
                  transcript_hash=learning.digest(learning.normalized(cues)), frames=frames,
                  complete=len(frames)>=3 and (not target_evidence or target_evidence['complete']),
                  created_at=time.time(), extra=bool(extra or transitions),transition=transition,
                  target=target_evidence)
    record['matching_rules'] = learning.analyze([cue])['proposals']
    (folder/'manifest.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    return record


def load_verified(runner, project_id, cue_id, token):
    if len(token)!=32 or any(c not in '0123456789abcdef' for c in token):
        raise ValueError('Mã bằng chứng không hợp lệ.')
    folder = learning.root()/'evidence'/token
    record = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    if record['project_id'] != project_id or record['cue_id'] != cue_id:
        raise ValueError('Bằng chứng thuộc câu/dự án khác.')
    if not learning.transcript_hash_matches(runner.get_cues(project_id),record['transcript_hash']):
        raise ValueError('Nội dung đã đổi từ lúc xem ảnh; lấy lại ảnh trước khi quyết định.')
    if learning.video_identity(runner.get_source_video_path(project_id),fresh=True) != record['video_hash']:
        raise ValueError('Nguồn video đã thay đổi.')
    for row in record['frames']:
        for name,sha in [(row['name'],row['sha256']), (row.get('crop_name'),row.get('crop_sha256'))]:
            if name and (Path(name).name != name or hashlib.sha256((folder/name).read_bytes()).hexdigest()!=sha):
                raise ValueError('Ảnh bằng chứng đã đổi/hỏng.')
    if record.get('target'):
        target=record['target']
        load_verified(runner,project_id,target['cue_id'],target['token'])
    return record


def decide(runner, project_id, cue_id, payload):
    from src.database import connection
    from src.database.models import CueReviewRevision
    token = payload['token']
    request_hash = learning.digest(payload)
    with connection.SessionLocal() as db:
        previous = db.get(CueReviewRevision,token)
        if previous:
            if previous.project_id!=project_id or previous.payload.get('request_hash')!=request_hash:
                raise ValueError('Bằng chứng này đã dùng cho quyết định khác.')
            return previous.payload
    evidence = load_verified(runner,project_id,cue_id,token)
    action = payload['action']
    answers = payload.get('answers',{})
    if action not in {'GIỮ','SỬA','LOẠI','CẮT','GỘP','CẦN NGƯỜI'}:
        raise ValueError('Hành động chưa hỗ trợ.')
    reason = payload.get('reason','').strip()
    if not reason:
        raise ValueError('Cần ghi lý do cho từng quyết định.')
    confirmed = payload.get('confirmed') is True and action!='CẦN NGƯỜI'
    if confirmed:
        if not evidence['complete']:
            raise ValueError('Chưa đủ ba khung hình khác nhau; chuyển CẦN NGƯỜI.')
        if answers.get('text') not in {'đúng','sai'} or answers.get('role') not in {'phụ đề','chữ cảnh','trùng'}:
            raise ValueError('Cần xác nhận riêng chữ và vai trò của chữ.')
        if action in {'GIỮ','SỬA','CẮT','GỘP'} and (answers['role']!='phụ đề' or answers.get('alignment')!='khớp'):
            raise ValueError('Cần đối chiếu nội dung/người nói/thời gian với âm thanh.')
        if action=='GIỮ' and answers['text']!='đúng':
            raise ValueError('GIỮ cần xác nhận chữ đúng.')
        if action=='LOẠI' and answers['role'] not in {'chữ cảnh','trùng'}:
            raise ValueError('LOẠI cần xác minh chữ cảnh hoặc câu trùng.')
        if not payload.get('cause','').strip():
            raise ValueError('Cần ghi nguyên nhân gốc để lưu nhãn học.')
    if not confirmed and action!='CẦN NGƯỜI':
        raise ValueError('Chưa xác nhận thì chỉ ghi CẦN NGƯỜI; không sửa nội dung.')
    metadata = dict(decided_by='người dùng',human_confirmed=confirmed,evidence=evidence,
                    decision_id=token,request_hash=request_hash,answers=answers,reason=reason,
                    cause=payload.get('cause',''),supersedes=payload.get('supersedes'),
                    confidence=payload.get('confidence'), image_features=payload.get('image_features',{}),
                    crop_matches_text=payload.get('crop_matches_text') is True,
                    needs_human=action=='CẦN NGƯỜI')
    if payload.get('rule_id'):
        rules,_=learning.read_memory()
        rule=next((r for r in rules if r['id']==payload['rule_id']),None)
        if (not rule or rule['status']=='tạm ngưng' or rule['condition'] not in learning.signals(
                next(c for c in runner.get_cues(project_id) if c.index==cue_id))):
            raise ValueError('Luật không khớp bằng chứng hiện tại.')
        metadata['rule_id']=rule['id']
    if payload.get('supersedes'):
        with connection.SessionLocal() as db:
            old=db.get(CueReviewRevision,payload['supersedes'])
            if not old or old.project_id!=project_id:
                raise ValueError('Quyết định cũ không thuộc dự án này.')
    with learning.review_context(**metadata):
        cues = runner.get_cues(project_id)
        cue = next((c for c in cues if c.index==cue_id),None)
        if cue is None:
            raise ValueError('Câu không còn tồn tại.')
        if action in {'GIỮ','SỬA'}:
            text = cue.effective_source_text if action=='GIỮ' else payload.get('text','').strip()
            if not text:
                raise ValueError('Chữ xác nhận không được rỗng.')
            if cue.corrected_text==text:
                runner.save_cues(project_id,cues,history={'action':action,'cue_ids':[cue_id],'expected_before':cues})
            else:
                metadata['action_override']=action
                with learning.review_context(**metadata):
                    runner.update_cue(project_id,cue_id,{'corrected_text':text})
        elif action=='LOẠI':
            runner.delete_cue(project_id,cue_id)
        elif action=='CẮT':
            if (evidence.get('transition') is None or abs(float(payload['split_time'])-evidence['transition'])>.001
                    or not str(payload.get('text1') or '').strip() or not str(payload.get('text2') or '').strip()):
                raise ValueError('Cần xem ảnh quanh đúng mốc tách và nhập chữ cho cả hai câu.')
            runner.split_cue(project_id,cue_id,float(payload['split_time']),payload.get('text1'),payload.get('text2'))
        elif action=='GỘP':
            target=int(payload['target_cue_id'])
            ids=[c.index for c in cues]
            if target not in ids or abs(ids.index(target)-ids.index(cue_id))!=1:
                raise ValueError('Chỉ gộp hai câu liền nhau; cần đối chiếu cả hai đoạn.')
            if not evidence.get('target') or evidence['target']['cue_id']!=target:
                raise ValueError('Cần lấy và xem ba khung hình của cả hai câu gộp.')
            merged_text = str(payload.get('merged_text') or '').strip()
            runner.merge_cues(project_id,cue_id,target,confirmed_text=merged_text or None)
        else:
            runner.save_cues(project_id,cues,history={'action':'CẦN NGƯỜI','cue_ids':[cue_id],'expected_before':cues})
    with connection.SessionLocal() as db:
        return db.get(CueReviewRevision,token).payload


def undo(runner,project_id,revision_id):
    from src.database import connection
    from src.database.models import CueReviewRevision
    with connection.SessionLocal() as db:
        row=db.get(CueReviewRevision,revision_id)
        if not row or row.project_id!=project_id:
            raise ValueError('Không có lịch sử của dự án này.')
        record=row.payload
    current=runner.get_cues(project_id)
    if record.get('video_hash') and learning.video_identity(
            runner.get_source_video_path(project_id), fresh=True) != record['video_hash']:
        raise ValueError('Nguồn video đã đổi. Không hoàn tác lên nguồn khác.')
    if record['before_hash']==record['after_hash']:
        raise ValueError('Quyết định này không đổi nội dung để hoàn tác.')
    if not learning.transcript_hash_matches(current,record['after_hash']):
        raise ValueError('Đã có thay đổi tiếp theo; không ghi đè khi hoàn tác. Hãy đối chiếu lịch sử.')
    restore={c.index:c for c in current if c.index not in record['cue_ids']}
    for data in record['before']:
        cue=Cue.model_validate(data)
        cue.vi_text=None;cue.voice_audio_path=None;cue.voice_duration=None;cue.tts_rate=None;cue.is_approved=False
        restore[cue.index]=cue
    cues=[restore[i] for i in record['order_before'] if i in restore]
    with learning.review_context(decided_by='người dùng',reason='Hoàn tác quyết định trước; cần duyệt lại nguồn.'):
        runner.save_cues(project_id,cues,history={'action':'HOÀN TÁC','expected_before':current,'reverses':revision_id})
    state=runner.get_state(project_id)
    invalidate_pipeline(state,InvalidationTrigger.SOURCE_CUES_CHANGED)
    runner._remove_stale_artifacts(project_id,InvalidationTrigger.SOURCE_CUES_CHANGED)
    runner.save_state(project_id,state)
    return {'restored_cues':len(cues),'reverses':revision_id,'needs_review':True}


def record_agent_proposal(runner,project_id,cue_id,token,reason,cause):
    """Append an agent observation to DB/outbox without saving transcript/artifacts."""
    from src.database import connection
    from src.database.models import Transcript
    evidence=load_verified(runner,project_id,cue_id,token)
    with connection.SessionLocal() as db:
        transcript=db.query(Transcript).filter_by(project_id=project_id).first()
        if transcript is None or not learning.transcript_hash_matches(transcript.segments,evidence['transcript_hash']):
            raise ValueError('Transcript đã thay đổi; chưa ghi đề xuất.')
        with learning.review_context(decided_by='agent',needs_human=True,evidence=evidence,
                                     reason=reason,cause=cause):
            revision=learning.build_revision(runner,project_id,transcript.segments,transcript.segments,
                                            {'action':'CẦN NGƯỜI','cue_ids':[cue_id]})
        revision.payload['applied']=False
        db.add(revision);db.commit()
        result=revision.payload
    learning.export_pending()
    return result
