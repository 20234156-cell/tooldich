"""Inspect a VERIFIED encoded delivery with real decode/frame/speech samples.

Run outside pytest. Speech recognition is evidence for review, not an automatic
semantic certificate. The cached Whisper model is never downloaded here.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.pipeline.orchestrator import orchestrator
from src.pipeline.render import get_ffmpeg_bin, get_ffprobe_bin


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project_id')
    parser.add_argument('--speech', action='store_true')
    args = parser.parse_args()
    root = orchestrator.get_project_dir(args.project_id)
    delivery = root/'output/video_vi.mp4'
    qa = json.loads((root/'output/verify_report.json').read_text(encoding='utf-8'))
    if not qa.get('passed') or orchestrator.get_state(args.project_id).current_state.value != 'VERIFIED':
        raise RuntimeError('Finish real pipeline verification before delivery sampling')
    directory = root/'audit/encoded_full_delivery'
    directory.mkdir(parents=True, exist_ok=True)
    report = {'project_id': args.project_id, 'pipeline_verification': qa,
              'scope': 'Real encoded full-film identity and samples; speech semantics require review',
              'samples': []}
    target = directory/'report.json'
    def save():
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    digest = hashlib.sha256()
    with delivery.open('rb') as stream:
        for block in iter(lambda: stream.read(8*1024*1024), b''):
            digest.update(block)
    report['encoded_identity'] = {'path': str(delivery), 'bytes': delivery.stat().st_size,
                                  'sha256': digest.hexdigest()}
    probe = subprocess.run([str(get_ffprobe_bin()), '-v', 'error', '-show_streams',
                            '-show_format', '-of', 'json', str(delivery)],
                           capture_output=True, text=True, check=True, timeout=30)
    report['ffprobe'] = json.loads(probe.stdout)
    rows = json.loads((root/'voiceover/timeline.json').read_text(encoding='utf-8'))
    cues = {c.index:c for c in orchestrator.get_cues(args.project_id)}
    if {row['cue_index'] for row in rows} != set(cues):
        raise RuntimeError('Encoded sampling timeline no longer matches DB IDs')
    model = None
    if args.speech:
        from faster_whisper import WhisperModel
        model = WhisperModel('small', device='cpu', compute_type='int8',
                             cpu_threads=4, local_files_only=True)
    ffmpeg = str(get_ffmpeg_bin())
    for label, position in [('start', 8), ('middle', len(rows)//2), ('end', len(rows)-12)]:
        group = rows[position:position+3]
        start = max(0, group[0]['actual_start'])
        duration = max(.2, group[-1]['actual_end']-start)
        # Decode actual video AND audio; errors must fail the command.
        subprocess.run([ffmpeg, '-v', 'error', '-xerror', '-ss', str(start), '-i', str(delivery),
                        '-t', str(duration), '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'],
                       capture_output=True, check=True, timeout=120)
        frame = directory/f'{label}.png'
        subprocess.run([ffmpeg, '-v', 'error', '-y', '-ss', str(start+.2), '-i', str(delivery),
                        '-frames:v', '1', str(frame)], capture_output=True, check=True, timeout=60)
        audio = directory/f'{label}.wav'
        subprocess.run([ffmpeg, '-v', 'error', '-y', '-ss', str(start), '-i', str(delivery),
                        '-t', str(duration), '-vn', '-ar', '16000', '-ac', '1', str(audio)],
                       capture_output=True, check=True, timeout=60)
        sample = {'label': label, 'start': start, 'duration': duration, 'decode_passed': True,
                  'frame': frame.name, 'audio': audio.name,
                  'expected': [{'id':r['cue_index'], 'text':cues[r['cue_index']].vi_text} for r in group]}
        if model:
            began = time.perf_counter()
            segments, info = model.transcribe(str(audio), language='vi', beam_size=3)
            sample['recognized'] = [{'start':s.start, 'end':s.end, 'text':s.text} for s in segments]
            sample['asr_seconds'] = round(time.perf_counter()-began, 2)
        report['samples'].append(sample)
        save()
        print(json.dumps({'sample': label, 'decode_passed': True}), flush=True)
    duration = float(report['ffprobe']['format']['duration'])
    subprocess.run([ffmpeg, '-v', 'error', '-xerror', '-ss', str(max(0,duration-3)),
                    '-i', str(delivery), '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'],
                   capture_output=True, check=True, timeout=120)
    report['tail_decode_passed'] = True
    report['sample_checks_passed'] = True
    save()
    print(json.dumps({'report': str(target), 'sample_checks_passed': True,
                      'fully_passed': qa.get('fully_passed')}), flush=True)


if __name__ == '__main__':
    main()
