"""Compare production OCR with experimental native-resolution ROI-only video.

Real local audit, outside pytest. Never modifies project records or production defaults.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cv2
from src.ocr.vsf_adapter import VideoSubFinderAdapter
from src.pipeline.render import get_ffmpeg_bin
from src.pipeline.roi import ROIBox, to_pixel_rect


class MeasuredAdapter(VideoSubFinderAdapter):
    def __init__(self):
        super().__init__()
        self.times = {}

    def _create_scan_proxy(self, *args, **kwargs):
        start = time.perf_counter()
        result = super()._create_scan_proxy(*args, **kwargs)
        self.times['prepare'] = time.perf_counter()-start
        return result

    def recognize_vsf_images(self, *args, **kwargs):
        start = time.perf_counter()
        result = super().recognize_vsf_images(*args, **kwargs)
        self.times['ocr'] = time.perf_counter()-start
        return result


def geometry(roi, width, height):
    x, y, w, h = to_pixel_rect(roi, width, height)
    # Context around text, even dimensions required by H.264 chroma sampling.
    left = max(0, x-32)//2*2
    top = max(0, y-32)//2*2
    right = min(width, x+w+32)//2*2
    bottom = min(height, y+h+32)//2*2
    local = ROIBox(x=(x-left)/(right-left), y=(y-top)/(bottom-top),
                   width=w/(right-left), height=h/(bottom-top))
    return (left, top, right-left, bottom-top), local


def compare(reference, candidate):
    matches = []
    for cue in reference:
        possible = [other for other in candidate if min(cue.end, other.end) > max(cue.start, other.start)]
        exact = [other for other in possible if other.text == cue.text]
        matches.append({'reference_id': cue.index, 'text': cue.text,
                        'exact_text_overlap': bool(exact),
                        'candidates': [{'text': c.text, 'start': c.start, 'end': c.end} for c in possible]})
    return {'reference_count': len(reference), 'candidate_count': len(candidate),
            'exact_text_overlap_count': sum(m['exact_text_overlap'] for m in matches),
            'comparisons': matches}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video', type=Path)
    parser.add_argument('roi_json', type=Path)
    parser.add_argument('--starts', type=float, nargs='+', default=[30, 900, 1800])
    parser.add_argument('--seconds', type=float, default=20)
    parser.add_argument('--rounds', type=int, default=2)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.seconds <= 0 or args.rounds < 1 or any(s < 0 for s in args.starts):
        parser.error('Invalid sample durations/rounds')
    args.output.mkdir(parents=True, exist_ok=False)
    roi = next(ROIBox(**r) for r in json.loads(args.roi_json.read_text(encoding='utf-8'))['rois']
               if r.get('type') == 'chinese_subtitle' and r.get('enabled', True))
    cap = cv2.VideoCapture(str(args.video))
    width, height = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    duration = cap.get(cv2.CAP_PROP_FRAME_COUNT)/cap.get(cv2.CAP_PROP_FPS)
    cap.release()
    crop, local_roi = geometry(roi, width, height)
    report = {'video': str(args.video.resolve()), 'roi': roi.model_dump(), 'crop_pixels': crop,
              'source_size': args.video.stat().st_size, 'source_mtime_ns': args.video.stat().st_mtime_ns,
              'samples': [], 'production_changed': False, 'quality_certified': False}
    ffmpeg = str(get_ffmpeg_bin())
    try:
        for repeat in range(args.rounds):
            for start in args.starts:
                if start+args.seconds > duration:
                    raise ValueError('Sample extends beyond source')
                results = {}
                sample = {'round': repeat, 'start': start, 'duration': args.seconds, 'variants': {}}
                report['samples'].append(sample)
                # Alternate order to reduce systematic warm-up bias.
                for variant in (['baseline', 'roi_native'] if repeat % 2 == 0 else ['roi_native', 'baseline']):
                    out = args.output/f'round{repeat}_{start:g}'/variant
                    out.mkdir(parents=True)
                    adapter = MeasuredAdapter()
                    begin = time.perf_counter()
                    if variant == 'baseline':
                        cues = adapter.extract_cues(args.video, roi, out, start_time=start, max_duration=args.seconds)
                    else:
                        proxy = out/'roi.mp4'
                        x,y,w,h = crop
                        prep = time.perf_counter()
                        subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-ss', str(start),
                                        '-i', str(args.video), '-t', str(args.seconds), '-an', '-vf', f'crop={w}:{h}:{x}:{y}',
                                        '-c:v','h264_nvenc','-preset','p1','-cq','26','-rc','vbr',str(proxy)],
                                       check=True, capture_output=True, timeout=120)
                        adapter.times['prepare'] = time.perf_counter()-prep
                        cues = adapter._run_vsf(adapter.get_executable(), proxy, local_roi, out, 0, args.seconds)
                        for cue in cues:
                            cue.start = round(cue.start+start, 3)
                            cue.end = round(cue.end+start, 3)
                        from src.ocr.deduplicate import deduplicate_cues
                        cues = deduplicate_cues(cues)
                    total = time.perf_counter()-begin
                    results[variant] = cues
                    sample['variants'][variant] = dict(adapter.times, total=total, cue_count=len(cues), warnings=adapter.warnings,
                                                       images=len(list((out/'RGBImages').glob('*'))))
                    (out/'cues.json').write_text(json.dumps([c.model_dump() for c in cues], ensure_ascii=False, indent=2), encoding='utf-8')
                    print(json.dumps({'start': start,'round': repeat, 'variant':variant, **sample['variants'][variant]}, ensure_ascii=True),flush=True)
                sample['comparison'] = compare(results['baseline'], results['roi_native'])
                (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        report['status'] = 'measured_not_approved'
    except Exception as exc:
        report.update(status='failed', error=type(exc).__name__+': '+str(exc))
        raise
    finally:
        (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__ == '__main__':
    main()
