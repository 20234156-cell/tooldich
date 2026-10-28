"""Re-recognize a verified completed VSF scan after an OCR code/model fix."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.ocr.vsf_adapter import VideoSubFinderAdapter
from src.pipeline.orchestrator import orchestrator


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('project_id')
    parser.add_argument('--adopt-completed-scan', action='store_true', help='Only after independently verifying the scanner completed for this source/ROI')
    args = parser.parse_args()
    root = orchestrator.get_project_dir(args.project_id)
    source = orchestrator.get_source_video_path(args.project_id)
    roi = orchestrator.get_roi_set(args.project_id).get_primary_subtitle_roi()
    directory = root / 'transcript'
    manifest = directory / 'vsf_scan.json'
    if args.adopt_completed_scan:
        images = list((directory / 'RGBImages').glob('*.jpeg'))
        if not images or roi is None:
            raise ValueError('Missing completed scan images or ROI')
        manifest.write_text(json.dumps({'key': VideoSubFinderAdapter.scan_cache_key(source, roi), 'image_count': len(images), 'time_offset': 0,
                                        'adopted': True}), encoding='utf-8')
    if not manifest.is_file():
        raise ValueError('No verified completed scan manifest; run normal OCR first')
    last = [-1]
    def progress(percent, stage):
        if percent != last[0]:
            last[0] = percent
            state = orchestrator.get_state(args.project_id)
            state.get_stage('OCR_READY').progress = percent
            orchestrator.save_state(args.project_id, state)
            print(f'{percent}% {stage}', flush=True)
    start = time.perf_counter()
    cues = orchestrator.run_ocr(args.project_id, progress_callback=progress)
    report = {'cue_count': len(cues), 'seconds': round(time.perf_counter()-start, 2),
              'average_confidence': sum(c.confidence for c in cues)/len(cues),
              'low_confidence_count': sum(c.confidence < 70 for c in cues)}
    (root / 'audit').mkdir(exist_ok=True)
    (root / 'audit/ocr_recovery.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
