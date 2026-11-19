import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.pipeline import auto_workflow
from src.translation.memory import atomic_json


@pytest.fixture(params=['translation', 'auto'])
def write_json(request, monkeypatch):
    if request.param == 'translation':
        return atomic_json

    def write_auto(target, value):
        monkeypatch.setattr(auto_workflow, 'path', lambda project_id: target)
        auto_workflow.write('test-project', value)

    return write_auto


@pytest.fixture
def target(tmp_path):
    directory = tmp_path / 'atomic-write'
    directory.mkdir()
    path = directory / 'progress.json'
    path.write_text('{"old":true}', encoding='utf-8')
    return path


def denied():
    error=PermissionError('Windows target is temporarily open')
    error.winerror=5
    return error


def test_transient_windows_reader_does_not_abort_completed_atomic_write(target, write_json):
    import os
    replace=os.replace
    calls=[]
    def briefly_locked(source,destination):
        calls.append(Path(source).read_bytes())
        if len(calls)<3:raise denied()
        replace(source,destination)
    with patch('src.atomic_files.os.replace',side_effect=briefly_locked), \
         patch('src.atomic_files.time.sleep') as sleep:
        write_json(target,{'complete':True})
    assert len(calls)==3 and len(set(calls))==1
    assert json.loads(target.read_text())=={'complete':True}
    assert [c.args[0] for c in sleep.call_args_list]==[.02,.04]
    assert list(target.parent.iterdir()) == [target]


def test_persistent_windows_permission_failure_preserves_old_data_and_raises(target, write_json):
    with patch('src.atomic_files.os.replace',side_effect=denied()) as replace, \
         patch('src.atomic_files.time.sleep'),pytest.raises(PermissionError):
        write_json(target,{'complete':True})
    assert replace.call_count==6
    assert json.loads(target.read_text())=={'old':True}
    assert list(target.parent.iterdir()) == [target]
