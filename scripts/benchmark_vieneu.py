"""Repeatable, isolated baseline for the production Ngoc Huyen GPU worker.

No project/database writes, audio cache, provider calls or production defaults.
Output WAV validation is technical screening, NOT proof of intelligibility.
"""
import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import statistics
import subprocess
import sys
import threading
import time

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))


def load_corpus(path):
    raw = path.read_bytes()
    corpus = json.loads(raw)
    cases = corpus['cases']
    if not cases or corpus['voice'] != 'vieneu:ngoc_huyen_review_phim':
        raise ValueError('Expected registered Ngoc Huyen corpus')
    ids = []
    for case in cases:
        if not isinstance(case['id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]+', case['id']):
            raise ValueError('Unsafe or missing cue ID')
        if not isinstance(case['text'], str) or not case['text'].strip():
            raise ValueError('Empty benchmark text')
        if 'target_seconds' in case and (not math.isfinite(case['target_seconds']) or case['target_seconds'] <= 0):
            raise ValueError('Invalid timing target')
        ids.append(case['id'])
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate cue IDs')
    return corpus, hashlib.sha256(raw).hexdigest()


def validate_wav(path, text, target_seconds=None):
    import numpy as np
    import soundfile as sf
    data, rate = sf.read(path, dtype='float32')
    if data.ndim != 1 or rate != 48000 or not data.size or not np.isfinite(data).all() or np.max(np.abs(data)) < 1e-5:
        raise ValueError('Invalid, silent, non-mono or wrong-rate WAV')
    duration = len(data) / rate
    words = len(text.split())
    flags = []
    if duration < max(.08, words * .035):
        flags.append('suspiciously_short_requires_listening')
    if duration > max(10, words * 1.5):
        flags.append('suspiciously_long_requires_listening')
    return {'audio_seconds':duration, 'sample_rate':rate, 'words':words,
            'target_seconds':target_seconds,
            'target_overflow_seconds':max(0, duration-target_seconds) if target_seconds else None,
            'review_flags':flags, 'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}


def worker(metrics):
    # Instrument only this benchmark process. Synthesis and PCM publication
    # still execute the unmodified production worker, including chunk18/pause.
    from src.tts import vieneu_worker as production
    import torch
    import numpy as np
    torch.manual_seed(1234)
    np.random.seed(1234)
    original_create = production.create_engine
    def emit(record):
        with metrics.open('a', encoding='utf-8') as out:
            out.write(json.dumps(record)+'\n')
    def create(factory, reference):
        started = time.perf_counter()
        engine = original_create(factory, reference)
        original_infer = engine.infer
        def infer(*args, **kwargs):
            torch.cuda.synchronize()
            torch.cuda.reset_peak_memory_stats()
            begin = time.perf_counter()
            result = original_infer(*args, **kwargs)
            torch.cuda.synchronize()
            emit({'event':'chunk','seconds':time.perf_counter()-begin,
                  'torch_peak_allocated_bytes':torch.cuda.max_memory_allocated(),
                  'torch_peak_reserved_bytes':torch.cuda.max_memory_reserved()})
            return result
        engine.infer = infer
        emit({'event':'ready','model_create_seconds':time.perf_counter()-started,
              'device':str(engine.engine.device), 'dtype':str(engine.engine.dtype),
              'torch_version':torch.__version__, 'gpu':torch.cuda.get_device_name(0)})
        return engine
    production.create_engine = create
    reference = BASE/'storage/voices/ngoc_huyen_review_phim/reference.wav'
    sys.argv = [str(Path(production.__file__)), str(reference)]
    production.main()


class ResourceMonitor:
    def __init__(self, process):
        self.process = process
        self.stop = threading.Event()
        self.peak_tree_rss_bytes = 0
        self.peak_gpu_used_mib = None
        self.gpu_samples = 0
        self.errors = []
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        import psutil
        next_gpu = 0
        while not self.stop.is_set():
            try:
                parent = psutil.Process(self.process.pid)
                processes = [parent] + parent.children(recursive=True)
                rss = 0
                for process in processes:
                    try: rss += process.memory_info().rss
                    except psutil.Error: pass
                self.peak_tree_rss_bytes = max(self.peak_tree_rss_bytes, rss)
            except psutil.Error:
                pass
            if time.monotonic() >= next_gpu:
                next_gpu = time.monotonic()+1
                try:
                    result = subprocess.run(['nvidia-smi','--query-gpu=memory.used','--format=csv,noheader,nounits'],
                        capture_output=True,text=True,timeout=3,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    value = int(result.stdout.splitlines()[0].strip())
                    self.peak_gpu_used_mib = max(self.peak_gpu_used_mib or 0, value)
                    self.gpu_samples += 1
                except (OSError, ValueError, IndexError, subprocess.TimeoutExpired) as exc:
                    if not self.errors: self.errors.append(type(exc).__name__)
            self.stop.wait(.1)


def request(process, responses, text, timeout):
    started = time.perf_counter()
    process.stdin.write(json.dumps({'text':text}, ensure_ascii=True)+'\n')
    process.stdin.flush()
    line = responses.get(timeout=timeout)
    result = json.loads(line) if line else {}
    if not result.get('ok'):
        raise RuntimeError('Worker failed: '+str(result.get('error','EOF')))
    return base64.b64decode(result['audio'], validate=True), time.perf_counter()-started


def run_round(corpus, output, number):
    from src.tts.vieneu import worker_python, identity
    from src.tts.vieneu_runtime import runtime
    executable, device, runtime_identity = runtime()
    if device != 'cuda':
        raise RuntimeError('Validated CUDA runtime required for this baseline')
    folder = output/f'round_{number:02d}'
    folder.mkdir()
    metrics = folder/'worker_metrics.jsonl'
    env = os.environ.copy()
    env.update(VIENEU_DEVICE='cuda', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
               PYTHONIOENCODING='utf-8', VIENEU_CPU_THREADS='2')
    executable = worker_python(executable, 'cuda', env)
    responses = queue.Queue()
    started = time.perf_counter()
    with (folder/'worker.log').open('w', encoding='utf-8') as log:
        process = subprocess.Popen([executable,str(Path(__file__).resolve()),'--worker',str(metrics)],
            cwd=BASE,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,
            text=True,encoding='utf-8',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        def reader():
            for line in process.stdout: responses.put(line)
            responses.put('')
        threading.Thread(target=reader,daemon=True).start()
        monitor = ResourceMonitor(process)
        monitor.thread.start()
        report = {'round':number,'batch_size':1,'items':[], 'errors':[], 'runtime_identity':runtime_identity,
                  'voice_identity':identity(), 'seed':1234}
        try:
            warmup, _ = request(process,responses,'Xin chào, đây là bài đo giọng đọc.',180)
            (folder/'warmup.wav').write_bytes(warmup)
            report['cold_first_audio_seconds'] = time.perf_counter()-started
            warm_started = time.perf_counter()
            for case in corpus['cases']:
                payload, elapsed = request(process,responses,case['text'],90)
                path = folder/(case['id']+'.wav')
                path.write_bytes(payload)
                audio = validate_wav(path,case['text'],case.get('target_seconds'))
                report['items'].append({'id':case['id'],'category':case['category'],
                                        'request_seconds':elapsed,**audio})
            report['warm_corpus_end_to_end_seconds'] = time.perf_counter()-warm_started
        except Exception as exc:
            report['errors'].append(type(exc).__name__+': '+str(exc))
        finally:
            if process.poll() is None: process.kill()
            process.wait(timeout=10)
            process.stdin.close()
            process.stdout.close()
            monitor.stop.set()
            monitor.thread.join(timeout=5)
        records = [json.loads(line) for line in metrics.read_text(encoding='utf-8').splitlines()] if metrics.exists() else []
        chunks = [r for r in records if r['event']=='chunk']
        report.update(total_seconds=time.perf_counter()-started,
            peak_tree_rss_bytes=monitor.peak_tree_rss_bytes,
            peak_whole_gpu_used_mib=monitor.peak_gpu_used_mib,
            gpu_sample_count=monitor.gpu_samples,monitor_errors=monitor.errors,
            torch_peak_allocated_bytes=max((r['torch_peak_allocated_bytes'] for r in chunks),default=None),
            torch_peak_reserved_bytes=max((r['torch_peak_reserved_bytes'] for r in chunks),default=None),
            model_ready=next((r for r in records if r['event']=='ready'),None))
        report['expected_ids'] = [c['id'] for c in corpus['cases']]
        report['id_coverage_passed'] = [r['id'] for r in report['items']] == report['expected_ids']
        report['technical_passed'] = report['id_coverage_passed'] and not report['errors']
        (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus',type=Path,default=BASE/'tests/fixtures/vieneu_benchmark.json')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--rounds',type=int,default=3)
    parser.add_argument('--worker',type=Path,help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker)
        return
    if not args.output or not 1 <= args.rounds <= 5:
        parser.error('Specify a fresh --output folder and 1..5 rounds')
    corpus, digest = load_corpus(args.corpus)
    output = args.output.resolve()
    output.mkdir(parents=True,exist_ok=False)
    report = {'corpus_sha256':digest,'case_count':len(corpus['cases']),'batch_size':1,
              'audio_cache_used':False,'rounds':[],
              'limitations':['Whole GPU memory includes other applications; process RSS sums may count shared pages.',
                             'Duration/PCM checks do not establish complete speech or voice similarity; listening required.',
                             'Warm end-to-end includes protocol, file writing and validation; excludes translation/timeline assembly.']}
    for number in range(1,args.rounds+1):
        result = run_round(corpus,output,number)
        report['rounds'].append(result)
        valid = [r['warm_corpus_end_to_end_seconds'] for r in report['rounds'] if r['technical_passed']]
        report['median_warm_seconds'] = statistics.median(valid) if valid else None
        report['worst_warm_seconds'] = max(valid) if valid else None
        report['failed_rounds'] = sum(not r['technical_passed'] for r in report['rounds'])
        (output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'round':number,'technical_passed':result['technical_passed'],
                          'cold_seconds':result.get('cold_first_audio_seconds'),
                          'warm_seconds':result.get('warm_corpus_end_to_end_seconds'),
                          'files':len(result['items']),'errors':result['errors']}),flush=True)
        if not result['technical_passed']: break


if __name__ == '__main__':
    main()
