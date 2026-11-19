from unittest.mock import Mock

import pytest

from src.api import app
from src.database.models import Project
from src.jobs.logger import PipelineException


@pytest.mark.parametrize('state', ['QUEUED', 'RUNNING', 'CANCELLED'])
def test_delete_waits_until_worker_is_finished(monkeypatch, state):
    task = Mock(state=state, cancel_requested=True)
    task.future.done.return_value = False
    monkeypatch.setattr(app.queue, 'get_by_project', lambda _: task)
    remove = Mock()
    monkeypatch.setattr(app.manager, 'delete_project', remove)
    with pytest.raises(app.HTTPException) as exc:
        app.delete_project('test_project')
    assert exc.value.status_code == 409
    remove.assert_not_called()


def test_locked_project_files_keep_database_record(monkeypatch, db_session, isolated_env):
    project_id = 'test_locked_project'
    db_session.add(Project(id=project_id))
    db_session.commit()
    folder = isolated_env['storage_dir'] / project_id
    folder.mkdir()
    (folder / 'video.mp4').write_bytes(b'video')

    def locked(_):
        raise PermissionError('file is open')

    with monkeypatch.context() as patch:
        patch.setattr('src.jobs.system.shutil.rmtree', locked)
        with pytest.raises(PipelineException, match='Chưa xóa hết'):
            app.delete_project(project_id)
    assert db_session.get(Project, project_id) is not None
    assert app.delete_project(project_id)['storage_removed'] is True
    assert not folder.exists()
