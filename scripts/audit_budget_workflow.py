"""Run the main Auto implementation on an isolated copy of a real project.

Providers/videos are real. No pytest fixtures or business-project mutations.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import marshal

repo=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(repo))
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('project_id')
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--voice-only',action='store_true')
parser.add_argument('--shorten',action='store_true')
parser.add_argument('--skip-source-review', action='store_true',
                    help='Isolate timing text recovery without requesting new source borrowing')
parser.add_argument('--inspect-local-edges', action='store_true',
                    help='Measure exact missing edge lengths using local VAD and source images')
parser.add_argument('--resume',action='store_true')
parser.add_argument('--restore-unshortened',action='store_true',help='Audit copy only: replay from pre-shortening AI translations')
args=parser.parse_args()
if args.inspect_local_edges and (not args.voice_only or args.shorten):
    parser.error('--inspect-local-edges requires --voice-only without --shorten')
if args.inspect_local_edges:
    os.environ['OFFLINE_MODE']='true'
root=args.output.resolve()
if root==repo or repo.is_relative_to(root) or root.is_relative_to((repo/'storage/projects').resolve()):
    raise SystemExit('Audit output must be isolated from business projects and workspace root')
root.mkdir(parents=True,exist_ok=True)
os.environ['DATABASE_URL']='sqlite:///'+(root/'audit.db').as_posix()
os.environ['STORAGE_DIR']=str(root/'projects')
os.environ['OCR_LEARNING_DIR']=str(root/'learning')

import soundfile as sf
from config.settings import settings
from src.database.connection import init_db,SessionLocal
from src.database.models import Project,Media,Transcript,Source
from src.jobs.logger import mask_secrets
from src.ocr.base import Cue
from src.pipeline.orchestrator import orchestrator
from src.pipeline.render import RenderConfig,get_ffmpeg_bin
from src.pipeline.attempts import artifact_path
from src.tts.cache import TTSCache
from src.tts.registry import provider_for_voice
from src.tts.normalizer_vi import normalize_vietnamese_text
from src.tts.speech_text import spoken_text
from src.tts.timeline import AudioTimelineBuilder
from src.translation.memory import atomic_json

def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def business_snapshot():
    with sqlite3.connect((repo/'storage/app.db').as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        media=dict(db.execute('select * from media where project_id=?',(args.project_id,)).fetchone())
        tr=db.execute('select * from transcripts where project_id=?',(args.project_id,)).fetchone()
        source=db.execute('select * from sources where project_id=?',(args.project_id,)).fetchone()
        return media,dict(tr) if tr else None,dict(source) if source else None

original=repo/'storage/projects'/args.project_id
pid='audit_'+args.project_id
target=settings.storage_path/pid
started=time.monotonic()
media,transcript,source=business_snapshot()
video=(repo/media['file_path']).resolve()
raw=transcript['segments'] if transcript else None
snapshot=dict(project_id=args.project_id,source_path=str(video),source_sha256=sha(video),
              transcript_sha256=hashlib.sha256(raw.encode()).hexdigest() if raw is not None else None)
options=json.loads((original/'auto_workflow.json').read_text(encoding='utf8')).get('options',{}) if (original/'auto_workflow.json').is_file() else {}
voice=options.get('voice','piper:ngochuyen');rate=options.get('rate','+15%');pitch=options.get('pitch','+0Hz')
if not args.resume:
    if (root/'audit.db').exists() or target.exists():raise SystemExit('Existing audit preserved; specify --resume')
    atomic_json(root/'inventory.json',snapshot)
    init_db();target.mkdir(parents=True);(target/'source').mkdir()
    for name in ('roi.json','subtitle_settings.json','trim.json'):
        file=original/'source'/name
        if file.is_file():shutil.copyfile(file,target/'source'/name)
    shutil.copyfile(original/'pipeline_state.json',target/'pipeline_state.json')
    if (original/'transcript/cues').is_dir():
        shutil.copytree(original/'transcript/cues',target/'transcript/cues')
    with SessionLocal() as db:
        db.add(Project(id=pid,title='Budget workflow audit: '+args.project_id));db.flush()
        payload={k:v for k,v in media.items() if k not in ('id','project_id')}
        payload['file_path']=str(video);db.add(Media(project_id=pid,**payload))
        if source:
            db.add(Source(project_id=pid,**{k:v for k,v in source.items() if k not in ('id','project_id')}))
        if transcript:
            db.add(Transcript(project_id=pid,language=transcript['language'],
                              target_language=transcript['target_language'],segments=json.loads(raw)))
        db.commit()
    cache=TTSCache(repo/'storage/projects/tts_cache');own=TTSCache()
    provider=provider_for_voice(voice);checks=[]
    for cue in [Cue.model_validate(c) for c in json.loads(raw or '[]')]:
        text=spoken_text(cue)
        if not text:continue
        key=cache.compute_key(provider=provider.name,voice=voice,normalized_text=normalize_vietnamese_text(text),
            rate=cue.tts_rate or rate,pitch=pitch,sample_rate=provider.sample_rate,
            model_version=provider.cache_version_for_voice(voice) if provider.name=='piper' else getattr(provider,'cache_version','v1'))
        data=cache.get(key)
        if data:
            info=sf.info(io.BytesIO(data));assert info.frames>0
            own.put(key,data)
            checks.append(dict(cue_id=cue.index,cache_key=key,raw_frames=info.frames,
                               sr=info.samplerate,sha256=hashlib.sha256(data).hexdigest()))
    atomic_json(root/'raw_cache_checks.json',checks)
else:
    saved=json.loads((root/'inventory.json').read_text(encoding='utf8'))
    if saved!=snapshot:raise SystemExit('Business source/transcript changed; preserve old audit and inspect first')

if args.restore_unshortened:
    if not args.resume:raise SystemExit('--restore-unshortened requires an existing isolated audit')
    before=orchestrator.get_cues(pid);current={c.index:c for c in before};restored=[]
    previous_restores=set()
    for receipt in root.glob('replay_restore_*.json'):
        previous_restores.update(json.loads(receipt.read_text(encoding='utf8')).get('processed_plans',[]))
    processed=[]
    for path in reversed(sorted((target/'audit').glob('timing_shortening_*.json'))):
        if path.name in previous_restores:continue
        plan=json.loads(path.read_text(encoding='utf8'))
        if plan.get('status')!='applied':continue
        processed.append(path.name)
        for change in plan.get('changes',[]):
            cue=current.get(change['cue_index'])
            if cue is None:continue  # never resurrect a proven source-unit merge
            if cue.vi_text==change['before']:continue
            if cue.vi_text!=change['after'] or cue.translation_origin=='manual':
                raise SystemExit('Audit text changed independently; do not overwrite it')
            current[cue.index]=cue.model_copy(update=dict(vi_text=change['before'],
                translation_origin=change.get('previous_origin','ai'),
                is_approved=change.get('previous_approved',False),voice_audio_path=None,
                voice_duration=None,tts_rate=None))
            restored.append(dict(cue_id=cue.index,before=change['after'],after=change['before'],plan=path.name))
    if restored:
        from src.ocr.learning import review_context
        with review_context(decided_by='agent',human_confirmed=False,
                reason='Audit đối chứng: phục hồi lời AI trước rút gọn để kiểm cơ chế mới, không đổi project chính.'):
            orchestrator.save_cues(pid,[current[c.index] for c in before],history=dict(action='SỬA',expected_before=before))
    if processed:
        atomic_json(root/f'replay_restore_{time.time_ns()}.json',dict(changes=restored,
            processed_plans=processed,training_label=False))

def progress(percent,message):
    atomic_json(root/'progress.json',dict(percent=percent,message=message,elapsed_seconds=time.monotonic()-started))


def inspect_local_edges():
    import cv2
    from src.ocr.caption_role_review import speech_segments

    report=json.loads((target/'audit/voice_budget/latest.json').read_text(encoding='utf8'))
    source=orchestrator.get_source_video_path(pid)
    source_hash=sha(source)
    if source_hash != report['source_sha256']:
        raise ValueError('Measured schedule and local source differ')
    cues=orchestrator.get_cues(pid)
    by_id={cue.index:cue for cue in cues}
    folder=root/'local_edges'
    folder.mkdir(exist_ok=True)
    started_scan=time.monotonic()
    speech,identity=speech_segments(source,folder)
    cap=cv2.VideoCapture(str(source))
    rows=[]
    combined=[]
    duration=json.loads((folder/'vad.json').read_text(encoding='utf8'))['duration']
    try:
        if not cap.isOpened():
            raise ValueError('Cannot open source for frame inspection')
        for conflict in report['phases'][-1]['result'].get('conflicts', []):
            cue=by_id[conflict['cue_id']]
            missing=conflict['missing_seconds']
            blocked=[(c.start,c.end) for c in cues if c.index!=cue.index]+speech
            left=max(0.,cue.start-.8)
            right=min(duration,cue.end+.8)
            for a,b in blocked:
                if a<cue.start and b>left:
                    left=min(cue.start,b)
                if b>cue.end and a<right:
                    right=max(cue.end,a)
            before=max(0.,cue.start-left)
            after=max(0.,right-cue.end)
            combined.append(dict(cue_id=cue.index,missing_seconds=missing,
                                 before_candidate_seconds=before,after_candidate_seconds=after,
                                 sufficient_before_independent_review=before+after>=missing))
            for side,start,end in [('before',cue.start-missing,cue.start),
                                   ('after',cue.end,cue.end+missing)]:
                reason='unverified'
                frame_hash=None
                if start<0 or end>duration or missing>.8:
                    reason='outside_borrow_budget'
                elif any(c.index!=cue.index and c.start<end and start<c.end for c in cues):
                    reason='overlaps_other_caption'
                elif any(a<end and start<b for a,b in speech):
                    reason='speech_detected'
                else:
                    midpoint=(start+end)/2
                    cap.set(cv2.CAP_PROP_POS_MSEC,midpoint*1000)
                    ok,frame=cap.read()
                    observed=cap.get(cv2.CAP_PROP_POS_MSEC)/1000
                    if ok and start<=observed<=end:
                        ok,encoded=cv2.imencode('.jpg',frame)
                        if ok:
                            data=encoded.tobytes()
                            (folder/f'{cue.index}_{side}.jpg').write_bytes(data)
                            frame_hash=hashlib.sha256(data).hexdigest()
                            reason='candidate_needs_independent_source_evidence'
                    if frame_hash is None:
                        reason='no_frame_inside_edge'
                rows.append(dict(cue_id=cue.index,side=side,start=start,end=end,
                                 missing_seconds=missing,reason=reason,frame_sha256=frame_hash))
    finally:
        cap.release()
    if sha(source)!=source_hash:
        raise ValueError('Source changed during local scan')
    atomic_json(folder/'report.json',dict(source_sha256=source_hash,vad_identity=identity,
                elapsed_seconds=time.monotonic()-started_scan,rows=rows,
                combined_candidate_capacity=combined,authorized_edges=0))

result={}
try:
    if args.voice_only:
        result['voice']=orchestrator.run_voiceover(pid,voice=voice,rate=rate,pitch=pitch,
            auto_fit=True,budgeted=True,shorten_to_fit=args.shorten,
            auto_merge_timing=args.shorten and not args.skip_source_review,progress_callback=progress)
        atomic_json(root/'voice_result.json',result['voice'])
        cfg=RenderConfig.model_validate(orchestrator.get_subtitle_settings(pid))
        orchestrator.run_render(pid,cfg,progress_callback=progress)
        report=orchestrator.verify(pid)
        result['verification']=report.to_dict()
        if not report.passed:raise RuntimeError('Actual encoded output failed project QA; see verify_report.json')
    else:
        result=orchestrator.run_all(pid,voice=voice,rate=rate,pitch=pitch,
            auto_fit=True,shorten_to_fit=True,skip_uncertain=False,extraction_mode='auto',
            audio_mode='mix',bg_volume=.15,voice_volume=1.7,progress_callback=progress)
    output=artifact_path(target,'output/video_vi.mp4')
    if output.is_file():
        decode=subprocess.run([str(get_ffmpeg_bin()),'-v','error','-i',str(output),'-f','null','-'],capture_output=True)
        result.update(output=str(output),output_sha256=sha(output),bytes=output.stat().st_size,
                      decode_returncode=decode.returncode,decode_errors=decode.stderr.decode(errors='replace'))
        if decode.returncode:raise RuntimeError('Full encoded output decode failed')
    result['status']='completed'
except Exception as exc:
    result.update(status='failed',error_type=type(exc).__name__,error=mask_secrets(str(exc)),
                  error_code=getattr(exc,'code',None),details=getattr(exc,'details',{}))
    if args.inspect_local_edges and result['error_code']=='TIMING_REVIEW_REQUIRED':
        try:
            inspect_local_edges()
            result['local_edge_report']='local_edges/report.json'
        except (OSError, ValueError, ImportError, RuntimeError) as scan_error:
            result['local_edge_error']=mask_secrets(str(scan_error))
finally:
    after=business_snapshot()[1]
    after_raw=after['segments'] if after else None
    result.update(elapsed_seconds=time.monotonic()-started,
        business_transcript_unchanged=(hashlib.sha256(after_raw.encode()).hexdigest() if after_raw is not None else None)==snapshot['transcript_sha256'],
        business_source_unchanged=sha(video)==snapshot['source_sha256'])
    runtime=dict(run_all=orchestrator.run_all.__func__,run_voiceover=orchestrator.run_voiceover.__func__,
                 synthesize_cues=AudioTimelineBuilder.synthesize_cues)
    for module,name in [('src.tts.budget_recovery','recover_measured'),('src.tts.cluster_schedule','solve_measured'),
                        ('src.tts.source_windows','review_source_windows')]:
        if module in sys.modules:runtime[name]=getattr(sys.modules[module],name)
    result['runtime_code']={name:hashlib.sha256(marshal.dumps(function.__code__)).hexdigest()
                            for name,function in runtime.items()}
    atomic_json(root/'result.json',result)
    print(json.dumps({k:v for k,v in result.items() if k not in ('voice','details')},ensure_ascii=True),flush=True)
