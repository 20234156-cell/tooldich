"""Auto ROI must replace the completed download label before detection runs."""
import pytest

from src.jobs.logger import PipelineException, SystemErrorCodes
from src.pipeline.orchestrator import PipelineOrchestrator
from src.pipeline.roi import ROISet
from src.pipeline.state_machine import PipelineState


@pytest.mark.parametrize('with_callback', [True, False])
def test_run_all_reports_auto_roi_before_detector_work(tmp_path, monkeypatch, with_callback):
    from src.jobs.system import manager

    runner = PipelineOrchestrator()
    source = tmp_path / 'source.mp4'
    source_ready = False
    events = []

    def get_source(_):
        if not source_ready:
            raise PipelineException(SystemErrorCodes.MEDIA_NOT_FOUND, 'Source not downloaded')
        return source

    def download(*args, **kwargs):
        nonlocal source_ready
        source_ready = True

    def detect(_):
        # Inspect while the detector is entered, not after an OCR probe replaces
        # the label. This reproduces the real interval that displayed download.
        if with_callback:
            assert [label for _, label in events] == [
                'Tải video nguồn', 'Bước 4/9 · Auto: đang tìm vùng phụ đề (ROI)',
            ]
            assert events[-1][0] == 0
        else:
            assert events == []
        raise RuntimeError('Stop before external detection')

    monkeypatch.setattr(runner, 'get_source_video_path', get_source)
    monkeypatch.setattr(manager, 'download_and_validate_project', download)
    monkeypatch.setattr(runner, 'get_roi_set', lambda _: ROISet())
    monkeypatch.setattr(runner, 'get_state', lambda _: PipelineState())
    monkeypatch.setattr(runner, 'auto_detect_roi', detect)
    callback = (lambda percent, label: events.append((percent, label))) if with_callback else None

    with pytest.raises(RuntimeError, match='Stop before external detection'):
        runner.run_all('auto_roi_progress', extraction_mode='auto', progress_callback=callback)
