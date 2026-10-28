"""Independent encoded-speech evidence from a cached local Whisper model only."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['HF_HUB_OFFLINE'] = '1'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('audit_root', type=Path)
    args = parser.parse_args()
    root = args.audit_root.resolve()
    if not root.is_relative_to(Path('storage/audits').resolve()):
        raise ValueError('Audit root must be under storage/audits')
    from faster_whisper import WhisperModel
    from src.pipeline.render import get_ffmpeg_bin
    output = root/'encoded_speech'
    output.mkdir(exist_ok=True)
    report = {'scope':'Actual encoded AAC excerpts transcribed with cached Whisper small. ASR is review evidence, not a semantic approval or replacement QA threshold.', 'samples':[]}
    try:
        model = WhisperModel('small', device='cpu', compute_type='int8', cpu_threads=2, local_files_only=True)
    except Exception as exc:
        report.update(passed=False, error_type=type(exc).__name__, error=str(exc))
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=True))
        return 1
    with sqlite3.connect(f'file:{(root/"audit.db").as_posix()}?mode=ro', uri=True) as db:
        for case in ('normal', 'dense'):
            project = 'audit_'+case
            cues = json.loads(db.execute('SELECT segments FROM transcripts WHERE project_id=? ORDER BY id LIMIT 1',(project,)).fetchone()[0])
            by_id = {c['index']: c for c in cues}
            directory = root/'projects'/project
            timeline = json.loads((directory/'voiceover/timeline.json').read_text(encoding='utf-8'))
            if {row['cue_index'] for row in timeline} != set(by_id):
                raise ValueError('Encoded timeline does not match DB IDs')
            for row in timeline:
                position = max(0, row['actual_start']-.1)
                duration = row['actual_end']-position+.15
                path = output/f'{case}_{row["cue_index"]}.wav'
                subprocess.run([str(get_ffmpeg_bin()),'-v','error','-y','-ss',str(position),'-i',str(directory/'output/video_vi.mp4'),
                    '-t',str(duration),'-vn','-ar','16000','-ac','1',str(path)],check=True,capture_output=True,timeout=30)
                segments, _ = model.transcribe(str(path), language='vi', beam_size=3)
                text = ' '.join(segment.text.strip() for segment in segments)
                report['samples'].append({'case':case,'cue_index':row['cue_index'],'expected':by_id[row['cue_index']]['vi_text'],
                                          'recognized':text,'start':position,'duration':duration,'speech_recognized':bool(text.strip())})
                (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    report['passed'] = len(report['samples'])==4 and all(s['speech_recognized'] for s in report['samples'])
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
