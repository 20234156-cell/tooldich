import json
import sqlite3

import pytest

from src.database.legacy_import import TABLES, import_database, legacy_segments, relocate, publish_database


def test_legacy_transcript_keeps_text_timing_and_extra_fields():
    before = [{'start': 1.25, 'end': 4.5, 'original': '你好', 'translated': 'Xin chào', 'translation_failed': False}]
    after = legacy_segments(before)
    assert before[0] == {key: after[0][key] for key in before[0]}
    assert after[0]['text'] == '你好'
    assert after[0]['vi_text'] == 'Xin chào'
    assert after[0]['index'] == 1
    assert after[0]['is_approved'] is False
    assert 'voice_audio_path' not in after[0]


def test_empty_and_current_transcripts_are_authoritative():
    assert legacy_segments([]) == []
    current = [{'index': 8, 'start': 0, 'end': 2, 'text': '', 'vi_text': '', 'original': 'stale', 'translated': 'stale'}]
    result = legacy_segments(current)[0]
    assert result['text'] == result['vi_text'] == ''
    assert result['index'] == 8


@pytest.mark.parametrize('rows', [
    [{'start': 2, 'end': 1}],
    [{'start': float('nan'), 'end': 2}],
    [{'index': 1, 'start': 0, 'end': 1}, {'index': 1, 'start': 1, 'end': 2}],
])
def test_invalid_legacy_cues_fail_closed(rows):
    with pytest.raises(ValueError):
        legacy_segments(rows)


def test_relocation_uses_path_boundary_and_preserves_other_text(tmp_path):
    data = {'path': r'C:\old\storage\projects\p\source.mp4', 'note': 'mentions C:/old', 'other': 'C:/older/video.mp4'}
    result = relocate(data, 'C:/old', tmp_path)
    assert result['path'] == str(tmp_path/'storage/projects/p/source.mp4')
    assert result['note'] == data['note'] and result['other'] == data['other']


def test_atomic_import_remaps_ids_and_refuses_repeat(isolated_env, tmp_path):
    destination = tmp_path/'test_app.db'
    legacy = tmp_path/'legacy.db'
    with sqlite3.connect(destination) as db, sqlite3.connect(legacy) as old:
        db.backup(old)
        for connection, project in ((db, 'current'), (old, 'legacy')):
            connection.execute("INSERT INTO projects(id,title,status,created_at,updated_at) VALUES (?,?,'CREATED','2026-01-01','2026-01-01')", (project, project))
            segments = [] if project == 'current' else [{'start': 0, 'end': 1, 'original': '源', 'translated': 'dịch'}]
            connection.execute('INSERT INTO transcripts(id,project_id,language,target_language,segments) VALUES (1,?,?,?,?)', (project, 'zh', 'vi', json.dumps(segments)))
    counts = import_database(destination, legacy)
    assert counts['projects'] == counts['transcripts'] == 1
    with sqlite3.connect(destination) as db:
        rows = db.execute('SELECT id,project_id,segments FROM transcripts ORDER BY id').fetchall()
        assert rows[0] == (1, 'current', '[]')
        assert rows[1][0:2] == (2, 'legacy')
        assert json.loads(rows[1][2])[0]['vi_text'] == 'dịch'
    with pytest.raises(ValueError, match='collision'):
        import_database(destination, legacy)


def test_bad_transcript_rolls_back_all_imported_rows(isolated_env, tmp_path):
    destination = tmp_path/'test_app.db'
    legacy = tmp_path/'legacy.db'
    with sqlite3.connect(destination) as db, sqlite3.connect(legacy) as old:
        db.backup(old)
        old.execute("INSERT INTO projects(id,title,status,created_at,updated_at) VALUES ('legacy','legacy','CREATED','2026-01-01','2026-01-01')")
        old.execute("INSERT INTO transcripts(project_id,language,target_language,segments) VALUES ('legacy','zh','vi','[{\"start\":2,\"end\":1}]')")
    with pytest.raises(ValueError):
        import_database(destination, legacy)
    with sqlite3.connect(destination) as db:
        assert db.execute('SELECT count(*) FROM projects').fetchone()[0] == 0


def test_database_publication_closes_sqlite_handles_before_replace(tmp_path):
    from contextlib import closing
    staged, destination = tmp_path/'staged.db', tmp_path/'live.db'
    for path, value in ((staged, 'merged'), (destination, 'legacy')):
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE marker(value TEXT)')
            db.execute('INSERT INTO marker VALUES (?)', (value,))
            db.commit()
    publish_database(staged, destination)
    assert not staged.exists()
    with closing(sqlite3.connect(destination)) as db:
        assert db.execute('SELECT value FROM marker').fetchone()[0] == 'merged'
