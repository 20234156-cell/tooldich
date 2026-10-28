"""Project-scoped translation memory; positions and batch boundaries are not identities."""
import hashlib
import json
import os
import unicodedata
import uuid


def normalized(text):
    return unicodedata.normalize('NFC', text).strip()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / ('.tmp-' + uuid.uuid4().hex[:16])
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class TranslationMemory:
    version = 'speech-budget-context-v6'

    def __init__(self, directory, cues, tone, glossary, models, max_tokens):
        self.directory = directory / 'translation_memory' if directory else None
        self.entries = {}
        sources = [normalized(c.effective_source_text) for c in cues]
        policy = {'engine': self.version, 'tone': tone, 'glossary': glossary,
                  'models': models, 'max_output_tokens': max_tokens, 'target': 'vi'}
        for position, cue in enumerate(cues):
            from src.translation.preflight import soft_budget, structure_findings
            identity = {**policy, 'source': sources[position], 'duration': round(cue.duration, 3),
                        'budget': soft_budget(cue),
                        'structure': [f['reasons'] for f in structure_findings(cues[max(0, position-1):position+1])],
                        'before': sources[max(0, position - 4):position], 'after': sources[position + 1:position + 3]}
            self.entries[cue.index] = fingerprint(identity)
        self.models = set(models)
        self.completed = {}

    def read(self, cue_id, parser):
        identity = self.entries[cue_id]
        data = self.completed.get(identity)
        if data is None and self.directory:
            try:
                data = json.loads((self.directory / (identity + '.json')).read_text(encoding='utf-8'))
            except (OSError, ValueError, TypeError):
                return None
        if not isinstance(data, dict) or data.get('identity') != identity or data.get('engine') != self.version:
            return None
        if data.get('model') not in self.models or not isinstance(data.get('model_version'), str) or not data['model_version']:
            return None
        try:
            row = dict(data['translation'], id=cue_id)
            parsed = parser(json.dumps([row], ensure_ascii=False), {cue_id})[cue_id]
        except (TypeError, KeyError, ValueError):
            return None
        return parsed, data['model'], data['model_version']

    def write(self, cue_id, row, model, version):
        identity = self.entries[cue_id]
        value = {'identity': identity, 'engine': self.version, 'model': model, 'model_version': version,
                 'translation': {k: v for k, v in row.items() if k != 'id'}}
        self.completed[identity] = value
        if self.directory:
            atomic_json(self.directory / (identity + '.json'), value)
