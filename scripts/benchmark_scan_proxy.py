"""Real, bounded proxy thread benchmark with decoded-frame parity (outside pytest)."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--start', type=float, default=60)
    parser.add_argument('--seconds', type=float, default=20)
    parser.add_argument('--rounds', type=int, default=2)
    args = parser.parse_args()
    if not 0 < args.seconds <= 120 or not 1 <= args.rounds <= 3 or args.start < 0:
        parser.error('Use positive duration <=120, 1..3 rounds, and nonnegative start')
    args.output.mkdir(parents=True, exist_ok=False)
    from src.pipeline.render import get_ffmpeg_bin
    ffmpeg = str(get_ffmpeg_bin())
    modes = {'baseline': [], 'filter1': ['-filter_threads','1'],
             'decoder2_filter1': ['-filter_threads','1','-threads','2']}
    report = dict(target='src/ocr/vsf_adapter.py:_create_scan_proxy',
        acceptance='All decoded frames/timestamps identical; repeated median encode time lower; no OCR/QA threshold change',
        source=str(args.source.resolve()), start=args.start, duration=args.seconds,
        ffmpeg=ffmpeg, runs=[], limitations=['Other machine activity may affect timings; this is proxy-only, not end-to-end.'])
    with args.source.open('rb') as stream:
        report['source_sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
    for number in range(args.rounds):
        names = list(modes) if number % 2 == 0 else list(reversed(modes))
        for mode in names:
            output = args.output / f'{mode}_{number}.mp4'
            command = [ffmpeg,'-nostdin','-v','error','-y',*modes[mode],'-ss',str(args.start),
                '-i',str(args.source),'-t',str(args.seconds),'-an','-vf','scale=960:-2',
                '-c:v','h264_nvenc','-preset','p1','-cq','26','-rc','vbr',str(output)]
            started = time.perf_counter()
            result = subprocess.run(command,capture_output=True,timeout=300)
            elapsed = time.perf_counter()-started
            if result.returncode:
                raise RuntimeError(result.stderr.decode(errors='replace')[-1000:])
            decoded = subprocess.run([ffmpeg,'-nostdin','-v','error','-threads','2','-i',str(output),
                '-map','0:v:0','-f','framemd5','-'],capture_output=True,check=True,timeout=120).stdout
            records = b'\n'.join(row for row in decoded.splitlines() if not row.startswith(b'#'))
            row = dict(mode=mode,round=number,seconds=elapsed,bytes=output.stat().st_size,
                       frames=len(records.splitlines()),decoded_sha256=hashlib.sha256(records).hexdigest())
            report['runs'].append(row)
            (args.output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
            print(json.dumps(row),flush=True)
    reference = report['runs'][0]['decoded_sha256']
    report['aggregates'] = {mode:dict(median_seconds=statistics.median(
        row['seconds'] for row in report['runs'] if row['mode']==mode),
        decoded_parity=all(row['decoded_sha256']==reference for row in report['runs'] if row['mode']==mode))
        for mode in modes}
    (args.output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report['aggregates']),flush=True)


if __name__ == '__main__':main()
