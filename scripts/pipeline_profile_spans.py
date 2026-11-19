"""Opt-in diagnostic spans; no text, credentials or provider responses recorded."""
import functools
import json
import threading
import time
import uuid


class Recorder:
    def __init__(self,path):
        self.path=path
        self.local=threading.local()
        self.lock=threading.Lock()

    def wrap(self,owner,method,label):
        original=getattr(owner,method)
        @functools.wraps(original)
        def measured(*args,**kwargs):
            stack=getattr(self.local,'stack',[])
            self.local.stack=stack
            span={'id':uuid.uuid4().hex,'parent':stack[-1]['id'] if stack else None,
                  'name':label,'thread':threading.get_ident(),'children_seconds':0.0}
            stack.append(span)
            start=time.perf_counter()
            try:
                result=original(*args,**kwargs)
                if label=='tts.cache.get':span['hit']=result is not None
                span['ok']=True
                return result
            except BaseException as exc:
                span.update(ok=False,error_type=type(exc).__name__)
                raise
            finally:
                elapsed=time.perf_counter()-start
                stack.pop()
                if stack:stack[-1]['children_seconds']+=elapsed
                span.update(inclusive_seconds=elapsed,exclusive_same_thread_seconds=max(0,elapsed-span['children_seconds']))
                with self.lock:
                    with self.path.open('a',encoding='utf-8') as f:f.write(json.dumps(span)+'\n')
        setattr(owner,method,measured)


def install(path):
    from src.tts.vieneu import VieNeuTTSProvider
    from src.tts.timeline import AudioTimelineBuilder
    from src.tts.cache import TTSCache
    from src.translation.timing_shortener import TimingShortener,MeaningVerifier
    from src.translation.batch_translator import BatchTranslator
    from src.ocr.vsf_adapter import VideoSubFinderAdapter
    from src.ocr.gpu_client import GPUOCREngine
    from src.ocr import source_recheck, bilingual_recheck
    r=Recorder(path)
    for owner,method,label in [
        (VieNeuTTSProvider,'synthesize','tts.provider'),
        (AudioTimelineBuilder,'synthesize_cues','tts.timeline'),
        (TTSCache,'get','tts.cache.get'),
        (TimingShortener,'shorten','translation.shorten'),
        (MeaningVerifier,'translate_cues','translation.meaning'),
        (BatchTranslator,'_call_gemini_with_rotation','translation.api_with_retry'),
        (VideoSubFinderAdapter,'extract_cues','ocr.extract'),
        (VideoSubFinderAdapter,'_create_scan_proxy','ocr.proxy'),
        (VideoSubFinderAdapter,'_run_vsf','ocr.scan'),
        (VideoSubFinderAdapter,'recognize_vsf_images','ocr.images'),
        (source_recheck,'recheck_uncertain_cues','ocr.source_recheck'),
        (source_recheck,'learn_band','ocr.caption_anchors'),
        (bilingual_recheck,'recover','ocr.bilingual_recheck'),
        (GPUOCREngine,'recognize','ocr.gpu_request')]:
        r.wrap(owner,method,label)
    return r
