import cv2
import numpy as np
import pytest

from src.ocr.frame_reader import FrameReader
from src.jobs.logger import TaskCancelled


class Capture:
    def __init__(self): self.position = 0
    def get(self, _): return self.position
    def set(self, _, index): self.position = index
    def grab(self): self.position += 1; return True
    def read(self):
        result = self.position
        self.position += 1
        return True, result


def test_reader_keeps_exact_indices_forward_backward_and_large_gaps():
    reader = FrameReader(Capture(), lambda:None)
    for index in [8,10,12,100,3,4]:
        assert reader.read(index) == (True,index)
    assert reader.stats == {'seeks':2,'grabs':10,'reads':6}


def test_reader_cancellation_during_forward_grabs():
    checks = 0
    def check():
        nonlocal checks
        checks += 1
        if checks == 4: raise TaskCancelled('stop')
    reader = FrameReader(Capture(),check)
    with pytest.raises(TaskCancelled): reader.read(40)
    assert reader.stats['reads'] == 0


def test_recovery_without_targets_is_not_running(tmp_path):
    from src.ocr.bilingual_recheck import recover
    result, evidence = recover(tmp_path/'absent.mp4', None, [], None, lambda:None)
    assert result == [] and evidence['status'] == 'not_needed'


def test_checkpoint_batching_and_exception_flush(tmp_path, monkeypatch):
    from src.ocr import bilingual_recheck as module
    from src.ocr.base import Cue
    from src.pipeline.roi import ROIBox
    from test_ocr_source_recheck import Capture as VideoCapture
    from test_sirvietsub_integration import rows
    source=tmp_path/'source.mp4';source.write_bytes(b'fixture')
    cap=VideoCapture(np.zeros((100,200,3),np.uint8))
    monkeypatch.setattr(module.cv2,'VideoCapture',lambda _:cap)
    class Engine:
        cache_version='offline'
        calls=0
        def read_boxes(self,*a,**kw):
            self.calls+=1
            if self.calls == 13*6+1: raise RuntimeError('interrupted')
            return rows()
    cues=[Cue(index=i,start=i*2,end=i*2+1,text='raw',ocr_alternatives=[dict(variant='dominant_text_size',text='你是我的光')]) for i in range(1,16)]
    snapshots=[]
    with pytest.raises(RuntimeError,match='interrupted'):
        module.recover(source,ROIBox(x=0,y=.5,width=1,height=.45),cues,Engine(),lambda:None,
            save=lambda e:snapshots.append((e['status'],e['resolved'])))
    assert snapshots == [('running',12),('failed',13)]
    assert cap.released


def test_bilingual_stops_reading_after_two_views_disagree(tmp_path, monkeypatch):
    from src.ocr import bilingual_recheck as module
    from src.ocr.base import Cue
    from src.pipeline.roi import ROIBox
    from test_ocr_source_recheck import Capture as VideoCapture
    from test_sirvietsub_integration import rows

    source = tmp_path / 'source.mp4'
    source.write_bytes(b'fixture')
    cap = VideoCapture(np.zeros((100, 200, 3), np.uint8))
    monkeypatch.setattr(module.cv2, 'VideoCapture', lambda _: cap)

    class Engine:
        cache_version = 'offline'
        calls = 0

        def read_boxes(self, *args, **kwargs):
            self.calls += 1
            return rows('你是我的光' if self.calls == 1 else '真不一样')

    engine = Engine()
    cue = Cue(index=1, start=1, end=2, text='raw',
              ocr_alternatives=[dict(variant='scene_layout_review')])
    result, report = module.recover(source, ROIBox(x=0, y=.5, width=1, height=.45),
                                    [cue], engine, lambda: None)
    assert result == [cue]
    assert report['decisions'][0]['action'] == 'pending'
    assert 'disagreed' in report['decisions'][0]['reason']
    assert engine.calls == 2
