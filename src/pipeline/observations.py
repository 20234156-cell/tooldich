"""Small stage evidence index. No source decisions, auto-training or media copies.

Called inside the existing project-status transaction. An identical stage attempt
is recorded once; a new attempt or changed finding is a distinct observation.
"""
import hashlib
import json
import math
import time
from collections import Counter

from src.database.models import PipelineObservation
from src.jobs.logger import mask_secrets


def capture(db, project_id, state):
    for name, stage in state.stages.items():
        if not (stage.errors or stage.warnings or stage.degraded or stage.status == 'FAILED'):
            continue
        # Progress, elapsed time and the global state timestamp are not findings.
        # No exception strings, provider settings or source URLs are added here.
        payload = {
            'schema': 1, 'status': stage.status,
            'attempt_started_at': stage.started_at,
            'input_hash': stage.input_hash, 'engine_version': stage.engine_version,
            'warnings': list(dict.fromkeys(mask_secrets(x) for x in stage.warnings)),
            'errors': list(dict.fromkeys(mask_secrets(x) for x in stage.errors)),
            'degraded': list(dict.fromkeys(stage.degraded)),
            'artifact_references': dict(stage.artifacts),
            'assessment': 'unverified_observation',
            'root_cause': None,
            'evidence_retention': 'references_only_not_media_snapshot',
        }
        identity = json.dumps([project_id, name, payload], sort_keys=True,
                              ensure_ascii=False, allow_nan=False)
        key = hashlib.sha256(identity.encode('utf-8')).hexdigest()
        if db.get(PipelineObservation, key) is None:
            db.add(PipelineObservation(id=key, project_id=project_id, stage=name,
                                       created_at=time.time(), payload=payload))
            # The caller uses autoflush=False; make repeated captures in this
            # transaction see the new key without committing project edits.
            db.flush()


def summarize(db, since=0.0, until=None):
    """Half-open UTC timestamp window; counts observations, never error rates."""
    # Include records written in the same coarse Windows clock tick. Explicit
    # caller-supplied boundaries remain strictly half-open for daily exports.
    until = math.nextafter(time.time(), math.inf) if until is None else until
    if until < since:
        raise ValueError('until must be >= since')
    rows = db.query(PipelineObservation).filter(
        PipelineObservation.created_at >= since,
        PipelineObservation.created_at < until,
    ).order_by(PipelineObservation.created_at, PipelineObservation.id).all()
    grouped = {}
    for row in rows:
        # Only explicit policy codes are grouped. Similar warning text is not
        # enough to infer a shared cause. Unknown groups retain the stage.
        codes = row.payload['degraded'] or ['unclassified']
        for code in codes:
            key = (row.stage, code)
            group = grouped.setdefault(key, {'projects': set(), 'observations': 0})
            group['projects'].add(row.project_id)
            group['observations'] += 1
    return {
        'schema': 1, 'since_utc_epoch': since, 'until_utc_epoch': until,
        'observation_count': len(rows),
        'affected_projects': len({row.project_id for row in rows}),
        'status_counts': dict(Counter(row.payload['status'] for row in rows)),
        'groups': [{'stage': stage, 'signal': code,
                    'observations': value['observations'],
                    'affected_projects': len(value['projects'])}
                   for (stage, code), value in sorted(grouped.items())],
        'observations': [{'id': row.id, 'project_id': row.project_id,
                          'stage': row.stage, 'created_at': row.created_at,
                          **row.payload} for row in rows],
        'limits': ['Not a count of confirmed bugs or unique cues.',
                   'No production/cue/minute denominator; no error rate inferred.',
                   'Artifact references can expire; no media retention guarantee.',
                   'Only states saved by the orchestrator are captured; not all exceptions.',
                   'Old projects are not backfilled. No automatic training labels.'],
    }
