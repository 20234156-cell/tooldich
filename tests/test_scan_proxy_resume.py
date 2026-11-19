import copy
from unittest.mock import Mock

import pytest

from src.jobs.logger import TaskCancelled
from src.jobs.resources import TaskInterrupted
from src.ocr import proxy_cache
from src.ocr.vsf_adapter import VideoSubFinderAdapter
from src.pipeline.roi import ROIBox


@pytest.mark.parametrize('change', ['source', 'trim', 'width', 'binary', 'content', 'missing_manifest'])
def test_completed_proxy_rejects_wrong_identity_or_corruption(tmp_path, change):
    path = tmp_path / '_vsf_scan_proxy.mp4'
    path.write_bytes(b'completed proxy')
    key = {'source': 'full-sha256', 'trim': [0, 60], 'width': 960, 'binary': 'version-a'}
    proxy_cache.publish(path, key)
    assert proxy_cache.reuse(path, key)
    changed = copy.deepcopy(key)
    if change == 'content':path.write_bytes(b'corrupted proxy')
    elif change == 'missing_manifest':path.with_suffix('.complete.json').unlink()
    else:changed[change] = 'changed'
    assert not proxy_cache.reuse(path, changed)


@pytest.mark.parametrize('error', [TaskCancelled('cancel'), TaskInterrupted('LOW_DISK_SPACE')])
def test_interrupted_scan_keeps_completed_proxy_but_never_invokes_fallback(tmp_path, monkeypatch, error):
    adapter = VideoSubFinderAdapter()
    proxy = tmp_path / '_vsf_scan_proxy.mp4'
    proxy.write_bytes(b'completed proxy')
    proxy_cache.publish(proxy, {'source': 'hash'})
    monkeypatch.setattr(adapter, 'get_executable', lambda: tmp_path / 'vsf.exe')
    monkeypatch.setattr(adapter, '_create_scan_proxy', lambda **_: proxy)
    monkeypatch.setattr(adapter, '_run_vsf', Mock(side_effect=error))
    fallback = Mock(side_effect=AssertionError('must not start a different scan'))
    monkeypatch.setattr(adapter.fallback_detector, 'detect_cues', fallback)
    with pytest.raises(type(error)):
        adapter.extract_cues(tmp_path / 'source.mp4', ROIBox(x=.1,y=.8,width=.8,height=.15), tmp_path)
    assert proxy_cache.reuse(proxy, {'source': 'hash'})
    fallback.assert_not_called()


def test_reuse_skips_encoder_with_verified_source_and_output(tmp_path, monkeypatch):
    import cv2
    adapter = VideoSubFinderAdapter()
    source = tmp_path / 'source.mp4';source.write_bytes(b'source')
    proxy = tmp_path / '_vsf_scan_proxy.mp4';proxy.write_bytes(b'completed')
    source_key = {'sha256': 'source-hash', 'start': 0, 'duration': 60}
    variants = [['-c:v','libx264','-preset','ultrafast','-crf','28']]
    key = proxy_cache.identity(source_key, 'ffmpeg', 'scale=960:-2', variants)
    proxy_cache.publish(proxy, key)
    capture = Mock()
    capture.get.side_effect = lambda prop: {cv2.CAP_PROP_FRAME_WIDTH:1920,cv2.CAP_PROP_FRAME_COUNT:1800,
                                           cv2.CAP_PROP_FPS:30}.get(prop,0)
    monkeypatch.setattr('src.ocr.vsf_adapter.cv2.VideoCapture', lambda *_: capture)
    monkeypatch.setattr('src.pipeline.render.get_ffmpeg_bin', lambda: 'ffmpeg')
    monkeypatch.setattr('src.gpu.nvenc_available', lambda *_: False)
    monkeypatch.setattr('src.ocr.vsf_adapter.subprocess.Popen', Mock(side_effect=AssertionError('no re-encode')))
    assert adapter._create_scan_proxy(source,tmp_path,0,60,source_identity=source_key) == proxy
