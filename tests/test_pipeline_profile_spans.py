import importlib.util
import json
from pathlib import Path
import pytest


def test_nested_spans_do_not_add_child_time_twice_and_keep_failures(tmp_path):
    spec=importlib.util.spec_from_file_location('spans',Path(__file__).parents[1]/'scripts/pipeline_profile_spans.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    class Work:
        def child(self): raise ValueError('private text must not be logged')
        def parent(self): return self.child()
    path=tmp_path/'spans.jsonl';r=module.Recorder(path)
    r.wrap(Work,'child','child');r.wrap(Work,'parent','parent')
    with pytest.raises(ValueError):Work().parent()
    rows=[json.loads(line) for line in path.read_text().splitlines()]
    child,parent=rows
    assert child['parent']==parent['id'] and parent['parent'] is None
    assert parent['exclusive_same_thread_seconds']==pytest.approx(parent['inclusive_seconds']-child['inclusive_seconds'])
    assert not parent['ok'] and parent['error_type']=='ValueError'
    assert 'private text' not in path.read_text()
