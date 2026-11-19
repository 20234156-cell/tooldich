"""Controlled real auto/stepwise comparison from one existing DB checkpoint.

Creates two NEW projects. Never approves the semantic quality of the checkpoint.
Network extraction/translation speed is deliberately outside this experiment.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint-project', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    from src.database.connection import SessionLocal
    from src.database.models import Media, Source
    from src.jobs.system import manager
    from src.pipeline.orchestrator import orchestrator as runner
    from src.pipeline.state_machine import PipelineState, ProjectState
    from src.pipeline.render import RenderConfig
    from src.tts.cache import TTSCache

    source = runner.get_source_video_path(args.checkpoint_project)
    with SessionLocal() as db:
        source_record = db.query(Source).filter_by(project_id=args.checkpoint_project).one()
        source_url = source_record.input_url
    checkpoint = runner.get_cues(args.checkpoint_project)
    if not checkpoint or any(not c.vi_text for c in checkpoint):
        raise ValueError('Comparison requires a nonempty translated database checkpoint')
    original_cache_init = TTSCache.__init__
    active_cache = [None]
    def cache_init(self, cache_dir=None):
        original_cache_init(self, cache_dir or active_cache[0])
    TTSCache.__init__ = cache_init
    config = RenderConfig(style='cover', font_name='Montserrat', font_color='&H0000FFFF',
                          outline_width=3, font_size=48, audio_mode='mix',
                          voice_volume=1.7, bg_volume=.15)
    voice = dict(voice='piper:ngochuyen', rate='+15%', auto_fit=True,
                 shorten_to_fit=True, translation_model='gemini-3.5-flash-lite')
    report = dict(status='running', checkpoint_project=args.checkpoint_project,
                  source_sha256=digest(source), runs={},
                  limitations=['Shared model runtime; separate empty audio caches.',
                    'Common existing translated DB input; not a fresh translation benchmark.',
                    'OCR/text/translation/listening review remains pending; equality is not semantic correctness.',
                    'Python orchestrator paths; browser UI is not exercised.'])
    def publish():
        (args.output/'comparison.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    def measured(row, name, fn):
        start = time.perf_counter()
        print(json.dumps(dict(stage=name, status='started')), flush=True)
        try:
            return fn()
        finally:
            row['seconds'][name] = time.perf_counter()-start
            publish()
    publish()
    for mode in ('auto', 'manual'):
        row = report['runs'][mode] = dict(status='running', seconds={})
        started = time.perf_counter()
        try:
            project = manager.create_project(source_url)
            pid = row['project_id'] = project['project_id']
            root = runner.get_project_dir(pid)
            target = root/'source'/source.name
            shutil.copyfile(source, target)
            if digest(target) != report['source_sha256']:
                raise ValueError('Source copy identity mismatch')
            subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(target), '-f', 'null', '-'], check=True, capture_output=True)
            with SessionLocal() as db:
                old = db.query(Media).filter_by(project_id=args.checkpoint_project).one()
                values = {col.name: getattr(old, col.name) for col in Media.__table__.columns
                          if col.name not in ('id', 'project_id', 'file_path')}
                db.add(Media(project_id=pid, file_path=str(target.resolve()), **values))
                db.commit()
            shutil.copyfile(runner.get_project_dir(args.checkpoint_project)/'source/roi.json', root/'source/roi.json')
            runner.save_cues(pid, [c.model_copy(update={'voice_audio_path': None, 'voice_duration': None, 'tts_rate': None}) for c in checkpoint])
            state = PipelineState(current_state=ProjectState.OCR_READY)
            for stage in ('SOURCE_READY', 'MEDIA_READY', 'ROI_READY'):
                state.set_stage_status(stage, 'COMPLETED', progress=100)
            state.set_stage_status('OCR_READY', 'COMPLETED', progress=100,
                warnings=['Controlled checkpoint comparison; source/translation and listening not independently approved.'],
                degraded=['comparison_content_review_pending'])
            runner.save_state(pid, state)
            active_cache[0] = args.output/(mode+'_fresh_tts_cache')
            if mode == 'auto':
                result = measured(row, 'run_all', lambda: runner.run_all(pid, **voice,
                    style=config.style, audio_mode=config.audio_mode, font_family=config.font_name,
                    font_color=config.font_color, outline_color=config.outline_color,
                    outline_width=config.outline_width, font_size=config.font_size,
                    voice_volume=config.voice_volume, bg_volume=config.bg_volume))
                row['qa'] = result['verification']
            else:
                measured(row, 'source_review', lambda: runner.approve_source_cues(pid))
                measured(row, 'translation_review', lambda: runner.approve_translation(pid))
                measured(row, 'save_style', lambda: runner.save_subtitle_settings(pid, config.model_dump()))
                measured(row, 'voiceover', lambda: runner.run_voiceover(pid, **voice))
                measured(row, 'render', lambda: runner.run_render(pid, cfg=config))
                row['qa'] = measured(row, 'verify', lambda: runner.verify(pid)).to_dict()
            output = root/'output/video_vi.mp4'
            measured(row, 'full_decode', lambda: subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(output), '-f', 'null', '-'], check=True, capture_output=True))
            row['output'] = str(output.resolve())
            row['output_sha256'] = digest(output)
            row['stream_hashes'] = {}
            for stream in ('v', 'a'):
                cmd = ['ffmpeg', '-v', 'error', '-i', str(output), '-map', '0:'+stream+':0', '-f', 'hash', '-hash', 'sha256', '-']
                row['stream_hashes'][stream] = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
            row['artifacts'] = {name: digest(root/name) for name in ('voiceover/voiceover.wav', 'voiceover/timeline.json', 'output/video_vi.ass', 'output/render_config.json')}
            row['status'] = 'completed'
        except Exception as exc:
            row.update(status='failed', error=type(exc).__name__+': '+str(exc))
        finally:
            row['wall_seconds_including_setup_validation'] = time.perf_counter()-started
            publish()
    runs = report['runs']
    if all(r['status']=='completed' for r in runs.values()):
        report['equal_decoded_streams'] = runs['auto']['stream_hashes'] == runs['manual']['stream_hashes']
        report['equal_artifacts'] = {k:runs['auto']['artifacts'][k] == runs['manual']['artifacts'][k] for k in runs['auto']['artifacts']}
        report['status'] = 'completed_content_review_pending'
    else:
        report['status'] = 'failed'
    publish()
    print(json.dumps({'status':report['status'], 'projects':{k:v.get('project_id') for k,v in runs.items()}}), flush=True)
    return 0 if report['status'].startswith('completed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
