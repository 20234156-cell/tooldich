"""Real video stage profile in a new project, with an isolated empty TTS cache."""
import argparse
import functools
import json
import os
from pathlib import Path
import sys
import time

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))


def source_present(orchestrator,project_id):
    from src.jobs.logger import PipelineException,SystemErrorCodes
    try:
        orchestrator.get_source_video_path(project_id)
        return True
    except PipelineException as exc:
        if exc.code!=SystemErrorCodes.MEDIA_NOT_FOUND: raise
        return False


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--model',default='gemini-3.1-flash-lite')
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--source-only',action='store_true')
    parser.add_argument('--subtitle-style',choices=['cover','clean','bilingual'])
    parser.add_argument('--margin-v',type=int)
    parser.add_argument('--extraction-mode',choices=['auto','hardsub','stt'],default='auto')
    parser.add_argument('--reextract',action='store_true',help='Explicitly replace transcript by re-extraction in this audit project')
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=args.resume)
    os.environ['VIENEU_BATCH_SIZE']='1'
    from config.settings import settings
    from src.tts.cache import TTSCache
    original_init=TTSCache.__init__
    def cache_init(self,cache_dir=None):
        original_init(self,cache_dir if cache_dir is not None else args.output/'fresh_tts_cache')
    TTSCache.__init__=cache_init
    from src.jobs.system import manager
    from src.pipeline.orchestrator import orchestrator
    from pipeline_profile_spans import install
    recorder=install(args.output/'spans.jsonl')
    report={'url':args.url,'batch_size':1,'fresh_audio_cache':True,'stage_seconds':{},
            'limitations':['Existing shared translation/model caches are retained; timings are on a shared machine.'],
            'status':'running','started_at':time.time()}
    if args.resume:
        report=json.loads((args.output/'profile.json').read_text(encoding='utf-8'))
        if report['url']!=args.url or report['batch_size']!=1: raise ValueError('Resume identity mismatch')
        report.setdefault('previous_attempts',[]).append({k:report.get(k) for k in ['status','error','total_seconds']})
        report['status']='running'
        report.pop('error',None)
    def publish():
        (args.output/'profile.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    def wrap(name):
        original=getattr(orchestrator,name)
        @functools.wraps(original)
        def run(*a,**kw):
            if name=='run_render' and (args.subtitle_style or args.margin_v is not None):
                values=kw['cfg'].model_dump()
                if args.subtitle_style: values['style']=args.subtitle_style
                if args.margin_v is not None: values['margin_v']=args.margin_v
                kw['cfg']=type(kw['cfg']).model_validate(values)
                orchestrator.save_subtitle_settings(report['project_id'],kw['cfg'].model_dump())
            begin=time.perf_counter()
            print(json.dumps({'stage':name,'status':'started'}),flush=True)
            try: return original(*a,**kw)
            finally:
                report['stage_seconds'][name]=report['stage_seconds'].get(name,0)+time.perf_counter()-begin
                publish()
                print(json.dumps({'stage':name,'seconds':report['stage_seconds'][name]}),flush=True)
        setattr(orchestrator,name,run)
    for name in ['run_ocr','run_translation','run_voiceover','run_render','verify']: wrap(name)
    started=time.perf_counter()
    try:
        project={'project_id':report['project_id']} if args.resume else manager.create_project(args.url)
        report['project_id']=project['project_id']
        publish()
        begin=time.perf_counter()
        source_ready=False
        if args.resume:
            source_ready=source_present(orchestrator,project['project_id'])
        if not source_ready:
            manager.download_and_validate_project(project['project_id'],quiet=True)
            report['stage_seconds']['source']=time.perf_counter()-begin
        report['requested_bilibili_quality']=settings.BILIBILI_PREFERRED_QUALITY
        publish()
        if args.source_only:
            report['status']='source_ready'
            return 0
        report['extraction_mode']=args.extraction_mode
        if args.reextract:
            orchestrator.run_ocr(project['project_id'],extraction_mode=args.extraction_mode)
        report['result']=orchestrator.run_all(project['project_id'],voice='vieneu:ngoc_huyen_review_phim',
            extraction_mode=args.extraction_mode,translation_model=args.model)
        report['status']='completed'
    except Exception as exc:
        report['status']='failed'
        report['error']=type(exc).__name__+': '+str(exc)
    finally:
        report['total_seconds']=(report.get('total_seconds',0) if args.resume else 0)+time.perf_counter()-started
        publish()
    print(json.dumps({'status':report['status'],'project_id':report.get('project_id'),'total_seconds':report['total_seconds']}),flush=True)
    return 0 if report['status']=='completed' else 1


if __name__=='__main__': raise SystemExit(main())
