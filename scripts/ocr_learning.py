"""Read-only proposal/inventory/replay runs; never manufactures confirmed labels."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['init','inventory','analyze','replay','propose-rule'])
    parser.add_argument('--project')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--evidence-limit',type=int,default=0)
    parser.add_argument('--condition')
    parser.add_argument('--action',default='CẦN NGƯỜI')
    args=parser.parse_args()
    from config.settings import settings
    from src.ocr import learning
    from src.ocr.base import Cue
    from src.database import connection
    from src.database.models import CueReviewRevision
    learning.ensure_files()
    rules,records=learning.read_memory()
    result=dict(command=args.command,created_at=time.time(),no_learning_history=not records,
                actual_changes=0,auto_processed=0,visually_reviewed=0,write_errors=[])
    if args.command=='init':
        # One additive table; no ProjectManager startup/recovery on the user's DB.
        CueReviewRevision.__table__.create(bind=connection.engine,checkfirst=True)
        result.update(status='initialized',history_records=len(records),rules=len(rules))
    elif args.command=='propose-rule':
        result['rule']=learning.propose_rule(args.condition,args.action)
        result['status']='experimental_only'
    elif args.command=='replay':
        labels=learning.active_labels(records)
        checks=[]
        for label in labels:
            if not label.get('eligible_for_recognition_training'):
                continue
            for frame in label['evidence']['frames']:
                image=learning.root()/'evidence'/label['evidence']['token']/frame['crop_name']
                if not image.resolve().is_relative_to(learning.root()):
                    raise ValueError('Ảnh ngoài bộ nhớ học.')
                import cv2
                from src.ocr.rapidocr_engine import RapidOCREngine
                if 'engine' not in locals():engine=RapidOCREngine()
                crop=cv2.imread(str(image))
                if crop is None:raise ValueError('Không đọc được ảnh nhãn.')
                text,confidence=engine.recognize(crop)
                final=[(c.get('corrected_text') or c.get('text') or '').strip() for c in label['after']]
                match=text in final if label['action'] in {'GIỮ','SỬA'} else None
                checks.append(dict(review_id=label['id'],video_hash=label['video_hash'],cue_ids=label['cue_ids'],
                                   recognized=text,confidence=confidence,expected=final,
                                   recognition_matches_label=match,
                                   decision_action=label['action'],
                                   structure_or_role_requires_review=match is None))
        result.update(status='no_verified_labels' if not labels else 'no_replayable_recognition_labels' if not checks else 'replayed',
                      verified_labels=len(labels),checks=checks,
                      mismatches=sum(x['recognition_matches_label'] is False for x in checks),
                      note='Lệch được báo cáo; không đổi luật để khớp. Không tự suy ra vai trò chữ hoặc timing từ recognizer.')
    else:
        if not settings.DATABASE_URL.startswith('sqlite:///'):
            raise ValueError('Read-only inventory hiện cần SQLite.')
        path=Path(settings.DATABASE_URL[10:])
        if not path.is_absolute():path=settings.BASE_DIR/path
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
            query='SELECT t.project_id,t.segments FROM transcripts t JOIN projects p ON p.id=t.project_id'
            rows=db.execute(query+(' WHERE p.id=?' if args.project else ''),(args.project,) if args.project else ()).fetchall()
        result['projects']=[]
        for project_id,raw in rows:
            cues=[Cue.model_validate(c) for c in json.loads(raw)]
            if args.command=='inventory':
                result['projects'].append(dict(project_id=project_id,total_cues=len(cues),
                    legacy_corrected=[dict(cue_id=c.index,text=c.corrected_text,
                        provenance='unknown: AI hoặc người dùng; chưa nhập làm nhãn đúng')
                        for c in cues if (c.corrected_text or '').strip()]))
            else:
                report=learning.analyze(cues)
                if args.evidence_limit:
                    if not 0<=args.evidence_limit<=20:raise ValueError('evidence-limit phải từ0 đến20.')
                    from src.pipeline.orchestrator import PipelineOrchestrator
                    from src.ocr.review_evidence import prepare
                    runner=PipelineOrchestrator()
                    selected=report['proposals'][:args.evidence_limit]
                    evidence=[]
                    for row in selected:
                        try:
                            package=prepare(runner,project_id,row['cue_id'])
                            evidence.append(dict(cue_id=row['cue_id'],token=package['token'],
                                                 complete=package['complete'],frames=len(package['frames'])))
                        except Exception as exc:
                            evidence.append(dict(cue_id=row['cue_id'],error=str(exc)))
                            result['write_errors'].append(f'{project_id}/#{row["cue_id"]}: {exc}')
                    report['evidence_prepared']=evidence
                result['projects'].append(dict(project_id=project_id,**report))
        result['status']='completed_with_evidence_errors' if result['write_errors'] else 'completed_read_only'
    text=json.dumps(result,ensure_ascii=False,indent=2)
    if args.output:
        if args.output.resolve().is_relative_to(settings.storage_path.resolve()):
            raise ValueError('Báo cáo phải nằm ngoài storage/projects.')
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(text,encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    print(text)


if __name__=='__main__':main()
