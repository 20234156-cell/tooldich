"""Real AAC mastering: legacy zero-offset analysis versus patched production."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))


def elementary_video_hash(ffmpeg,path):
    process=subprocess.Popen([str(ffmpeg),'-v','error','-i',str(path),'-map','0:v:0','-c:v','copy','-f','h264','-'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    digest=hashlib.sha256()
    while data:=process.stdout.read(1024*1024):digest.update(data)
    errors=process.stderr.read();code=process.wait(timeout=30)
    if code:raise RuntimeError(errors.decode(errors='replace'))
    return digest.hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--duration',type=float,required=True)
    parser.add_argument('--candidate-first',action='store_true')
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    from src.pipeline.render import get_ffmpeg_bin,get_ffprobe_bin
    from src.pipeline import audio_qc
    ffmpeg=get_ffmpeg_bin()
    captured=[]
    def measure(path,label,keep_offset=False):
        started=time.perf_counter()
        result=subprocess.run([str(ffmpeg),'-hide_banner','-nostats','-i',str(path),'-vn',
            '-af','loudnorm=I=-14:TP=-2:LRA=11:print_format=json','-f','null','-'],capture_output=True,text=True,check=True,timeout=600)
        raw=json.loads(result.stderr[result.stderr.rfind('{'):result.stderr.rfind('}')+1])
        values={key:float(raw[name]) for key,name in [('integrated_lufs','input_i'),('true_peak_dbtp','input_tp'),('lra_lu','input_lra'),('threshold','input_thresh')]}
        if keep_offset and 'target_offset' in raw:values['target_offset']=float(raw['target_offset'])
        captured.append({'label':label,'seconds':time.perf_counter()-started,'raw':raw,'measured':values})
        print(json.dumps(captured[-1]),flush=True)
        return values
    baseline=args.output/'baseline_master.mp4'
    shutil.copyfile(args.input,baseline)
    candidate=args.output/'offset_master.mp4'
    shutil.copyfile(args.input,candidate)
    def master(path,label,keep_offset):
        audio_qc.measure_audio=lambda ffmpeg,path:measure(path,label,keep_offset=keep_offset)
        started=time.perf_counter()
        result=audio_qc.master_delivery(ffmpeg,path,duration_seconds=args.duration)
        return result,time.perf_counter()-started
    if args.candidate_first:
        candidate_report,candidate_seconds=master(candidate,'patched_production',True)
        baseline_report,baseline_seconds=master(baseline,'baseline_legacy',False)
    else:
        baseline_report,baseline_seconds=master(baseline,'baseline_legacy',False)
        candidate_report,candidate_seconds=master(candidate,'patched_production',True)
    candidate_measured=candidate_report['after']
    hashes={name:elementary_video_hash(ffmpeg,path) for name,path in [('original',args.input),('baseline',baseline),('candidate',candidate)]}
    durations={}
    for name,path in [('original',args.input),('baseline',baseline),('candidate',candidate)]:
        probe=json.loads(subprocess.check_output([str(get_ffprobe_bin()),'-v','quiet','-show_entries','format=duration','-of','json',str(path)],text=True))
        durations[name]=float(probe['format']['duration'])
    report={'input':str(args.input.resolve()),'baseline_seconds':baseline_seconds,'baseline_report':baseline_report,
            'baseline_scope':'Production feedback loop with legacy four-field measurements (offset deliberately omitted).',
            'candidate_seconds':candidate_seconds,'candidate_report':candidate_report,
            'candidate_scope':'Actual patched master_delivery, including its own first analysis and encoded AAC remeasurement.',
            'candidate_measured':candidate_measured,'candidate_passed':audio_qc.delivery_passed(candidate_measured),
            'video_elementary_hashes':hashes,'picture_bitstream_unchanged':len(set(hashes.values()))==1,
            'durations':durations,'measurements':captured,
            'limitations':['Candidate reuses offset only with exactly the analysis target settings (-14LUFS/-2dBTP/LRA11).',
                           'Candidate AAC differs intentionally; no listening review implied by loudness/peak acceptance.']}
    report['run_order']=['candidate','baseline'] if args.candidate_first else ['baseline','candidate']
    with ffmpeg.open('rb') as stream:report['ffmpeg_sha256']=hashlib.file_digest(stream,'sha256').hexdigest()
    report['ffmpeg_version']=subprocess.check_output([str(ffmpeg),'-version'],text=True).splitlines()[0]
    (args.output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({'baseline_seconds':baseline_seconds,'baseline_corrections':baseline_report['corrections'],
                      'candidate_seconds':candidate_seconds,'candidate_passed':report['candidate_passed'],
                      'picture_bitstream_unchanged':report['picture_bitstream_unchanged']}),flush=True)


if __name__=='__main__':main()
