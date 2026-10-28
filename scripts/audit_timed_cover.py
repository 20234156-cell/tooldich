"""Real local FFmpeg audit; synthetic media, no providers or project writes."""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np

from src.ocr.base import Cue
from src.pipeline.render import RenderConfig, build_filter_graphs, cues_to_ass, get_ffmpeg_bin, render_preview_frame
from src.pipeline.roi import ROIBox


def main():
    style = sys.argv[1] if len(sys.argv) > 1 else 'cover'
    if style not in {'cover', 'clean'}:
        raise SystemExit('Usage: python scripts/audit_timed_cover.py [cover|clean]')
    directory = Path('storage/audits') / ('timed_' + style + '_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    directory.mkdir(parents=True)
    ffmpeg = str(get_ffmpeg_bin())
    source = directory / 'source.mp4'
    pattern = ('color=c=blue:s=320x180:r=10:d=6,drawbox=x=90:y=135:w=140:h=12:color=white:t=fill'
               if style == 'clean' else 'color=c=white:s=320x180:r=10:d=6')
    subprocess.run([ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i',
                    pattern, '-c:v', 'libx264', str(source)], check=True, timeout=30)
    roi = ROIBox(x=.1, y=.7, width=.8, height=.2)
    cues = [Cue(index=1, start=1, end=2, text='source'), Cue(index=2, start=4, end=5, text='source')]
    cfg = RenderConfig(style=style, use_nvenc=False)
    ass = directory / 'mask.ass'
    ass.write_text(cues_to_ass(cues, 320, 180, cfg, 126, roi), encoding='utf-8')
    graph, _ = build_filter_graphs(320, 180, roi, ass, cfg, False, False, cues)
    graph_path = directory / 'filter.txt'
    graph_path.write_text(graph, encoding='utf-8')
    encoded = directory / 'encoded.mkv'
    subprocess.run([ffmpeg, '-v', 'error', '-y', '-i', str(source), '-filter_complex_script', str(graph_path),
                    '-map', '[v_out]', '-c:v', 'ffv1', str(encoded)], check=True, timeout=30)
    rows = []
    for moment, masked in [(.5, False), (1.5, True), (2.0, False), (2.5, False), (4.5, True), (5.5, False)]:
        jpeg = render_preview_frame(source, roi, cues, moment, cfg)
        (directory / f'preview_{moment}.jpg').write_bytes(jpeg)
        preview = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        raw = subprocess.run([ffmpeg, '-v', 'error', '-ss', str(moment), '-i', str(encoded), '-frames:v', '1',
                              '-pix_fmt', 'bgr24', '-f', 'rawvideo', 'pipe:1'], capture_output=True, check=True, timeout=30).stdout
        frame = np.frombuffer(raw, np.uint8).reshape(180, 320, 3)
        actual = float(frame[135:145, 100:220].mean())
        shown = float(preview[135:145, 100:220].mean())
        passed = (actual < 3 and shown < 3) if masked else (actual > 250 and shown > 250)
        if masked and style == 'clean':
            expected = np.array([255, 0, 0])
            passed = (np.max(np.abs(frame[135:145, 100:220].mean(axis=(0, 1)) - expected)) < 6
                      and np.max(np.abs(preview[135:145, 100:220].mean(axis=(0, 1)) - expected)) < 6)
        rows.append(dict(time=moment, masked=masked, encoded_mean=actual, preview_mean=shown, passed=bool(passed)))
    # Parse a real long-film expression without running a long encode.
    many = [Cue(index=i+1, start=i*2, end=i*2+1, text='source') for i in range(4000)]
    long_graph, _ = build_filter_graphs(320, 180, roi, ass, cfg, False, False, many)
    graph_path.write_text(long_graph, encoding='utf-8')
    subprocess.run([ffmpeg, '-v', 'error', '-i', str(source), '-filter_complex_script', str(graph_path),
                    '-map', '[v_out]', '-frames:v', '1', '-f', 'null', '-'], check=True, timeout=30)
    report = dict(passed=all(row['passed'] for row in rows), style=style, samples=rows, long_expression_cues=4000,
                  scope='Synthetic timed cover and preview/encoded agreement; no film semantic or audio certification')
    (directory / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(directory / 'report.json')
    if not report['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
