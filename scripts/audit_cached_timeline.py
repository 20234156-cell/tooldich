"""Measure a full real-audio timeline without provider calls or DB writes."""
import argparse
import json
from pathlib import Path
import sys
import time
import re

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.pipeline.orchestrator import orchestrator
from src.tts.edge import EdgeTTSProvider
from src.tts.timeline import AudioTimelineBuilder


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('project_id')
    parser.add_argument('--baseline',action='store_true')
    parser.add_argument('--allow-provider', action='store_true',
                        help='Explicit live audit: synthesize missing matching voices outside pytest')
    parser.add_argument('--output-name', help='Separate audit directory name; preserves earlier measurements')
    parser.add_argument('--fitted-review-plan', type=Path, action='append', default=[],
                        help='Replay measured +30%% on reviewed IDs only; DB remains unchanged')
    args=parser.parse_args()
    cues=orchestrator.get_cues(args.project_id)
    reviewed = set()
    by_id = {cue.index: cue for cue in cues}
    for path in args.fitted_review_plan:
        plan = json.loads(path.read_text(encoding='utf-8'))
        if plan['project_id'] != args.project_id or plan['expected_cue_count'] != len(cues):
            raise ValueError('Review plan project/cue identity differs from DB')
        for index, patch in plan['patches'].items():
            cue = by_id[int(index)]
            if any(getattr(cue, field) != value for field, value in patch.items()):
                raise ValueError(f'Review plan no longer matches DB cue {index}')
            reviewed.add(cue.index)
    if args.baseline and reviewed:
        parser.error('Baseline and fitted review overrides are mutually exclusive')
    if args.baseline:
        for cue in cues:
            cue.tts_rate=None
    for cue in cues:
        if cue.index in reviewed:
            cue.tts_rate = '+30%'
    name=args.output_name or ('timeline_v5_baseline' if args.baseline else 'timeline_v5_saved')
    if not re.fullmatch(r'[A-Za-z0-9_-]+',name):
        parser.error('Output name must be a single plain directory name')
    directory=orchestrator.get_project_dir(args.project_id)/'audit'/name
    provider=EdgeTTSProvider()
    def no_network(*args,**kwargs):
        raise RuntimeError('Cache-only audit: required matching real voice cache is missing')
    if not args.allow_provider:
        provider.synthesize=no_network
    last=[-5]
    def progress(percent,message):
        if percent >= last[0]+5:
            last[0]=percent
            print(json.dumps({'progress':percent,'stage':message}),flush=True)
    started=time.perf_counter()
    result=AudioTimelineBuilder(provider=provider).synthesize_cues(cues,directory,progress_callback=progress)
    rows = result['timeline']
    mean = sum(row['drift'] for row in rows)/len(rows) if rows else 0
    maximum = max((row['drift'] for row in rows), default=0)
    issues = [row for row in rows if row['drift'] > 1.5+1e-6 or
              (mean > .3+1e-6 and row['drift'] > .3)]
    report={'engine':'timeline-v5','cache_only':not args.allow_provider,'baseline':args.baseline,'cue_count':len(cues),
            'reviewed_rate_override_ids':sorted(reviewed),
            'seconds':round(time.perf_counter()-started,2),'mean_drift':mean,
            'max_drift':maximum,'timing_passed':len(rows)==len(cues) and not issues,
            'timing_issue_count':len(issues),
            'scope':'Full real voice timeline only; encoded video QA is separate',
            'worst_cues':sorted(result['timeline'],key=lambda row:row['drift'],reverse=True)[:20]}
    (directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key:value for key,value in report.items() if key!='worst_cues'}),flush=True)


if __name__=='__main__':
    main()
