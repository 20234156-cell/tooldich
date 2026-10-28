"""Measure real TTS/timing on an available translated prefix without changing DB."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.jobs.logger import PipelineException
from src.pipeline.orchestrator import orchestrator
from src.tts.timeline import AudioTimelineBuilder
from src.ocr.base import Cue
from src.tts.edge import EdgeTTSProvider


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('project_id')
    parser.add_argument('--max-cues', type=int, default=100)
    parser.add_argument('--concurrency', type=int, default=4)
    parser.add_argument('--measure-auto-fit', action='store_true')
    parser.add_argument('--start-cue', type=int, default=1)
    parser.add_argument('--rate', choices=['+0%', '+30%'])
    parser.add_argument('--snapshot', type=Path, help='Explicit historical audit snapshot; never writes DB')
    parser.add_argument('--cache-only', action='store_true')
    args = parser.parse_args()
    source = ([Cue.model_validate(row) for row in json.loads(args.snapshot.read_text(encoding='utf-8'))]
              if args.snapshot else orchestrator.get_cues(args.project_id))
    cues = [cue for cue in source if cue.vi_text and cue.index >= args.start_cue][:args.max_cues]
    offset = cues[0].start if cues and args.start_cue > 1 else 0
    for cue in cues:
        cue.start -= offset
        cue.end -= offset
    if args.rate:
        for cue in cues:
            cue.tts_rate = args.rate
    name = 'prefix_voice' if args.start_cue == 1 else f'segment_voice_{args.start_cue}'
    if args.snapshot:
        name += '_snapshot'
    if args.rate:
        name += '_rate'+args.rate.strip('+%')
    directory = orchestrator.get_project_dir(args.project_id)/'audit'/name
    if args.measure_auto_fit:
        timeline = json.loads((directory/'voiceover/timeline.json').read_text(encoding='utf-8'))
        if isinstance(timeline, dict):
            timeline = timeline['timeline']
        mean = sum(row['drift'] for row in timeline)/len(timeline)
        affected = set()
        for position, row in enumerate(timeline):
            if row['drift'] <= 1.5 and not (mean > .3 and row['drift'] > .3):
                continue
            left, right = position, position
            while left > 0 and timeline[left-1]['drift'] > .05:
                left -= 1
            while right+1 < len(timeline) and timeline[right+1]['drift'] > .05:
                right += 1
            affected.update(item['cue_index'] for item in timeline[left:right+1])
        for cue in cues:
            if cue.index in affected:
                cue.tts_rate = '+30%'
        directory = directory.with_name(directory.name+'_auto_fit')
        print(f'Measuring real +30% synthesis on {len(affected)} affected cues', flush=True)
    directory.mkdir(parents=True, exist_ok=True)
    provider = EdgeTTSProvider()
    if args.cache_only:
        def no_network(*args, **kwargs):
            raise RuntimeError('Cache-only segment audit requires matching completed real audio')
        provider.synthesize = no_network
    start = time.perf_counter()
    for attempt in range(3):
        last = [-5]
        def progress(percent, message):
            if percent >= last[0]+5:
                last[0] = percent
                print(f'{percent}% {message}', flush=True)
        try:
            result = AudioTimelineBuilder(provider=provider,max_concurrency=max(1,args.concurrency//(4**attempt))).synthesize_cues(cues, directory, progress_callback=progress)
            report = {'cue_count': len(cues), 'seconds': round(time.perf_counter()-start,2),
                      'source_offset':offset, 'snapshot':str(args.snapshot) if args.snapshot else None,
                      'cache_only':args.cache_only,
                      'scope':'Selected cues only; no incoming backlog from preceding cues; not encoded-video QA',
                      'mean_drift': result['mean_drift'], 'max_drift': result['max_drift'],
                      'worst_cues': sorted(result['timeline'], key=lambda row: row['drift'], reverse=True)[:30]}
            (directory/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({key:value for key,value in report.items() if key != 'worst_cues'}, ensure_ascii=False), flush=True)
            return
        except PipelineException as exc:
            print(f'Attempt {attempt+1}: {exc}', flush=True)
            if not exc.details.get('retryable') or attempt == 2:
                raise


if __name__ == '__main__':
    main()
