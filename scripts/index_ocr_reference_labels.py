"""Index upstream text references separately from image-backed OCR samples."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import unicodedata


def references(path):
    rows = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        match = re.fullmatch(r'(\d+) \[(.*)\]', line)
        if not match:
            raise ValueError(f'Malformed prediction row in {path.name}')
        index, fields = match.groups()
        if index in rows:
            raise ValueError(f'Duplicate index in {path.name}')
        # The publisher documents [prediction] [ground truth]. An ambiguous
        # delimiter inside either string cannot safely be interpreted.
        parts = fields.split('] [')
        rows[index] = parts[1] if len(parts) == 2 and parts[1].strip() else None
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('fudan_repo', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    predictions = args.fudan_repo / 'predictions'
    sections = ('scene', 'web', 'document', 'handwriting')
    args.output.mkdir(parents=True, exist_ok=True)
    summary = {}
    texts = set()
    output = args.output / 'fudan_text_reference.jsonl'
    with output.open('x', encoding='utf-8') as stream:
        for section in sections:
            first = predictions / 'TransOCR' / f'TransOCR_{section}.txt'
            second = predictions / 'CRNN' / f'CRNN_{section}.txt'
            left, right = references(first), references(second)
            counts = Counter()
            for index, text in left.items():
                if not text or right.get(index) != text:
                    counts['skipped_unverified'] += 1
                    continue
                text = unicodedata.normalize('NFC', text)
                if text in texts:
                    counts['duplicates'] += 1
                texts.add(text)
                row = dict(source='FudanVI/benchmarking-chinese-text-recognition',
                           upstream_id=f'{section}:{index}', text=text, domain=section,
                           origin='published_ground_truth_column', split='upstream_test',
                           evidence='text_only', subtitle_role='unknown',
                           training_eligible=False)
                stream.write(json.dumps(row, ensure_ascii=False) + '\n')
                counts['indexed'] += 1
            summary[section] = dict(upstream_rows=len(left), **counts,
                                    source_sha256=[hashlib.sha256(p.read_bytes()).hexdigest()
                                                   for p in (first, second)])
    report = dict(sections=summary, unique_texts=len(texts),
                  indexed_rows=sum(row.get('indexed', 0) for row in summary.values()),
                  image_pairs=0, training_eligible=False)
    with (args.output / 'fudan_index_summary.json').open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
