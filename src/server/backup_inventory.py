"""Derive required files from a read-only SQLite snapshot, never a disk listing."""
import hashlib
import json
from pathlib import Path
import sqlite3
from contextlib import closing


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)


def rows(db, table):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return []
    return [dict(row) for row in db.execute('SELECT * FROM "' + table + '"')]


def inventory(snapshot, data_root, projects_name):
    root = Path(data_root).resolve()
    files, empty, errors = {}, [], []
    with closing(readonly(snapshot)) as db:
        db.row_factory = sqlite3.Row
        baselines = {r['path']: r for r in rows(db, 'file_baselines')}

        def add(path, reason, expected=None, size=None, provenance=None, external=False):
            if not path:
                errors.append({'reason': 'MISSING_DB_PATH', 'reference': reason})
                return
            original = Path(path)
            if not original.is_absolute(): original = root / original
            resolved = original.resolve()
            if original.is_symlink() or (not resolved.is_relative_to(root) and not external):
                errors.append({'reason': 'UNMANAGED_REQUIRED_PATH', 'reference': reason, 'path': str(path)})
                return
            name = resolved.relative_to(root).as_posix() if resolved.is_relative_to(root) else 'external_required/' + hashlib.sha256(str(resolved).encode()).hexdigest() + '/' + resolved.name
            baseline = baselines.get(name)
            if baseline and expected and baseline['sha256'] != expected and provenance!='SNAPSHOT_METADATA':
                errors.append({'reason': 'CONFLICTING_BASELINE', 'path': name})
            if expected is None and baseline:
                expected, size, provenance = baseline['sha256'], baseline['size'], baseline['provenance']
            elif baseline and expected==baseline['sha256'] and provenance not in {'VERIFIED_AT_CREATION','SNAPSHOT_METADATA'}:
                size,provenance=baseline['size'],baseline['provenance']
            item = dict(path=name, source=str(resolved), category='REQUIRED', expected_sha256=expected, expected_size=size,
                        provenance=provenance or 'LEGACY_UNVERIFIED', references=[reason])
            old = files.get(name)
            if old:
                if old['expected_sha256'] and expected and old['expected_sha256'] != expected:
                    errors.append({'reason': 'CONFLICTING_BASELINE', 'path': name})
                if not expected: item = old
                item['references'] = list(dict.fromkeys(old['references'] + [reason]))
            files[name] = item

        media = rows(db, 'media')
        for row in media:
            add(row['file_path'], 'media:' + row['project_id'], row['checksum_sha256'],
                row['file_size_bytes'] or None, 'LEGACY_UNVERIFIED' if row['checksum_sha256'] else None, external=True)
        for row in rows(db, 'sources'):
            if row['platform'] == 'local_file':
                value = row['input_url']
                if value.startswith('file://'):
                    from urllib.parse import unquote, urlsplit
                    value = unquote(urlsplit(value).path).lstrip('/') if __import__('os').name == 'nt' else unquote(urlsplit(value).path)
                add(value, 'source:' + row['project_id'], external=True)
        records = {r['key']: json.loads(r['data']) for r in rows(db, 'server_records') if r['key'].startswith('upload:')}
        for row in rows(db, 'upload_sessions'):
            if row['status'] != 'COMPLETED': continue
            record = records.get('upload:' + row['upload_id'], {})
            add(record.get('path'), 'upload:' + row['upload_id'], row['expected_sha256'], row['total_size'], 'VERIFIED_AT_CREATION')
        attempts = {r['id']: r for r in rows(db, 'processing_attempts')}
        pointers = {(r['project_id'], r['kind']): r for r in rows(db, 'artifact_pointers')}
        for key, pointer in pointers.items():
            if not pointer['directory']:
                empty.append(dict(category='INTENTIONALLY_EMPTY', reference='invalidated_pointer:' + ':'.join(key)))
                continue
            attempt = attempts.get(pointer['attempt_id'])
            if not attempt or attempt['state'] != 'PUBLISHED' or attempt['project_id'] != key[0] or attempt['kind'] != key[1]:
                errors.append({'reason': 'INVALID_ARTIFACT_POINTER', 'reference': str(key)})
                continue
            manifest = json.loads(attempt['manifest'] or '{}').get('files', {})
            if pointer['directory']!=attempt['directory'].rstrip('/')+'/'+key[1]:
                errors.append({'reason':'POINTER_DIRECTORY_MISMATCH','reference':str(key)})
            if not manifest:
                errors.append({'reason': 'MISSING_ATTEMPT_MANIFEST', 'reference': str(key)})
            for name, spec in manifest.items():
                if not name.startswith(attempt['directory'].rstrip('/') + '/'):
                    errors.append({'reason': 'POINTER_MANIFEST_MISMATCH', 'path': name})
                    continue
                add(Path(projects_name)/key[0]/name, 'attempt:' + attempt['id'], spec['sha256'], spec['size'], spec['verification'])
        voiced = set()
        for row in rows(db, 'transcripts'):
            project = row['project_id']
            for index, cue in enumerate(json.loads(row['segments'] or '[]')):
                ref = f'transcript:{row["id"]}:cue:{cue.get("index", index)}'
                path = cue.get('voice_audio_path')
                text = cue.get('vi_text', '')
                if not str(text or '').strip() or cue.get('skip') or not path:
                    empty.append(dict(category='INTENTIONALLY_EMPTY', reference=ref, reason='empty_or_not_generated'))
                    continue
                voiced.add(project)
                add(Path(projects_name)/project/path if not Path(path).is_absolute() else path, ref)
        for row in rows(db, 'exports'):
            if (row['project_id'], 'output') not in pointers:
                add(row['export_path'], 'legacy_export:' + row['project_id'])
        for row in rows(db,'legacy_backup_references'):
            add(row['path'],row['reference'],row['sha256'],row['size'],'SNAPSHOT_METADATA' if row['sha256'] else None)
        for row in rows(db,'stage_reset_audits'):
            add(row['path'],'stage_reset:'+row['id'],row['sha256'],row['size'],'VERIFIED_AT_CREATION')
    return dict(required=sorted(files.values(), key=lambda r:r['path']), empty=empty, errors=errors)


def logical_tables(snapshot,table_names=None):
    with closing(readonly(snapshot)) as db:
        db.row_factory = sqlite3.Row
        allowed=('projects', 'sources', 'media', 'transcripts', 'jobs', 'artifact_pointers', 'processing_attempts', 'file_baselines', 'legacy_backup_references','stage_reset_audits','file_lifecycles','cleanup_batches','cleanup_candidates')
        if table_names is not None and not set(table_names).issubset(allowed):raise ValueError('Unsupported backup tables')
        return {table: sorted(rows(db, table), key=lambda row: json.dumps(row, sort_keys=True)) for table in allowed if table_names is None or table in table_names}
