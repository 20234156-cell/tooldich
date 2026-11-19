"""Measure the local OCR recognizer on the published GeekLink subtitle labels."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sys
import time
import unicodedata

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def canonical(text):
    return ''.join(unicodedata.normalize('NFC', text).split())


def distance(left, right):
    previous = list(range(len(right) + 1))
    for index, char in enumerate(left, 1):
        current = [index]
        for column, other in enumerate(right, 1):
            current.append(min(previous[column] + 1, current[-1] + 1,
                               previous[column - 1] + (char != other)))
        previous = current
    return previous[-1]


def summarize(rows):
    answered = [row for row in rows if not row['pending']]
    return dict(samples=len(rows), exact=sum(row['edits'] == 0 for row in rows),
                pending=len(rows) - len(answered), blank=sum(not row['prediction'] for row in rows),
                wrong_without_flag=sum(row['edits'] > 0 for row in answered),
                cer=sum(row['edits'] for row in rows) / sum(row['characters'] for row in rows),
                seconds=sum(row['seconds'] for row in rows))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dataset', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--language', default='zh')
    args = parser.parse_args()
    from src.ocr.rapidocr_engine import RapidOCREngine
    import cv2

    dataset = args.dataset.resolve()
    manifest = (dataset / 'data/manifest.jsonl').read_bytes()
    records = [json.loads(line) for line in manifest.splitlines() if line.strip()]
    samples = [row for row in records if row['lang'] == args.language]
    if not samples or len({row['id'] for row in samples}) != len(samples):
        raise ValueError('Missing samples or duplicate IDs.')
    engine = RapidOCREngine()
    rows = []
    for sample in samples:
        image_path = (dataset / sample['image']).resolve()
        if not image_path.is_relative_to(dataset):
            raise ValueError('Image path outside dataset.')
        frame = cv2.imread(str(image_path))
        if frame is None:
            raise ValueError(f'Cannot decode {image_path}.')
        expected = canonical(sample['ground_truth'])
        if not expected:
            raise ValueError('Empty ground-truth label.')
        for variant, image in [('full_frame', frame),
                               ('bottom_30_percent_diagnostic', frame[int(.7 * frame.shape[0]):])]:
            started = time.perf_counter()
            text, confidence = engine.recognize(image)
            elapsed = time.perf_counter() - started
            rows.append(dict(id=sample['id'], source_video=sample['video_id'],
                             overlay=str(sample['has_watermark']).lower() == 'true',
                             variant=variant, expected=sample['ground_truth'], prediction=text,
                             confidence=confidence, pending=not text or confidence < 70
                             or bool(engine.last_alternatives),
                             flags=[item.get('variant') for item in engine.last_alternatives],
                             edits=distance(canonical(text), expected), characters=len(expected),
                             seconds=elapsed))
    groups = defaultdict(list)
    for row in rows:
        groups[row['variant']].append(row)
        groups[f"{row['variant']}/overlay={row['overlay']}"].append(row)
        groups[f"{row['variant']}/{row['source_video']}"].append(row)
    report = dict(manifest_sha256=hashlib.sha256(manifest).hexdigest(), engine=engine.cache_version,
                  label_origin='publisher_rendered_benchmark', samples=len(samples),
                  catalog_total=len(records), language=args.language,
                  source_groups=sorted({row['video_id'] for row in samples}),
                  summary={key:summarize(items) for key, items in sorted(groups.items())},
                  scope='Single-frame recognizer. Pending approximates cue gate before temporal recovery.',
                  rows=rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps(report['summary'], ensure_ascii=False))


if __name__ == '__main__':
    main()
