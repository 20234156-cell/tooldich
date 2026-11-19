"""Bounded chunk batching, explicit identities and recoverable CUDA OOM."""
import base64
import io
import time


def synthesize_request(tts, voice, items, size, max_chars, split, emit, log, oom_type, cleanup, request_id):
    import numpy as np
    import soundfile as sf
    ids=[i['id'] for i in items]
    if not ids or len(ids)>8 or any(not isinstance(i,str) or not i for i in ids) or len(set(ids))!=len(ids):
        raise ValueError('Invalid/duplicate batch cue IDs')
    chunks=[]
    for item in items:
        if not isinstance(item['text'],str) or not item['text'].strip(): raise ValueError('Empty batch text')
        for local,text in enumerate(split(item['text'])):
            chunks.append({'cue_id':item['id'],'cue_chunk_index':local,'chunk_index':len(chunks),
                           'text':text,'words':len(text.split()),'chars':len(text)})
    order=sorted(range(len(chunks)),key=lambda i:len(chunks[i]['text']))
    values={}
    completed=[]
    attempts=0
    oom_count=0
    def group(indices,level,depth=0):
        nonlocal attempts,oom_count
        attempts+=1
        trace={'request_id':request_id,'batch_id':attempts,'requested_batch_size':size,
               'actual_batch_size':len(indices),'fallback_depth':depth,
               'chunks':[{k:v for k,v in chunks[i].items() if k!='text'} for i in indices]}
        started=time.perf_counter()
        log({'event':'batch_start',**trace})
        failed=False
        try:
            waves=tts.infer_batch([chunks[i]['text'] for i in indices],voice=voice,batch_size=len(indices))
            if len(waves)!=len(indices): raise ValueError('Batch output count mismatch')
            checked=[]
            for i,w in zip(indices,waves):
                w=np.asarray(w,dtype=np.float32)
                if w.ndim!=1 or not w.size or not np.isfinite(w).all() or np.max(np.abs(w))<1e-5:
                    raise ValueError('Invalid batch audio')
                seconds=len(w)/tts.sample_rate
                if seconds<max(.08,chunks[i]['words']*.035) or seconds>max(10,chunks[i]['words']*1.5):
                    raise ValueError('Batch audio duration outside technical bounds')
                checked.append(w)
            log({'event':'batch_done',**trace,'seconds':time.perf_counter()-started,'oom_count':oom_count})
            return checked
        except oom_type:
            failed=True
        except Exception as exc:
            log({'event':'batch_failed',**trace,'seconds':time.perf_counter()-started,'error':type(exc).__name__})
            raise
        if failed:
            oom_count+=1
            log({'event':'batch_oom',**trace,'seconds':time.perf_counter()-started,'oom_count':oom_count})
            # No exception/tensor traceback from the failed call is retained here.
            cleanup()
            if len(indices)==1: raise RuntimeError('CUDA OOM at batch1; completed cue caches retained')
            lower=max(1,min(level//2,2**((len(indices)-1).bit_length()-1)))
            result=[]
            for n in range(0,len(indices),lower): result.extend(group(indices[n:n+lower],lower,depth+1))
            return result
    while order:
        indices=[]
        chars=0
        while order and len(indices)<size and (not indices or chars+chunks[order[0]]['chars']<=max_chars):
            i=order.pop(0);indices.append(i);chars+=chunks[i]['chars']
        waves=group(indices,size)
        values.update(zip(indices,waves))
        for cue_id in ids:
            owned=[i for i,c in enumerate(chunks) if c['cue_id']==cue_id]
            if cue_id in completed or not all(i in values for i in owned): continue
            pause=np.zeros(int(.08*tts.sample_rate),dtype=np.float32)
            parts=[]
            for n,i in enumerate(owned):
                if n: parts.append(pause)
                parts.append(values[i])
            audio=np.concatenate(parts)
            audio*=min(1.,.95/max(float(np.max(np.abs(audio))),1e-9))
            buffer=io.BytesIO();sf.write(buffer,audio,tts.sample_rate,format='WAV',subtype='PCM_16')
            emit({'type':'item','id':cue_id,'audio':base64.b64encode(buffer.getvalue()).decode('ascii')})
            completed.append(cue_id)
            log({'event':'cue_done','request_id':request_id,'cue_id':cue_id,'chunk_indices':owned,
                 'cue_chunk_indices':[chunks[i]['cue_chunk_index'] for i in owned],'audio_seconds':len(audio)/tts.sample_rate})
            for i in owned: del values[i]
    if set(completed)!=set(ids) or len(completed)!=len(ids): raise ValueError('Incomplete batch IDs')
    return {'ok':True,'type':'complete','ids':completed,'oom_count':oom_count}
