"""Isolated GPU batch experiment; never changes the production worker/defaults."""
import argparse
import contextlib
import gc
import html
import json
import os
import re
from pathlib import Path
import statistics
import subprocess
import sys
import time

import benchmark_vieneu as baseline


def recover_batch(indices, infer, oom_type, cleanup, events):
    """Leave failed tensor frames before cleanup/retry; never return partial success."""
    failed = False
    try:
        result = infer(indices)
        if len(result) != len(indices):
            raise ValueError('Batch result count differs from chunk IDs')
        return result
    except oom_type:
        failed = True
    if failed:
        events.append({'size':len(indices), 'ids':list(indices)})
        cleanup()
        if len(indices) == 1:
            raise RuntimeError('OOM at batch 1; completed files retained')
        middle = len(indices)//2
        return (recover_batch(indices[:middle], infer, oom_type, cleanup, events)
                + recover_batch(indices[middle:], infer, oom_type, cleanup, events))


def duration_comparison(seconds, reference, threshold=.10):
    if reference <= 0:
        raise ValueError('Nonpositive reference duration')
    delta = seconds/reference-1
    return {'reference_seconds':reference, 'relative_duration_change':delta,
            'duration_review_required':abs(delta)>threshold}


def word_error_rate(expected, actual):
    a=re.findall(r'\w+',expected.lower())
    b=re.findall(r'\w+',actual.lower())
    previous=list(range(len(b)+1))
    for i,x in enumerate(a,1):
        current=[i]
        for j,y in enumerate(b,1):
            current.append(min(current[-1]+1,previous[j]+1,previous[j-1]+(x!=y)))
        previous=current
    return previous[-1]/max(1,len(a))


def create_review(output, reference_path, use_asr=False):
    summary=json.loads((output/'summary.json').read_text(encoding='utf-8'))
    reference=json.loads(reference_path.read_text(encoding='utf-8'))
    rows=[r for r in summary['rounds'] if r['technical_passed']]
    if not rows:
        raise ValueError('No complete candidate for review')
    sizes=sorted({r['batch_size'] for r in rows})
    summary['aggregates']=[]
    for batch in sizes:
        group=[r for r in rows if r['batch_size']==batch]
        median=statistics.median(r['warm_corpus_seconds'] for r in group)
        summary['aggregates'].append({'batch_size':batch,'completed_rounds':len(group),
            'median_seconds':median,'worst_seconds':max(r['warm_corpus_seconds'] for r in group),
            'time_reduction_vs_baseline':1-median/reference['median_warm_seconds'],
            'oom_count':sum(len(r['oom_events']) for r in group),
            'duration_review_count':sum(i['duration_review_required'] for r in group for i in r['items']),
            'peak_torch_reserved_bytes':max(r['torch_peak_reserved_bytes'] for r in group),
            'peak_torch_allocated_bytes':max(r['torch_peak_allocated_bytes'] for r in group),
            'peak_tree_rss_bytes':max(r['peak_tree_rss_bytes'] for r in group),
            'peak_whole_gpu_used_mib':max(r['peak_whole_gpu_used_mib'] or 0 for r in group)})
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    size=min(sizes,key=lambda s:statistics.median(r['warm_corpus_seconds'] for r in rows if r['batch_size']==s))
    candidate=next(r for r in rows if r['batch_size']==size)
    folder=output/f"batch_{size}_round_{candidate['round']:02d}"
    corpus,_=baseline.load_corpus(baseline.BASE/'tests/fixtures/vieneu_benchmark.json')
    selected=['n01','n03','p01','p03','q02','t03','l01','l03','l05','m01']
    flagged=[r['id'] for r in candidate['items'] if r['duration_review_required']]
    selected=list(dict.fromkeys(flagged+selected))[:10]
    ref_folder=reference_path.parent/'round_01'
    review={'candidate_batch_size':size,'candidate_round':candidate['round'],
            'listening_status':'pending_human_review','asr_status':'not_requested','items':[],
            'limitations':['ASR is fallible; spelling/names and digit-versus-word formatting may inflate word error rate.',
                           'Duration checks and ASR do not establish voice similarity or replace listening.']}
    cards=[]
    for cue_id in selected:
        case=next(c for c in corpus['cases'] if c['id']==cue_id)
        ref=ref_folder/(cue_id+'.wav')
        audio=folder/(cue_id+'.wav')
        review['items'].append({'id':cue_id,'text':case['text'],'baseline':str(ref.resolve()),'candidate':str(audio.resolve())})
        cards.append('<section><h2>'+html.escape(cue_id)+'</h2><p>'+html.escape(case['text'])+'</p><p>Batch 1</p><audio controls src="'+html.escape(os.path.relpath(ref,output).replace('\\','/'),quote=True)+'"></audio><p>Batch '+str(size)+'</p><audio controls src="'+html.escape(os.path.relpath(audio,output).replace('\\','/'),quote=True)+'"></audio></section>')
    (output/'listening_review.html').write_text('<!doctype html><meta charset="utf-8"><title>Duyệt giọng Ngọc Huyền</title><style>body{max-width:850px;margin:30px auto;font:18px sans-serif}section{border-top:1px solid #bbb;padding:16px}audio{width:100%}</style><h1>Duyệt giọng Ngọc Huyền</h1><p>Chưa nghe duyệt. Kiểm tra đủ chữ, tên riêng, số, cuối câu và khoảng nghỉ ở cả hai bản.</p>'+''.join(cards),encoding='utf-8')
    if use_asr:
        try:
            from faster_whisper import WhisperModel
            model=WhisperModel('small',device='cpu',compute_type='int8',cpu_threads=2,local_files_only=True)
            review['asr_status']='running'
            review['asr_model']='faster-whisper small CPU int8, cached local files only'
            started=time.perf_counter()
            (output/'review.json').write_text(json.dumps(review,ensure_ascii=False,indent=2),encoding='utf-8')
            for item in review['items']:
                if item['id'] not in ['n01','n03','p01','p03','t03','l01']: continue
                for label in ['baseline','candidate']:
                    segments,_=model.transcribe(item[label],language='vi',beam_size=5,condition_on_previous_text=False,vad_filter=False)
                    text=' '.join(s.text.strip() for s in segments)
                    item[label+'_asr']={'text':text,'word_error_rate':word_error_rate(item['text'],text)}
                (output/'review.json').write_text(json.dumps(review,ensure_ascii=False,indent=2),encoding='utf-8')
            review['asr_seconds']=time.perf_counter()-started
            review['asr_status']='completed_diagnostic_only'
        except Exception as exc:
            review['asr_status']='unavailable_or_failed'
            review['asr_error']=type(exc).__name__+': '+str(exc)
    (output/'review.json').write_text(json.dumps(review,ensure_ascii=False,indent=2),encoding='utf-8')
    return review


def worker(output, size, corpus_path):
    import numpy as np
    import soundfile as sf
    import torch
    from src.tts import vieneu_worker as production
    # Use the same ORT memory options and registered engine factory as production.
    # Enter production.main() to create it, intercept only its stdin protocol.
    original_create = production.create_engine
    def create(factory, reference):
        torch.manual_seed(1234)
        np.random.seed(1234)
        tts = original_create(factory, reference)
        corpus, digest = baseline.load_corpus(corpus_path)
        with np.load(Path(reference).with_name('encoded.npz'), allow_pickle=False) as data:
            voice = {'speaker_emb':data['speaker_emb'], 'codes':data['codes']}
        from vieneu_utils.phonemize_text import normalize_to_chunks_v3_with_gaps
        torch.cuda.synchronize()
        cold = time.perf_counter()
        tts.infer('Xin chào, đây là bài đo giọng đọc.', voice=voice)
        torch.cuda.synchronize()
        report = {'batch_size':size,'corpus_sha256':digest,'items':[], 'oom_events':[],
                  'normalization':[], 'errors':[], 'dtype':str(tts.engine.dtype),
                  'device':str(tts.engine.device), 'warmup_seconds':time.perf_counter()-cold,
                  'torch_peak_allocated_bytes':0,'torch_peak_reserved_bytes':0}
        (output/'ready.json').write_text(json.dumps(report),encoding='utf-8')
        pieces = []
        owners = []
        for case in corpus['cases']:
            for chunk in production.split_text(case['text']):
                normalized, gaps = normalize_to_chunks_v3_with_gaps(chunk,max_chars=256)
                # Both infer(single outer chunk) and infer_batch([outer chunks])
                # use this same normalizer/default max_chars and per-text joining.
                report['normalization'].append({'id':case['id'],'text':chunk,
                                                'internal_chunks':normalized,'gaps':gaps})
                pieces.append(chunk)
                owners.append(case['id'])
        order = sorted(range(len(pieces)),key=lambda i:len(pieces[i]))
        chunk_dir = output/'chunks'
        chunk_dir.mkdir()
        values = {}
        def cleanup():
            gc.collect()
            torch.cuda.empty_cache()
        def infer(indices):
            torch.cuda.reset_peak_memory_stats()
            result = tts.infer_batch([pieces[i] for i in indices],voice=voice,batch_size=len(indices))
            torch.cuda.synchronize()
            report['torch_peak_allocated_bytes'] = max(report['torch_peak_allocated_bytes'],torch.cuda.max_memory_allocated())
            report['torch_peak_reserved_bytes'] = max(report['torch_peak_reserved_bytes'],torch.cuda.max_memory_reserved())
            for i,wav in zip(indices,result):
                wav=np.asarray(wav,dtype=np.float32)
                if wav.ndim!=1 or not wav.size or not np.isfinite(wav).all() or np.max(np.abs(wav))<1e-5:
                    raise ValueError('Invalid batch chunk '+str(i))
                # Retain completed chunks, including when a later group fails.
                sf.write(chunk_dir/f'{i:04d}.wav',wav,tts.sample_rate,subtype='FLOAT')
            return result
        started=time.perf_counter()
        try:
            while order:
                indices=[]
                chars=0
                while order and len(indices)<size and (not indices or chars+len(pieces[order[0]])<=1200):
                    i=order.pop(0)
                    indices.append(i)
                    chars+=len(pieces[i])
                waves=recover_batch(indices,infer,torch.cuda.OutOfMemoryError,cleanup,report['oom_events'])
                values.update(zip(indices,waves))
                # Publish every completed cue immediately with production pauses/PCM.
                for case in corpus['cases']:
                    indices_for_id=[i for i,owner in enumerate(owners) if owner==case['id']]
                    path=output/(case['id']+'.wav')
                    if path.exists() or not all(i in values for i in indices_for_id):
                        continue
                    pause=np.zeros(int(.08*tts.sample_rate),dtype=np.float32)
                    parts=[]
                    for n,i in enumerate(indices_for_id):
                        if n: parts.append(pause)
                        parts.append(values[i])
                    audio=np.concatenate(parts)
                    audio*=min(1.,.95/max(float(np.max(np.abs(audio))),1e-9))
                    sf.write(path,audio,tts.sample_rate,subtype='PCM_16')
                    report['items'].append({'id':case['id'],**baseline.validate_wav(path,case['text'],case.get('target_seconds'))})
            report['warm_corpus_seconds']=time.perf_counter()-started
        except Exception as exc:
            report['errors'].append(type(exc).__name__+': '+str(exc))
        report['technical_passed']=(len(report['items'])==len(corpus['cases']) and
                                     {r['id'] for r in report['items']}=={c['id'] for c in corpus['cases']} and not report['errors'])
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        # We deliberately do not enter the production request loop.
        raise SystemExit(0 if report['technical_passed'] else 1)
    production.create_engine=create
    sys.argv=[production.__file__,str(baseline.BASE/'storage/voices/ngoc_huyen_review_phim/reference.wav')]
    with contextlib.redirect_stdout(sys.stderr):
        production.main()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--reference',type=Path,default=baseline.BASE/'storage/maintenance/vieneu_batch_baseline_20261105/summary.json')
    parser.add_argument('--sizes',type=int,nargs='+',default=[2,4,8])
    parser.add_argument('--rounds',type=int,default=3)
    parser.add_argument('--worker',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--review-only',action='store_true')
    parser.add_argument('--asr',action='store_true',help='Offline small Whisper diagnostic, after benchmark only')
    args=parser.parse_args()
    corpus_path=baseline.BASE/'tests/fixtures/vieneu_benchmark.json'
    if args.worker:
        worker(args.output,args.sizes[0],corpus_path)
        return
    if args.review_only:
        review=create_review(args.output,args.reference,args.asr)
        print(json.dumps({'review_batch':review['candidate_batch_size'],'asr_status':review['asr_status']}),flush=True)
        return
    if args.rounds not in range(1,4) or any(s not in [1,2,4,8] for s in args.sizes):
        parser.error('Use 1..3 rounds and batch 1/2/4/8')
    from src.tts.vieneu import worker_python, identity
    from src.tts.vieneu_runtime import runtime
    reference=json.loads(args.reference.read_text(encoding='utf-8'))
    corpus,digest=baseline.load_corpus(corpus_path)
    if reference['corpus_sha256']!=digest or not all(r['technical_passed'] for r in reference['rounds']):
        raise ValueError('Reference corpus or validation mismatch')
    executable,device,runtime_identity=runtime()
    if device!='cuda' or any(r['runtime_identity']!=runtime_identity or r['voice_identity']!=identity() for r in reference['rounds']):
        raise ValueError('Reference runtime/voice identity mismatch')
    refs={c['id']:statistics.median(next(i['audio_seconds'] for i in r['items'] if i['id']==c['id']) for r in reference['rounds']) for c in corpus['cases']}
    args.output.mkdir(parents=True,exist_ok=False)
    summary={'corpus_sha256':digest,'reference':str(args.reference),'rounds':[],
             'limitations':['Duration differences are screening, not proof of missing speech; listening pending.',
                            'Batch timing includes extra chunk evidence writes; no translation/timeline assembly.',
                            'New process per round; OS disk cache is not cleared.']}
    env=os.environ.copy()
    env.update(VIENEU_DEVICE='cuda',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',VIENEU_CPU_THREADS='2',PYTHONIOENCODING='utf-8')
    executable=worker_python(executable,device,env)
    previous=reference['median_warm_seconds']
    for size in args.sizes:
        rows=[]
        for number in range(1,args.rounds+1):
            folder=args.output/f'batch_{size}_round_{number:02d}'
            folder.mkdir()
            started=time.perf_counter()
            with (folder/'worker.log').open('w',encoding='utf-8') as log:
                process=subprocess.Popen([executable,str(Path(__file__).resolve()),'--worker','--output',str(folder.resolve()),'--sizes',str(size)],cwd=baseline.BASE,env=env,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                monitor=baseline.ResourceMonitor(process)
                monitor.thread.start()
                ready_seconds=None
                try:
                    while process.poll() is None:
                        if ready_seconds is None and (folder/'ready.json').exists():
                            ready_seconds=time.perf_counter()-started
                        if time.perf_counter()-started>600:
                            process.kill()
                            break
                        time.sleep(.1)
                    process.wait(timeout=10)
                finally:
                    if process.poll() is None: process.kill(); process.wait(timeout=10)
                    monitor.stop.set()
                    monitor.thread.join(timeout=5)
            report_path=folder/'report.json'
            row=json.loads(report_path.read_text(encoding='utf-8')) if report_path.exists() else {'technical_passed':False,'errors':['Worker exit '+str(process.returncode)],'items':[],'batch_size':size}
            row.update(round=number,cold_first_audio_seconds=ready_seconds,peak_tree_rss_bytes=monitor.peak_tree_rss_bytes,peak_whole_gpu_used_mib=monitor.peak_gpu_used_mib,monitor_errors=monitor.errors)
            for item in row['items']:
                item.update(duration_comparison(item['audio_seconds'],refs[item['id']]))
            report_path.write_text(json.dumps(row,ensure_ascii=False,indent=2),encoding='utf-8')
            rows.append(row)
            summary['rounds'].append(row)
            (args.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps({'batch':size,'round':number,'passed':row['technical_passed'],'warm':row.get('warm_corpus_seconds'),'duration_flags':sum(i.get('duration_review_required',False) for i in row['items']),'errors':row['errors']}),flush=True)
            if not row['technical_passed']: return 1
        median=statistics.median(r['warm_corpus_seconds'] for r in rows)
        # Predeclared screening stop: don't spend larger batches on no clear gain,
        # OOM, or whole-device sampled use above 90% of this 4 GiB machine.
        stop=(median>previous*.9 or any(r['oom_events'] for r in rows)
              or any((r['peak_whole_gpu_used_mib'] or 0)>=3686 for r in rows))
        if stop:
            summary['stop_reason']='Less than 10% improvement versus preceding size, OOM, or sampled VRAM >=90% of 4 GiB'
            (args.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
            break
        previous=median
    return 0


if __name__=='__main__':
    sys.exit(main())
