"""Lossless import of a pre-Studio SQLite workspace into a current database.

The caller stages media first and provides an idle, disposable destination DB.
Existing destination rows, including intentionally empty transcripts, are kept.
"""
import json
import math
from pathlib import Path
import sqlite3
import os
from contextlib import closing

TABLES = ('projects', 'sources', 'media', 'transcripts', 'scenes', 'scripts', 'jobs', 'exports', 'job_logs')


def publish_database(staged, destination):
    """Checkpoint and close both SQLite handles before atomic Windows replace."""
    staged, destination = Path(staged), Path(destination)
    if staged.resolve() == destination.resolve() or not staged.is_file() or not destination.is_file():
        raise ValueError('Distinct existing database files are required')
    with closing(sqlite3.connect(staged)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Staged database integrity check failed')
        if db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
            raise RuntimeError('Staged database has an active WAL writer')
        db.execute('PRAGMA journal_mode=DELETE')
    with closing(sqlite3.connect(destination)) as db:
        if db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
            raise RuntimeError('Destination database has an active WAL writer')
        db.execute('PRAGMA journal_mode=DELETE')
    os.replace(staged, destination)


def legacy_segments(segments):
    """Keep old fields and add the current cue names without inventing approval."""
    if not isinstance(segments, list):
        raise ValueError('Transcript segments must be a list')
    result = []
    indices = set()
    for position, segment in enumerate(segments, 1):
        if not isinstance(segment, dict):
            raise ValueError('Transcript cue must be an object')
        row = dict(segment)
        index = row.setdefault('index', position)
        if not isinstance(index, int) or isinstance(index, bool) or index in indices:
            raise ValueError('Duplicate or invalid cue index')
        indices.add(index)
        start, end = row.get('start'), row.get('end')
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError('Invalid cue timing')
        row.setdefault('text', row.get('original', ''))
        row.setdefault('vi_text', row.get('translated'))
        row.setdefault('translation_origin', 'legacy')
        row.setdefault('is_approved', False)
        result.append(row)
    return result


def relocate(value, old_root, new_root):
    if isinstance(value, dict):
        return {key: relocate(item, old_root, new_root) for key, item in value.items()}
    if isinstance(value, list):
        return [relocate(item, old_root, new_root) for item in value]
    if isinstance(value, str):
        normal = value.replace('\\', '/')
        old = str(old_root).replace('\\', '/').rstrip('/')
        if normal.lower() == old.lower() or normal.lower().startswith(old.lower() + '/'):
            return str(Path(new_root) / normal[len(old):].lstrip('/'))
    return value


def import_database(destination, legacy):
    """Append disjoint projects atomically; remap numeric row IDs, keep job IDs."""
    if Path(destination).resolve() == Path(legacy).resolve():
        raise ValueError('Source and destination must differ')
    source = sqlite3.connect(Path(legacy).resolve().as_uri() + '?mode=ro', uri=True)
    source.row_factory = sqlite3.Row
    target = sqlite3.connect(destination)
    try:
        if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Legacy database integrity check failed')
        old_ids = {row[0] for row in source.execute('SELECT id FROM projects')}
        current_ids = {row[0] for row in target.execute('SELECT id FROM projects')}
        if old_ids & current_ids:
            raise ValueError('Project ID collision; import refused')
        jobs = {row[0] for row in source.execute('SELECT id FROM jobs')}
        if jobs & {row[0] for row in target.execute('SELECT id FROM jobs')}:
            raise ValueError('Job ID collision; import refused')
        counts = {}
        with target:
            for table in TABLES:
                columns = {row[1] for row in target.execute(f'PRAGMA table_info("{table}")')}
                rows = source.execute(f'SELECT * FROM "{table}" ORDER BY id').fetchall()
                numeric_id = table not in {'projects', 'jobs'}
                next_id = target.execute(f'SELECT COALESCE(MAX(id), 0) FROM "{table}"').fetchone()[0] if numeric_id else None
                for original in rows:
                    row = dict(original)
                    owner = row.get('project_id')
                    if owner is not None and owner not in old_ids:
                        raise ValueError(f'Orphan legacy row in {table}; import refused')
                    if table == 'job_logs' and row['job_id'] not in jobs:
                        raise ValueError('Orphan legacy job log; import refused')
                    if set(row) - columns:
                        raise ValueError(f'Unknown legacy columns in {table}')
                    if numeric_id:
                        next_id += 1
                        row['id'] = next_id
                    if table == 'transcripts':
                        row['segments'] = json.dumps(legacy_segments(json.loads(row['segments'])), ensure_ascii=False)
                    names = list(row)
                    quoted = ','.join('"' + name + '"' for name in names)
                    target.execute(f'INSERT INTO "{table}" ({quoted}) VALUES ({",".join("?" for _ in names)})', [row[name] for name in names])
                counts[table] = len(rows)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Merged database integrity check failed')
        return counts
    finally:
        source.close()
        target.close()
