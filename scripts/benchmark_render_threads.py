"""Compare render thread caps with unchanged filters/codecs; real audits only."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))


def decoded_hashes(ffmpeg,path):
    result={}
    for name,mapping,args in [('video','0:v:0',['-f','framemd5']),
                              ('audio','0:a:0',['-c:a','pcm_f32le','-f','f32le'])]:
        command=[str(ffmpeg),'-v','error','-threads','2','-i',str(path),'-map',mapping,*args,'-']
        data=subprocess.check_output(command)
        if name=='video':
            # Header text can mention stream metadata; compare frame records.
            data=b'\n'.join(line for line in data.splitlines() if not line.startswith(b'#'))
        result[name]=hashlib.sha256(data).hexdigest()
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--voice',type=Path,required=True)
    parser.add_argument('--filter',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--seconds',type=float,default=20)
    parser.add_argument('--rounds',type=int,default=2)
    parser.add_argument('--modes',nargs='+',default=['baseline','decoder2','filter1','capped2'])
    args=parser.parse_args()
    if not 1<=args.rounds<=3 or args.seconds<=0:parser.error('Use 1..3 rounds and positive duration')
    settings={'baseline':([],[]),'decoder2':(['-threads','2'],[]),
              'filter1':([],['-filter_complex_threads','1']),
              'capped2':(['-threads','2'],['-filter_complex_threads','2'])}
    if not set(args.modes)<=set(settings):parser.error('Unknown mode')
    from src.pipeline.render import get_ffmpeg_bin
    import psutil
    ffmpeg=get_ffmpeg_bin()
    args.output.mkdir(parents=True,exist_ok=False)
    report={'source':str(args.source.resolve()),'voice':str(args.voice.resolve()),
            'filter_sha256':hashlib.sha256(args.filter.read_bytes()).hexdigest(),
            'seconds':args.seconds,'runs':[],
            'scope':'Encode only, before master_delivery; fixed NVENC p4 CQ22 VBR, AAC192k/48k, unchanged filtergraph.'}
    for number in range(1,args.rounds+1):
        modes=args.modes if number%2 else list(reversed(args.modes))
        for mode in modes:
            decoder,global_args=settings[mode]
            target=args.output/f'{mode}_{number}.mp4'
            command=[str(ffmpeg),'-hide_banner','-loglevel','error','-y',*global_args,*decoder,
                '-i',str(args.source),'-i',str(args.voice),'-/filter_complex',str(args.filter),
                '-map','[v_out]','-map','[a_out]','-c:v','h264_nvenc','-preset','p4','-cq','22','-rc','vbr',
                '-t',str(args.seconds),'-c:a','aac','-ar','48000','-b:a','192k','-movflags','+faststart',str(target)]
            started=time.perf_counter();peak=[0];stop=threading.Event()
            with (args.output/f'{mode}_{number}.log').open('wb') as log:
                process=subprocess.Popen(command,stdout=log,stderr=log)
                def sample():
                    while not stop.wait(.05):
                        try:peak[0]=max(peak[0],psutil.Process(process.pid).memory_info().rss)
                        except psutil.Error:return
                thread=threading.Thread(target=sample,daemon=True);thread.start()
                try:code=process.wait(timeout=600)
                finally:
                    if process.poll() is None:process.kill();process.wait(timeout=10)
                    stop.set();thread.join(timeout=2)
            elapsed=time.perf_counter()-started
            if code:raise RuntimeError('Encode failed: '+str(args.output/f'{mode}_{number}.log'))
            hashes=decoded_hashes(ffmpeg,target)
            row={'mode':mode,'round':number,'seconds':elapsed,'peak_process_rss_bytes':peak[0],
                 'decoded_hashes':hashes,'bytes':target.stat().st_size}
            report['runs'].append(row)
            (args.output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
            print(json.dumps(row),flush=True)
    baseline=next(r for r in report['runs'] if r['mode']=='baseline')['decoded_hashes']
    report['aggregates']={mode:{'median_seconds':statistics.median(r['seconds'] for r in report['runs'] if r['mode']==mode),
        'all_decoded_hashes_match_baseline':all(r['decoded_hashes']==baseline for r in report['runs'] if r['mode']==mode)} for mode in args.modes}
    (args.output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')


if __name__=='__main__':main()
