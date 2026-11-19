"""Durable timing for project work. Workflow QA remains the completion authority."""
import json
import logging
import time
from pathlib import Path

from config.settings import settings
from src.pipeline.state_machine import load_pipeline_state
from src.translation.memory import atomic_json


ACTIONS = {'create', 'source', 'download', 'ocr', 'source-audio-review', 'translate',
           'voiceover', 'render', 'verify', 'remix', 'run-all', 'run_all'}
logger = logging.getLogger(__name__)
manual_verifications = {}


def recover_interrupted_tasks(root):
    """At startup, close stale running labels while preserving unknown duration."""
    for path in (root / 'project_timing').glob('task_*.json'):
        try:
            row = json.loads(path.read_text(encoding='utf-8'))
            if row['state'] == 'RUNNING' and row['finished_at'] is None:
                row.update(state='INTERRUPTED', error_code='SERVER_RESTART')
                atomic_json(path, row)
        except (OSError, ValueError, TypeError, KeyError):
            logger.exception('Cannot reconcile project timing for %s', path)


def save_task(task):
    if task.action not in ACTIONS:
        return
    root = settings.storage_path / task.project_id
    if not root.is_dir():
        return
    try:
        path = root / 'project_timing' / f'{task.task_id}.json'
        if task.timing_initial_stage is None:
            task.timing_initial_stage = load_pipeline_state(root).current_state.value
        record = dict(id=task.task_id, action=task.action, state=task.state,
                      queued_at=task.created_at, started_at=task.started_at,
                      finished_at=task.finished_at, error_code=task.error_code,
                      initial_stage=task.timing_initial_stage)
        if task.finished_at is not None and task.started_at is not None:
            workflow = load_pipeline_state(root)
            checkpoints = {key: value.completed_at for key, value in workflow.stages.items()
                           if value.status == 'COMPLETED' and value.completed_at is not None
                           and task.started_at <= value.completed_at <= task.finished_at}
            record['checkpoint_at'] = max(checkpoints.values(), default=None)
            if workflow.current_state.value == 'VERIFIED':
                record['verified_at'] = checkpoints.get('VERIFIED')
                render = workflow.stages.get('RENDERED')
                record['rendered_at'] = render.completed_at if render and render.status == 'COMPLETED' else None
        atomic_json(path, record)
    except (OSError, RuntimeError):
        logger.exception('Cannot save project timing for %s', task.project_id)


def summarize(root: Path, *, stage, live_task=None, now=None):
    now = time.time() if now is None else now
    records = {}
    partial = False
    for path in (root / 'project_timing').glob('task_*.json'):
        try:
            row = json.loads(path.read_text(encoding='utf-8'))
            records[row['id']] = row
        except (OSError, ValueError, TypeError, KeyError):
            partial = True
    live_id = getattr(live_task, 'task_id', None)
    if live_id in records and live_task.finished_at is None:
        records[live_id].update(started_at=live_task.started_at, state=live_task.state)
    if not records:
        return None
    rows = sorted(records.values(), key=lambda row: row['queued_at'])
    workflow = load_pipeline_state(root)
    qa = workflow.stages.get('VERIFIED')
    finished = qa.completed_at if stage == 'VERIFIED' and qa and qa.status == 'COMPLETED' else None
    render = workflow.stages.get('RENDERED')
    if finished is not None and render and render.completed_at is not None:
        finishes = [row['verified_at'] for row in rows if row.get('rendered_at') == render.completed_at
                    and row.get('verified_at') is not None]
        finished = min([finished, *finishes])
    if finished is not None:
        rows = [row for row in rows if row['queued_at'] <= finished]
    if not rows:
        return None
    previous = max((row['verified_at'] for row in rows if row.get('verified_at') is not None
                    and row['verified_at'] < (finished if finished is not None else now)
                    and (finished is not None or row['verified_at'] < rows[-1]['queued_at'])), default=None)
    if previous is not None:
        rows = [row for row in rows if row['queued_at'] >= previous]
    if not rows:
        return None
    if previous is None and rows[0].get('initial_stage') not in {None, 'CREATED', 'SOURCE_READY'}:
        partial = True
    start = rows[0]['queued_at']
    end = max(start, finished if finished is not None else now)
    ranges = []
    for index, row in enumerate(rows):
        begin, stop = row.get('started_at'), row.get('finished_at')
        live = row['id'] == live_id and row['state'] == 'RUNNING'
        if stop is None and not live:
            if begin is not None or row['id'] != live_id:
                partial = True
                if begin is not None:
                    next_start = rows[index+1]['queued_at'] if index+1 < len(rows) else end
                    ranges.append((begin, min(end, next_start), 'unknown'))
            continue
        if begin is None:
            continue
        stop = min(end, stop if stop is not None else now)
        if row['state'] in {'DONE', 'RUNNING'} or row.get('error_code') == 'OCR_REVIEW_REQUIRED':
            ranges.append((begin, stop, 'processing'))
        else:
            checkpoint = min(stop, max(begin, row.get('checkpoint_at') or begin))
            remainder = 'failed' if row['state'] == 'FAILED' else 'waiting'
            ranges.extend(((begin, checkpoint, 'processing'), (checkpoint, stop, remainder)))
    # A boundary sweep counts overlapping execution once, favoring retained work.
    boundaries = sorted({start, end} | {point for a, b, _ in ranges for point in (a, b) if start < point < end})
    totals = dict(processing=0.0, waiting=0.0, failed=0.0, unknown=0.0)
    for left, right in zip(boundaries, boundaries[1:]):
        covering = {kind for a, b, kind in ranges if a <= left and b >= right}
        kind = next((kind for kind in ('processing', 'failed', 'unknown') if kind in covering), 'waiting')
        totals[kind] += right - left
    return dict(started_at=start, finished_at=finished, total_seconds=end-start,
                processing_seconds=totals['processing'], waiting_seconds=totals['waiting'],
                failed_seconds=totals['failed'], unknown_seconds=totals['unknown'], partial=partial)
