"""Real ABBA measurement of source-OCR I/O; use a dedicated audit project."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def semantic(evidence):
    return [{key:row.get(key) for key in ('cue_id','action','text','confidence','source_cover','observations')}
            for row in evidence['decisions']]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    import cv2
    from src.pipeline.orchestrator import orchestrator as o
    from src.ocr import bilingual_recheck as module
    from src.ocr.frame_reader import FrameReader
    from src.ocr.vsf_adapter import VideoSubFinderAdapter
    source=o.get_source_video_path(args.project)
    cues=o.get_cues(args.project)
    roi=o.get_roi_set(args.project).get_primary_subtitle_roi()
    if not cues or roi is None:
        raise ValueError('Audit project needs source cues and an ROI')
    adapter=VideoSubFinderAdapter();engine=adapter._recognizer()
    # Warm the real selected runtime on an actual source image before timing.
    cap=cv2.VideoCapture(str(source));cap.set(cv2.CAP_PROP_POS_MSEC,cues[0].start*1000)
    ok,frame=cap.read();cap.release()
    if not ok:raise RuntimeError('Cannot read warmup source frame')
    h,w=frame.shape[:2]
    crop=frame[int(roi.y*h):min(h,int((roi.y+roi.height+.06)*h)),int(roi.x*w):int((roi.x+roi.width)*w)]
    for preprocess in (False,True):engine.read_boxes(crop,preprocess=preprocess)
    result=dict(project_id=args.project,source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                engine_version=engine.cache_version,runs=[],status='running',
                limitations=['Single machine ABBA diagnostic, warm OCR model; not an end-to-end speedup.'])
    expected=None
    try:
        for index,arm in enumerate(('baseline','optimized','optimized','baseline')):
            module.FrameReader=(lambda cap,check:FrameReader(cap,check,max_forward=-1)) if arm=='baseline' else FrameReader
            module.PUBLISH_EVERY=1 if arm=='baseline' else 12
            path=args.output/f'{index}_{arm}.json';writes=written=0
            def save(evidence):
                nonlocal writes,written
                data=json.dumps(evidence,ensure_ascii=False,indent=2).encode('utf-8')
                temporary=path.with_suffix('.tmp');temporary.write_bytes(data);temporary.replace(path)
                writes+=1;written+=len(data)
            start=time.perf_counter()
            output,evidence=module.recover(source,roi,cues,engine,lambda:None,save=save)
            seconds=time.perf_counter()-start
            data=dict(cues=[c.model_dump() for c in output],decisions=semantic(evidence))
            parity=True if expected is None else data==expected
            if expected is None:expected=data
            row=dict(arm=arm,seconds=seconds,decode=evidence['decode'],writes=writes,
                     written_bytes=written,resolved=evidence['resolved'],exact_evidence_and_cue_parity=parity)
            result['runs'].append(row)
            (args.output/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(row),flush=True)
            if not parity:raise RuntimeError('Source pixels/OCR/decisions changed; do not accept optimization')
        result['status']='passed_exact_parity'
    except BaseException as exc:
        result.update(status='failed',error_type=type(exc).__name__,error=str(exc))
        raise
    finally:
        module.FrameReader=FrameReader;module.PUBLISH_EVERY=12
        adapter.release_ocr()
        (args.output/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
