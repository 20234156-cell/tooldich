"""Controlled real-GPU queue ownership audit, outside pytest/business projects.

Before arm reproduces legacy ownership by disabling only request lease release
inside its isolated process. After arm uses the production lease. This tests the
direct queued single-cue entry point, not the timeline's thread-pool requests.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
TEXT='Xin chào, đây là kiểm tra giữ mô hình giọng đọc.'


def write_report(output,summary):
    lines=['Giữ worker qua hai tác vụ hàng đợi độc lập','',
           'Đường thử: JobQueue gọi trực tiếp tạo một câu, cùng giọng Ngọc Huyền/CUDA FP32/batch1. Nhánh trước mô phỏng duy nhất việc không trả quyền sở hữu sau request; nhánh sau dùng lease production. Không đổi cấu hình app hay dữ liệu dự án.','',
           '| Nhánh | Lần gọi1 (s) | Lần gọi2 (s) | Số tiến trình model đã mở | Cùng PID |','|---|---:|---:|---:|---|']
    for row in summary['reports']:
        if row.get('passed'):
            lines.append(f"| {row['arm']} | {row['calls'][0]['seconds']:.4f} | {row['calls'][1]['seconds']:.4f} | {row['model_process_start_count']} | {row['same_model_process_reused']} |")
    if summary['passed']:
        lines+=['',f"Chênh thời gian lần gọi thứ2: {summary['second_call_seconds_saved']:.4f}s. Bằng chứng mạnh là số lần mở model và PID sống sau DONE; không lấy chênh khởi động lần1 làm speedup vì cache đĩa/trạng thái máy khác."]
    lines+=['','Giới hạn: chỉ2 lời gọi/nhánh, không phải benchmark thống kê;4 WAV được đọc thật kiểm48kHz mono/finite/non-silent. Chưa nghe duyệt và không chứng minh chất lượng phát âm từ PCM. Các test hủy/lỗi/chuyển quyền dùng child pipe thật nhưng model nhỏ offline, không phải GPU cancellation audit.','',
            'Không suy ra toàn bộ pipeline95call được tăng tốc25,91s: trước sửa, đường timeline batch1 chạy pool thiếu ContextVar nên model thường không bị queuehook quản lý. Bản sửa riêng đã truyền Context riêng mỗi future để hủy/ownership đúng. Giữ worker120s vẫn có thể hết hạn giữa render dài và dự án kế tiếp; chưa kéo dài TTL hay ghim VRAM vô hạn.','']
    (output/'REPORT.md').write_text('\n'.join(lines),encoding='utf-8')


def arm(output,mode):
    output.mkdir(parents=True,exist_ok=False)
    os.environ.update(VIENEU_BATCH_SIZE='1',PERSONAL_MODE='true',OFFLINE_MODE='true',
                      DATABASE_URL='sqlite:///'+(output/'isolated.db').resolve().as_posix())
    from config.settings import settings
    from src.jobs.queue import JobQueue
    import src.jobs.process_ownership as ownership
    from src.tts.vieneu import provider,VOICE_ID,identity
    from src.tts.vieneu_runtime import runtime
    import numpy as np
    import soundfile as sf
    if settings.SERVER_MODE or runtime()[1]!='cuda':
        raise RuntimeError('This audit requires personal mode and validated CUDA')
    if mode=='before':
        class LegacyLease:
            def close(self):pass
            def release_provider(self,owned_process):pass
        ownership.lease_subprocess=lambda process:LegacyLease()
    queue=JobQueue(max_workers=1)
    starts=[]
    original_popen=subprocess.Popen
    def observe_start(*args,**kwargs):
        process=original_popen(*args,**kwargs)
        starts.append(process.pid)
        return process
    subprocess.Popen=observe_start
    report={'arm':mode,'entrypoint':'direct JobQueue single-cue request',
            'voice_identity':identity(),'runtime_identity':runtime()[2],
            'text_sha256':hashlib.sha256(TEXT.encode()).hexdigest(),'calls':[],
            'limitations':['Two calls per arm; lifecycle proof, not a statistical throughput benchmark.',
                           'Before disables lease release only, reproducing the former queue cleanup behavior.',
                           'No synthesis settings or model/voice changes; PCM validation does not certify pronunciation.']}
    def publish():
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    try:
        for n in [1,2]:
            def request():
                started=time.perf_counter()
                payload,duration=provider.synthesize(TEXT,voice=VOICE_ID)
                elapsed=time.perf_counter()-started
                data,sr=sf.read(__import__('io').BytesIO(payload),dtype='float32')
                if sr!=48000 or data.ndim!=1 or not len(data) or not np.isfinite(data).all() or np.max(np.abs(data))<1e-5:
                    raise ValueError('Invalid actual WAV')
                path=output/f'call_{n}.wav';path.write_bytes(payload)
                return {'seconds':elapsed,'audio_seconds':duration,'pid':provider._process.pid,
                        'sha256':hashlib.sha256(payload).hexdigest(),'path':str(path.resolve())}
            task=queue.submit(f'isolated-{n}','voiceover',request,update_pipeline_job=False)
            task.future.result(timeout=240)
            if task.state!='DONE':raise RuntimeError(f'{task.state}: {task.error_message}')
            row=task.result
            process=provider._process
            if mode=='before':process.wait(timeout=10)
            row['alive_after_done']=process.poll() is None
            row['task_children_after_done']=len(task.subprocesses)
            report['calls'].append(row)
            publish()
            print(json.dumps({'arm':mode,'call':n,'seconds':row['seconds'],'pid':row['pid'],
                              'alive_after_done':row['alive_after_done']}),flush=True)
        same=report['calls'][0]['pid']==report['calls'][1]['pid']
        report['same_model_process_reused']=same
        report['model_process_start_count']=len(starts)
        report['passed']=(same if mode=='after' else not same)
    except Exception as exc:
        report.update(passed=False,error=type(exc).__name__+': '+str(exc))
    finally:
        if provider._timer:provider._timer.cancel()
        provider.close()
        queue._executor.shutdown(wait=True,cancel_futures=True)
        subprocess.Popen=original_popen
        publish()
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--arm',choices=['before','after'])
    args=parser.parse_args()
    if args.arm:return 0 if arm(args.output,args.arm)['passed'] else 1
    args.output.mkdir(parents=True,exist_ok=False)
    reports=[]
    for mode in ['before','after']:
        with (args.output/(mode+'.log')).open('w',encoding='utf-8') as log:
            result=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--arm',mode,
                '--output',str((args.output/mode).resolve())],cwd=BASE,stdout=log,stderr=log,timeout=500)
        path=args.output/mode/'report.json'
        report=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'arm':mode,'passed':False,'error':'no report'}
        reports.append(report)
        print(json.dumps({'arm':mode,'passed':report['passed'],'exit_code':result.returncode}),flush=True)
        if result.returncode:break
    summary={'reports':reports,'passed':len(reports)==2 and all(r['passed'] for r in reports)}
    if summary['passed']:
        if reports[0]['voice_identity']!=reports[1]['voice_identity'] or reports[0]['runtime_identity']!=reports[1]['runtime_identity']:
            raise ValueError('Runtime/voice identity changed across arms')
        summary['second_call_seconds_saved']=reports[0]['calls'][1]['seconds']-reports[1]['calls'][1]['seconds']
    (args.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    write_report(args.output,summary)
    return 0 if summary['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
