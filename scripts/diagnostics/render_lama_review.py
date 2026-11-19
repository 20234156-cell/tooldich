"""Review-only subtitle restoration; no changes to project state or Auto defaults.

Integration target: render preprocessing before ASS, conditional on visual approval.
Acceptance: no source glyph/shadow residue, stable moving background, unchanged
duration/audio, and only one Vietnamese subtitle layer. Outputs remain review-only.
"""
import argparse
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np


from src.video.lama import active_box, expanded_box, scan, Restorer, TemporalRestorer


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--cues', type=Path, required=True)
    p.add_argument('--audio', type=Path, required=True)
    p.add_argument('--filter', type=Path, required=True)
    p.add_argument('--model', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--band', type=int, nargs=2, required=True)
    p.add_argument('--anchor-stride', type=int, choices=(1, 2, 3), default=3)
    p.add_argument('--ffmpeg', default='ffmpeg')
    p.add_argument('--review-masks', type=Path,
                   help='Explicit precomputed scan manifest with source SHA256 and band; never auto-reused')
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    cues = json.loads(args.cues.read_text(encoding='utf-8'))
    source_hash = hashlib.file_digest(args.source.open('rb'), 'sha256').hexdigest()
    if args.review_masks:
        manifest = json.loads(args.review_masks.read_text(encoding='utf-8'))
        if manifest['source_sha256'] != source_hash or manifest['band'] != args.band:
            raise ValueError('Scan source/band mismatch')
        rows = manifest['rows']
        if [(r['index'], r['start'], r['end']) for r in rows] != [(c['index'], c['start'], c['end']) for c in cues]:
            raise ValueError('Scan cue identity mismatch')
    else:
        rows = scan(args.source, cues, args.band, args.output / 'masks.json')
    (args.output / 'scan-manifest.json').write_text(json.dumps(dict(
        source_sha256=source_hash, band=args.band, rows=rows), indent=2), encoding='utf-8')
    restore = TemporalRestorer(Restorer(args.model), args.anchor_stride)
    cap = cv2.VideoCapture(str(args.source))
    fps, width, height, total = cap.get(5), int(cap.get(3)), int(cap.get(4)), int(cap.get(7))
    tmp = args.output / 'video-vi.partial.mp4'
    log = (args.output / 'encode.log').open('w')
    cmd = [args.ffmpeg, '-hide_banner', '-y', '-f', 'rawvideo', '-pix_fmt', 'bgr24',
           '-s', f'{width}x{height}', '-r', str(fps), '-i', '-', '-i', str(args.audio),
           '-filter_complex', args.filter.read_text(encoding='utf-8').strip(), '-map', '[v_out]', '-map', '1:a:0',
           '-c:v', 'h264_nvenc', '-preset', 'p5', '-cq', '19', '-pix_fmt', 'yuv420p',
           '-c:a', 'copy', '-movflags', '+faststart', str(tmp)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=log)
    started, count = time.monotonic(), 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            stamp = count / fps
            box = active_box(rows, stamp)
            if box:
                frame = restore.apply(frame, box)
            else:
                restore.previous = None
            if any(abs(stamp - t) < .5 / fps for t in (16.8, 6., 100., 190., 271.)):
                cv2.imwrite(str(args.output / f'clean-{stamp:.2f}.jpg'), frame)
            proc.stdin.write(frame.tobytes())
            count += 1
            if count % 100 == 0:
                elapsed = time.monotonic() - started
                print(f'Render {count}/{total}, {elapsed:.0f}s, ETA {elapsed / count * (total-count):.0f}s', flush=True)
    finally:
        cap.release()
        proc.stdin.close()
        code = proc.wait()
        log.close()
    if code or count != total:
        raise RuntimeError(f'Incomplete render: encoder {code}, frames {count}/{total}')
    tmp.replace(args.output / 'video-vi.mp4')
    report = dict(frames=count, fps=fps, elapsed=time.monotonic()-started, source_sha256=source_hash,
                  unmapped=[r for r in rows if not r['box']],
                  method=f'LaMa anchors every {args.anchor_stride} frames, short backward-flow propagation; full OCR rectangle plus 16px shadow padding',
                  status='review-only, temporal quality not certified')
    (args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
