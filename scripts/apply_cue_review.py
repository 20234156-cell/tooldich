"""Apply an explicit evidence-backed review plan through guarded Studio APIs."""
import argparse
import json
import sys
import time
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
from config.settings import settings


def patch_review(session, url, patch):
    """Retry only explicit local throttling, never ambiguous network failures."""
    deadline = time.monotonic()+90
    for attempt in range(5):
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Review PATCH exceeded throttle wait budget')
        response = session.patch(url, json=patch, timeout=min(30, remaining))
        if response.status_code != 429 or attempt == 4:
            response.raise_for_status()
            return response
        try:
            delay = max(1, float(response.headers.get('Retry-After', '1')))
        except ValueError:
            delay = 1
        response.close()
        remaining = deadline-time.monotonic()
        if delay >= remaining:
            raise TimeoutError('Review throttle retry exceeds remaining wait budget')
        time.sleep(min(delay, 60))


def validate_review_texts(plan):
    """Reject visible encoding loss before opening an API session.

    A question mark inside a word is a common Windows ASCII-pipe artifact.
    Sentence-final questions remain valid; this is a preflight, not a semantic
    translation check, and cannot detect every form of encoding damage.
    """
    for index, patch in plan['patches'].items():
        target = patch.get('vi_text')
        if not isinstance(target, str) or not target.strip():
            raise ValueError(f'Cue {index}: plan requires nonempty reviewed vi_text before any writes')
        for field in ('vi_text', 'corrected_text'):
            value = patch.get(field)
            if not isinstance(value, str):
                continue
            if '\ufffd' in value or re.search(r'[^\W\d_]\?[^\W\d_]', value):
                raise ValueError(f'Cue {index} {field}: suspected encoding loss; review UTF-8 input')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding='utf-8'))
    validate_review_texts(plan)
    project = plan['project_id']
    patches = plan['patches']
    if not args.apply:
        print(json.dumps({'project_id': project, 'cue_ids': list(patches)}))
        return
    with requests.Session() as session:
        if settings.API_TOKEN:
            session.headers['X-API-Token'] = settings.API_TOKEN
        root = 'http://127.0.0.1:8088/api/projects/'+project
        before = session.get(root+'/cues', timeout=20)
        before.raise_for_status()
        cues = before.json()
        if len(cues) != plan['expected_cue_count']:
            raise RuntimeError('Cue count changed; review the plan again')
        if not set(map(int, patches)).issubset(c['index'] for c in cues):
            raise RuntimeError('Reviewed cue IDs are missing')
        report = args.plan.with_name(args.plan.stem+f'.applied.{time.time_ns()}.json')
        record = {'plan':plan, 'before':cues, 'applied_ids':[]}
        by_id = {cue['index']: cue for cue in cues}
        record['already_matching_ids'] = []
        report.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
        for index, payload in patches.items():
            if all(by_id[int(index)].get(key) == value for key,value in payload.items()):
                record['already_matching_ids'].append(int(index))
                continue
            source = {key:value for key,value in payload.items() if key != 'vi_text'}
            # Source changes deliberately clear translation. Submit reviewed
            # target text afterwards; every write goes through the busy guard.
            for patch in [source, {'vi_text':payload['vi_text']}]:
                if not patch:
                    continue
                patch_review(session, root+'/cues/'+str(index), patch)
            record['applied_ids'].append(int(index))
            report.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
        after = session.get(root+'/cues', timeout=20)
        after.raise_for_status()
        record['after'] = after.json()
        report.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'report':str(report),'applied':len(record['applied_ids'])}))


if __name__ == '__main__':
    main()
