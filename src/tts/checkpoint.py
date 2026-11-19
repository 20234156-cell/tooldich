"""Only completed, input-bound voice artifacts may skip synthesis on resume."""
import hashlib
import json
from pathlib import Path

import soundfile as sf


def _digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _paths(cues, root=None):
    if not cues or any(not c.voice_audio_path for c in cues):
        raise ValueError('Missing voice cue paths')
    paths={'voiceover/voiceover.wav', 'voiceover/timeline.json', *(c.voice_audio_path for c in cues)}
    if root is not None:
        from src.pipeline.attempts import artifact_path
        timeline=artifact_path(root,'voiceover/timeline.json')
        if timeline.is_file():
            rows = json.loads(timeline.read_text(encoding='utf8'))
            if any(row.get('schedule_policy')=='cluster-budget-v1' for row in rows):
                paths.add('voiceover/budget_report.json')
            elif any(row.get('schedule_policy')=='bounded-mix-qa-v1' for row in rows):
                if artifact_path(root, 'voiceover/budget_report.json').is_file():
                    paths.add('voiceover/budget_report.json')
                if artifact_path(root, 'voiceover/bounded_schedule.json').is_file():
                    paths.add('voiceover/bounded_schedule.json')
    return sorted(paths)


def _path(root, name):
    from src.pipeline.attempts import artifact_path
    path = artifact_path(root,name)
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Voice artifact outside project')
    return path


def create_voice_manifest(root, cues, input_hash):
    files = {}
    for name in _paths(cues, root):
        path = _path(root, name)
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError('Missing voice artifact')
        if name.endswith('.wav') and sf.info(str(path)).frames <= 0:
            raise ValueError('Empty voice artifact')
        files[name] = _digest(path)
    return {'version':1, 'input_hash':input_hash, 'cue_ids':sorted(c.index for c in cues), 'files':files}


def voice_checkpoint_matches(root, cues, input_hash, *, strategy=None):
    try:
        from src.pipeline.attempts import artifact_path
        record = json.loads(artifact_path(root,'voiceover/manifest.json').read_text(encoding='utf-8'))
        if not isinstance(record, dict) or not isinstance(record.get('files'), dict):
            return False
        if record.get('version') != 1 or record.get('input_hash') != input_hash or record.get('cue_ids') != sorted(c.index for c in cues):
            return False
        if strategy is not None and record.get('strategy')!=strategy:
            return False
        if set(record['files']) != set(_paths(cues, root)):
            return False
        return all(_path(root, name).is_file() and _digest(_path(root, name)) == expected for name, expected in record['files'].items())
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return False


def budget_checkpoint_matches(root, source):
    from src.pipeline.attempts import artifact_path

    try:
        path = artifact_path(root, 'voiceover/budget_report.json')
        record = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(record, dict) or record.get('version') != 'cluster-budget-v1':
            return False
        evidence = root / 'source/timing_evidence.json'
        protected = root / 'source/protected_audio.json'
        return (record.get('source_sha256') == _digest(source)
                and record.get('timing_evidence_sha256') == (_digest(evidence) if evidence.is_file() else None)
                and record.get('protected_audio_sha256') == (_digest(protected) if protected.is_file() else None))
    except (OSError, ValueError):
        return False
