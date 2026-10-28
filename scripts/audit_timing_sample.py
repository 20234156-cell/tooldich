"""Stress-check fitted TTS on the densest already-translated cues, reusing cache."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.pipeline.orchestrator import orchestrator
from src.tts.timeline import AudioTimelineBuilder


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('project_id')
    parser.add_argument('--count', type=int, default=8)
    args = parser.parse_args()
    root = orchestrator.get_project_dir(args.project_id)
    translated = {}
    for path in (root/'translation_cache').glob('*.json'):
        translated.update({row['id']: row['translated'] for row in json.loads(path.read_text(encoding='utf-8'))})
    candidates = [cue for cue in orchestrator.get_cues(args.project_id) if cue.index in translated]
    candidates.sort(key=lambda cue: len(translated[cue.index].split())/max(.5, cue.duration), reverse=True)
    samples = [cue.model_copy(update={'vi_text': translated[cue.index], 'start': i*15, 'end': i*15+cue.duration}) for i, cue in enumerate(candidates[:args.count])]
    if not samples:
        raise ValueError('No completed translation batches available')
    result = AudioTimelineBuilder().synthesize_cues(samples, root/'audit/timing_sample')
    report = {'selection': 'densest translated cues, not a representative average',
              'mean_drift': result['mean_drift'], 'max_drift': result['max_drift'],
              'cues': [{**row, 'text': next(c.vi_text for c in samples if c.index == row['cue_index'])} for row in result['timeline']]}
    (root/'audit/timing_sample/report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
