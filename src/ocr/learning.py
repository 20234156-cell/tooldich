"""Source-review history, transactional outbox and bounded declarative suggestions.

The first version never mutates cues from a learned rule. Human-confirmed labels
are distinct from agent proposals and legacy corrections of unknown provenance.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

from config.settings import settings
from src.database.models import CueReviewRevision
from src.ocr.base import Cue

REVIEW_CONTEXT = ContextVar('ocr_review_context', default={})
LOCK = threading.RLock()
STATUSES = {'thử nghiệm', 'đang dùng', 'tạm ngưng'}
CONDITIONS = {'low_confidence', 'ocr_disagreement', 'scene_layout', 'short_text',
              'confusable_characters', 'temporal_disagreement', 'invalid_timing'}
RULE_TEMPLATE = '''# Luật duyệt nguồn OCR

Chưa có lịch sử học. Không có luật được người dùng phê duyệt.
Phiên bản đầu chỉ đề xuất; không tự sửa/loại/cắt/gộp câu.
Mỗi luật trong khối JSON có id, condition (tín hiệu có sẵn trong code), scope,
exceptions, action, correct_count, wrong_count, status, approved_by, validation.
Luật mới: "thử nghiệm". "đang dùng" cần người dùng xác nhận và kiểm thử riêng.
Điều kiện dùng đặc trưng, không dùng mã câu hoặc tên video. Không thực thi mã từ file.

```json
[]
```
'''


def root():
    return Path(settings.OCR_LEARNING_DIR or settings.BASE_DIR / 'learning').resolve()


def ensure_files():
    folder = root()
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in [('rules.md', RULE_TEMPLATE), ('corrections.jsonl', '')]:
        try:
            with (folder / name).open('x', encoding='utf-8') as stream:
                stream.write(text)
        except FileExistsError:
            pass
    return folder


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def normalized(items):
    return [Cue.model_validate(x.model_dump() if isinstance(x, Cue) else x).model_dump()
            for x in items]


def transcript_hash_matches(items, expected):
    """Preserve pre-role evidence/history without ignoring meaningful changes.

    Old Cue snapshots had no caption_role. Only omission of the new default is
    equivalent; nondefault role and every other field stay in both hashes.
    """
    current = normalized(items)
    if digest(current) == expected:
        return True
    # Accept each historical presence of additive defaults. A populated proof
    # and a nondefault role remain significant in every candidate snapshot.
    for drop_role, drop_proof in ((False, True), (True, False), (True, True)):
        legacy = [{k:v for k,v in row.items()
                   if not (drop_role and k == 'caption_role' and v == 'unclassified')
                   and not (drop_proof and k == 'caption_role_proof' and v is None)}
                  for row in current]
        if digest(legacy) == expected:
            return True
    return False


def _read_memory_unlocked():
    folder = ensure_files()
    text = (folder / 'rules.md').read_text(encoding='utf-8')
    blocks = re.findall(r'```json\s*(.*?)\s*```', text, re.S)
    if len(blocks) != 1:
        raise ValueError('rules.md cần đúng một khối JSON chứa danh sách luật.')
    rules = json.loads(blocks[0])
    if not isinstance(rules, list):
        raise ValueError('Danh sách luật không hợp lệ.')
    seen = set()
    for rule in rules:
        if (not isinstance(rule, dict) or not re.fullmatch(r'R[0-9]{3,8}', str(rule.get('id', '')))
                or rule['id'] in seen or rule.get('condition') not in CONDITIONS
                or rule.get('status') not in STATUSES
                or rule.get('action') not in {'GIỮ', 'SỬA', 'LOẠI', 'CẮT', 'GỘP', 'CẦN NGƯỜI'}
                or not isinstance(rule.get('scope'), dict)
                or set(rule['scope']) - {'source_kind'}
                or rule['scope'].get('source_kind', 'ocr') not in {'ocr','stt'}
                or not isinstance(rule.get('exceptions'), list)
                or any(e not in CONDITIONS for e in rule['exceptions'])
                or any(type(rule.get(k)) is not int or rule[k] < 0 for k in ('correct_count','wrong_count'))):
            raise ValueError('Luật học sai định dạng hoặc chứa điều kiện chưa hỗ trợ.')
        if rule['status'] == 'đang dùng' and (rule.get('approved_by') != 'người dùng'
                or not rule.get('approved_at') or not rule.get('validation', {}).get('passed')):
            raise ValueError(f"{rule['id']}: luật đang dùng thiếu xác nhận/kiểm thử.")
        seen.add(rule['id'])
    records = []
    for number, line in enumerate((folder / 'corrections.jsonl').read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            if not isinstance(item, dict) or not item.get('id'):
                raise ValueError('Thiếu ID')
            records.append(item)
        except (ValueError, TypeError) as exc:
            raise ValueError(f'corrections.jsonl hỏng ở dòng {number}; không bỏ qua lịch sử.') from exc
    return rules, records


@contextmanager
def review_context(**metadata):
    token = REVIEW_CONTEXT.set(metadata)
    try:
        yield
    finally:
        REVIEW_CONTEXT.reset(token)


@lru_cache(maxsize=16)
def _source_hash(path, size, mtime, inode):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def video_identity(path, *, fresh=False):
    path = Path(path).resolve()
    stat = path.stat()
    if fresh:
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()
    return _source_hash(str(path), stat.st_size, stat.st_mtime_ns, stat.st_ino)


def build_revision(runner, project_id, before, after, history):
    before, after = normalized(before), normalized(after)
    expected = history.get('expected_before')
    if expected is not None and normalized(expected) != before:
        raise ValueError('Câu đã thay đổi trong DB. Tải lại trước khi sửa.')
    old, new = {c['index']: c for c in before}, {c['index']: c for c in after}
    if len(old) != len(before) or len(new) != len(after):
        raise ValueError('Mã câu trùng; không thể ghi lịch sử an toàn.')
    changed = {i for i in old.keys() | new.keys() if old.get(i) != new.get(i)}
    changed.update(history.get('cue_ids', []))
    context = REVIEW_CONTEXT.get()
    action = context.get('action_override') or history['action']
    evidence = context.get('evidence', {})
    if evidence.get('transcript_hash') and not transcript_hash_matches(before,evidence['transcript_hash']):
        raise ValueError('Transcript đã đổi sau khi lấy bằng chứng. Hãy đối chiếu lại.')
    errors, crops = [], []
    for index in sorted(changed):
        cue = old.get(index) or new.get(index)
        if not cue or not cue.get('image_path'):
            continue
        try:
            project = runner.get_project_dir(project_id).resolve()
            image = (project / 'transcript' / cue['image_path']).resolve()
            if not image.is_relative_to(project):
                raise ValueError('Đường dẫn ảnh ngoài dự án')
            data = image.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            destination = ensure_files() / 'evidence' / 'crops' / (sha + image.suffix.lower())
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                destination.write_bytes(data)
            crops.append({'cue_id':index, 'sha256':sha, 'path':str(destination.relative_to(root()))})
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
    video_hash = evidence.get('video_hash')
    if not video_hash:
        try:
            video_hash = video_identity(runner.get_source_video_path(project_id))
        except Exception:
            errors.append('Không có video nguồn được kiểm chứng; không dùng làm nhãn học.')
    confirmed = bool(context.get('human_confirmed') and evidence.get('complete')
                     and context.get('decided_by') == 'người dùng')
    payload = dict(id=context.get('decision_id') or uuid.uuid4().hex, schema_version=1, project_id=project_id,
                   created_at=time.time(), video_hash=video_hash, cue_ids=sorted(changed),
                   before=[c for c in before if c['index'] in changed],
                   after=[c for c in after if c['index'] in changed],
                   order_before=[c['index'] for c in before], before_hash=digest(before),
                   after_hash=digest(after), action=action,
                   decided_by=context.get('decided_by', 'agent'),
                   review_status='người dùng xác nhận' if confirmed else 'cần người' if context.get('needs_human') else 'đề xuất',
                   cue_reasons=history.get('cue_reasons', {}),
                   reason=context.get('reason') or 'Thao tác sửa nguồn; chưa có lý do được xác nhận.',
                   cause=context.get('cause') or 'Chưa xác minh nguyên nhân gốc.',
                   rule_id=context.get('rule_id'), confidence=context.get('confidence'),
                   answers=context.get('answers', {}), image_features=context.get('image_features', {}),
                   evidence=evidence, cue_images=crops, evidence_errors=errors,
                   supersedes=context.get('supersedes'), reverses=history.get('reverses'),
                   request_hash=context.get('request_hash'),applied=before!=after,
                   test_fixture=os.environ.get('OCR_LEARNING_TEST_FIXTURE')=='1',
                   eligible_for_recognition_training=bool(confirmed and action in {'GIỮ','SỬA'}
                       and os.environ.get('OCR_LEARNING_TEST_FIXTURE')!='1'
                       and context.get('crop_matches_text') and all(f.get('crop_name') for f in evidence.get('frames',[]))))
    if confirmed and (not video_hash or errors):
        raise ValueError('Thiếu bằng chứng để ghi quyết định người dùng xác nhận.')
    return CueReviewRevision(id=payload['id'], project_id=project_id,
                            created_at=payload['created_at'], payload=payload, exported=False)


@contextmanager
def locked_journal():
    """Serialize JSONL writers across worker processes; recovery deduplicates by ID."""
    path = ensure_files() / 'corrections.jsonl'
    with LOCK, (path.parent / '.journal.lock').open('a+b') as lock:
        if lock.seek(0, 2) == 0:
            lock.write(b'0'); lock.flush()
        lock.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield path
        finally:
            lock.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def export_pending():
    from src.database import connection
    with locked_journal() as path, connection.SessionLocal() as db:
        _, records = _read_memory_unlocked()
        existing = {r['id'] for r in records}
        rows = db.query(CueReviewRevision).filter_by(exported=False).order_by(CueReviewRevision.created_at).all()
        with path.open('a', encoding='utf-8') as stream:
            for row in rows:
                if row.id not in existing:
                    stream.write(json.dumps(row.payload, ensure_ascii=False, allow_nan=False) + '\n')
                    stream.flush(); os.fsync(stream.fileno())
                    existing.add(row.id)
                row.exported = True
                row.export_error = None
        db.commit()
        _refresh_rules(records + [r.payload for r in rows if r.id not in {x['id'] for x in records}])
        return len(rows)


def read_memory():
    with locked_journal():
        rules,records=_read_memory_unlocked()
        from src.database import connection
        from sqlalchemy import inspect
        with connection.SessionLocal() as db:
            if inspect(db.get_bind()).has_table(CueReviewRevision.__tablename__):
                existing={r['id']:r for r in records}
                for row in db.query(CueReviewRevision).order_by(CueReviewRevision.created_at).all():
                    if row.id in existing:
                        if digest(existing[row.id])!=digest(row.payload):
                            raise ValueError('Nhật ký khác lịch sử DB; cần đối chiếu, không tự ghi đè.')
                    else:
                        records.append(row.payload)
        return rules,records


def _write_rules(rules):
    path=ensure_files()/'rules.md'
    text=path.read_text(encoding='utf-8')
    rendered=re.sub(r'```json\s*.*?\s*```',lambda _: '```json\n'+json.dumps(rules,ensure_ascii=False,indent=2)+'\n```',text,count=1,flags=re.S)
    temporary=path.with_name('rules.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(rendered,encoding='utf-8')
    os.replace(temporary,path)


def propose_rule(condition,action,scope=None,exceptions=None):
    if condition not in CONDITIONS or action not in {'GIỮ','SỬA','LOẠI','CẮT','GỘP','CẦN NGƯỜI'}:
        raise ValueError('Chỉ đề xuất điều kiện/hành động đã được định nghĩa; không thực thi luật tự do.')
    scope=scope or {'source_kind':'ocr'}
    if set(scope)-{'source_kind'} or scope.get('source_kind') not in {'ocr','stt'}:
        raise ValueError('Phạm vi chưa hỗ trợ.')
    if any(x not in CONDITIONS for x in (exceptions or [])):
        raise ValueError('Ngoại lệ chưa hỗ trợ.')
    with locked_journal():
        rules,_=_read_memory_unlocked()
        index=max([int(r['id'][1:]) for r in rules] or [0])+1
        rule=dict(id=f'R{index:03}',condition=condition,action=action,scope=scope,
                  exceptions=exceptions or [],correct_count=0,wrong_count=0,status='thử nghiệm',
                  created_at=time.time(),origin='agent đề xuất; chưa phải luật được xác nhận')
        rules.append(rule);_write_rules(rules)
        return rule


def _refresh_rules(records):
    rules,_=_read_memory_unlocked()
    if not rules:
        return
    labels=active_labels(records)
    changed=False
    for rule in rules:
        associated=[r for r in labels if r.get('rule_id')==rule['id']]
        correct={tuple(sorted(x['sha256'] for x in r.get('cue_images',[]))) for r in associated if r['action']==rule['action']}
        wrong={tuple(sorted(x['sha256'] for x in r.get('cue_images',[]))) for r in associated if r['action']!=rule['action']}
        correct.discard(());wrong.discard(())
        values=(len(correct),len(wrong))
        if values!=(rule['correct_count'],rule['wrong_count']):
            rule['correct_count'],rule['wrong_count']=values;changed=True
        if wrong and rule['status']!='tạm ngưng':
            rule.setdefault('status_history',[]).append(dict(status=rule['status'],at=time.time(),reason='Quyết định người dùng khác đề xuất luật'))
            rule['status']='tạm ngưng';changed=True
    if changed:_write_rules(rules)


def active_labels(records):
    revoked = {r.get(k) for r in records if r.get('decided_by') == 'người dùng'
               for k in ('supersedes','reverses') if r.get(k)}
    revoked.update(i for r in records if r.get('decided_by')=='người dùng' for i in r.get('supersedes_ids',[]))
    candidates = [r for r in records if r['id'] not in revoked
            and r.get('decided_by') == 'người dùng'
            and r.get('review_status') == 'người dùng xác nhận'
            and not r.get('test_fixture')
            and r.get('evidence', {}).get('complete') and r.get('video_hash')]
    if not candidates:
        return []
    from src.database import connection
    with connection.SessionLocal() as db:
        saved = {r.id:r.payload for r in db.query(CueReviewRevision).filter(
            CueReviewRevision.id.in_([r['id'] for r in candidates])).all()}
    result=[]
    for record in candidates:
        if record['id'] not in saved or digest(saved[record['id']])!=digest(record):
            raise ValueError('Nhãn xác nhận trong JSONL không khớp lịch sử DB; không dùng để học.')
        evidence=record['evidence']
        packages=[evidence]+([evidence['target']] if evidence.get('target') else [])
        for package in packages:
            token=package.get('token','')
            if not re.fullmatch('[a-f0-9]{32}',token) or len(package.get('frames',[]))<3:
                raise ValueError('Nhãn xác nhận thiếu bằng chứng nguồn.')
            for frame in package['frames']:
                for name,sha in [(frame['name'],frame['sha256']),
                                 (frame.get('crop_name'),frame.get('crop_sha256'))]:
                    if name:
                        path=root()/'evidence'/token/name
                        if Path(name).name!=name or hashlib.sha256(path.read_bytes()).hexdigest()!=sha:
                            raise ValueError('Ảnh nhãn học bị thay đổi.')
        for item in record.get('cue_images',[]):
            path=(root()/item['path']).resolve()
            if not path.is_relative_to(root()) or hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:
                raise ValueError('Ảnh crop lịch sử bị thay đổi.')
        result.append(record)
    return result


def flag_known_conflicts(cues, directory, video):
    """Exact source+image evidence only; never transfer a video-specific cue ID."""
    _, records = read_memory()
    labels = active_labels(records)
    if not labels:
        return cues
    source_hash = video_identity(video)
    labels = [r for r in labels if r['video_hash'] == source_hash]
    output = []
    for cue in cues:
        candidate = cue.model_copy(deep=True)
        if not cue.image_path:
            output.append(candidate); continue
        image = (Path(directory) / cue.image_path).resolve()
        if not image.is_relative_to(Path(directory).resolve()) or not image.is_file():
            output.append(candidate); continue
        sha = hashlib.sha256(image.read_bytes()).hexdigest()
        matching = [r for r in labels if any(x['sha256'] == sha and any(
                    old['index'] == x['cue_id'] and abs(old['start'] - cue.start) <= .001
                    and abs(old['end'] - cue.end) <= .001 for old in r['before'])
                    for x in r.get('cue_images', []))]
        if matching:
            label = max(matching, key=lambda r:r['created_at'])
            final = [(x.get('corrected_text') or x.get('text') or '').strip() for x in label['after']]
            if label['action'] in {'LOẠI','CẮT','GỘP'} or cue.effective_source_text not in final:
                candidate.ocr_alternatives.append(dict(variant='human_review_memory',
                    text=' / '.join(final) or cue.text, review_id=label['id'],
                    reason='Ảnh và nguồn khớp một quyết định người dùng; cần đối chiếu, không tự lặp kết quả cũ.'))
        output.append(candidate)
    return output


def signals(cue):
    found = set()
    text = cue.effective_source_text
    if cue.source_kind == 'ocr' and cue.confidence < 70:
        found.add('low_confidence')
    variants = {a.get('variant') for a in cue.ocr_alternatives}
    if cue.ocr_alternatives:
        found.add('ocr_disagreement')
    if variants & {'scene_layout_review','dominant_text_size'}:
        found.add('scene_layout')
    if 'temporal_vote' in variants:
        found.add('temporal_disagreement')
    if len(text) <= 1:
        found.add('short_text')
    if re.search(r'[01lIO5S]', text):
        found.add('confusable_characters')
    if not text or cue.end <= cue.start:
        found.add('invalid_timing')
    return found


def analyze(cues, *, video_hash=None):
    rules, records = read_memory()  # Always read memory before applying signals.
    result = []
    for cue in cues:
        found = signals(cue)
        matches = [r for r in rules if r['status'] != 'tạm ngưng'
                   and r['condition'] in found and not found.intersection(r['exceptions'])
                   and r['scope'].get('source_kind', cue.source_kind) == cue.source_kind]
        active = [r['id'] for r in matches if r['status'] == 'đang dùng']
        experimental = [r['id'] for r in matches if r['status'] == 'thử nghiệm']
        conflict = len({r['action'] for r in matches}) > 1
        if found or matches:
            result.append(dict(cue_id=cue.index, signals=sorted(found), active_rules=active,
                               experimental_rules=experimental, conflict=conflict,
                               action='CẦN NGƯỜI' if conflict else 'ĐỀ XUẤT KIỂM TRA',
                               proposed_actions=[{'rule_id':r['id'],'action':r['action']} for r in matches]))
    confirmed = active_labels(records)
    counts = {}
    for record in confirmed:
        key = record.get('cause', '')
        hashes = tuple(sorted(x['sha256'] for x in record.get('cue_images', [])))
        if hashes and key and key != 'Chưa xác minh nguyên nhân gốc.':
            counts.setdefault(key, set()).add(hashes)
    return dict(version='ocr-learning-v1', mode='suggest_only', total_cues=len(cues),
                history_records=len(records), confirmed_labels=len(confirmed),
                recognition_training_labels=sum(bool(r.get('eligible_for_recognition_training')) for r in confirmed),
                no_learning_history=not confirmed, actual_changes=0, auto_processed=0,
                visually_reviewed=0, suspended_rule_ids=[r['id'] for r in rules if r['status']=='tạm ngưng'],
                need_human=len(result), unconfirmed_proposals=len(result),
                proposals=result, active_rule_ids=sorted({i for r in result for i in r['active_rules']}),
                experimental_rule_ids=sorted({i for r in result for i in r['experimental_rules']}),
                regression_needed=[{'cause':k,'distinct_evidence':len(v)} for k,v in counts.items() if len(v)>=3],
                video_hash=video_hash, write_errors=[])
