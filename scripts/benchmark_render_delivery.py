"""Paired real production renders, changing only legacy versus fixed AAC offset."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
from benchmark_audio_offset import elementary_video_hash


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    from src.pipeline.orchestrator import orchestrator
    from src.pipeline.render import RenderConfig,render_one_pass,get_ffmpeg_bin,get_ffprobe_bin
    from src.pipeline import audio_qc
    from src.gpu import nvenc_available
    root=orchestrator.get_project_dir(args.project)
    source=orchestrator.get_source_video_path(args.project)
    voice=root/'voiceover/voiceover.wav'
    cfg=RenderConfig.model_validate(orchestrator.get_subtitle_settings(args.project))
    source_cues=orchestrator.get_cues(args.project)
    cues=orchestrator._render_cues(args.project,cfg,cues=source_cues)
    roi=orchestrator.get_roi_set(args.project).get_primary_subtitle_roi()
    assert roi is not None
    # Warm selectors/probe before timing; no project state or artifacts are saved.
    ffmpeg=get_ffmpeg_bin();get_ffprobe_bin()
    assert nvenc_available(str(ffmpeg))
    protected=[source,voice,root/'pipeline_state.json',root/'source/subtitle_settings.json',root/'output/video_vi.mp4']
    def fingerprints():
        return {str(p):hashlib.file_digest(p.open('rb'),'sha256').hexdigest() for p in protected}
    before=fingerprints()
    original_measure=audio_qc.measure_audio
    report={'project':args.project,'order':['candidate','baseline'],'runs':[],
            'scope':'Two render_one_pass calls on existing immutable inputs, fresh output folders; backend/projectstate untouched.'}
    for mode in report['order']:
        folder=args.output/mode;folder.mkdir()
        measurements=[]
        def measure(ffmpeg,path):
            started=time.perf_counter();result=original_measure(ffmpeg,path)
            measurements.append({'seconds':time.perf_counter()-started,'measured':dict(result)})
            if mode=='baseline':result.pop('target_offset',None)
            return result
        audio_qc.measure_audio=measure
        started=time.perf_counter()
        result=render_one_pass(source,voice,roi,cues,folder/'video_vi.mp4',cfg,source_cues=source_cues)
        elapsed=time.perf_counter()-started
        qc=json.loads((folder/'audio_qc.json').read_text(encoding='utf-8'))
        hashed=elementary_video_hash(ffmpeg,result.output_path)
        probe=json.loads(subprocess.check_output([str(get_ffprobe_bin()),'-v','quiet','-show_entries','format=duration','-of','json',str(result.output_path)],text=True))
        subprocess.run([str(ffmpeg),'-v','error','-threads','2','-i',str(result.output_path),'-f','null','-'],check=True,capture_output=True,timeout=180)
        row={'mode':mode,'seconds':elapsed,'encoder':result.used_encoder,'qc':qc,
             'video_elementary_sha256':hashed,'duration':float(probe['format']['duration']),
             'full_decode_passed':True,'measurements':measurements}
        report['runs'].append(row)
        (args.output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps({'mode':mode,'seconds':elapsed,'corrections':qc['corrections'],'after':qc['after']}),flush=True)
    audio_qc.measure_audio=original_measure
    report['same_picture_bitstream']=len({r['video_elementary_sha256'] for r in report['runs']})==1
    report['project_files_unchanged']=before==fingerprints()
    report['source_cues_unchanged']=[c.model_dump() for c in source_cues]==[c.model_dump() for c in orchestrator.get_cues(args.project)]
    report['seconds_saved']=report['runs'][1]['seconds']-report['runs'][0]['seconds']
    report['ffmpeg_version']=subprocess.check_output([str(ffmpeg),'-version'],text=True).splitlines()[0]
    (args.output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({k:report[k] for k in ['same_picture_bitstream','project_files_unchanged','source_cues_unchanged','seconds_saved']}),flush=True)


if __name__=='__main__':main()
