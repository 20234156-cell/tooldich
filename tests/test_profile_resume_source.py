import importlib.util
from pathlib import Path
from types import SimpleNamespace
import pytest
from src.jobs.logger import PipelineException,SystemErrorCodes


def test_resume_rechecks_source_and_does_not_hide_other_failures():
    spec=importlib.util.spec_from_file_location('profile_script',Path(__file__).parents[1]/'scripts/profile_video_pipeline.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    assert module.source_present(SimpleNamespace(get_source_video_path=lambda p:Path('source.mp4')),'p')
    def missing(p):raise PipelineException(SystemErrorCodes.MEDIA_NOT_FOUND,'missing')
    assert not module.source_present(SimpleNamespace(get_source_video_path=missing),'p')
    def failed(p):raise RuntimeError('database unavailable')
    with pytest.raises(RuntimeError,match='database'):
        module.source_present(SimpleNamespace(get_source_video_path=failed),'p')
