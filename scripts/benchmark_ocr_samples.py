"""Compare ONNX thread counts on the same already-extracted subtitle images."""
import argparse
import gc
import json
import time
from pathlib import Path

import cv2
from rapidocr_onnxruntime import RapidOCR


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('images', type=Path)
    parser.add_argument('--count', type=int, default=12)
    parser.add_argument('--output', type=Path, default=Path('storage/ui_audit/ocr_threads.json'))
    args = parser.parse_args()
    paths = sorted(args.images.glob('*.jpeg'))
    paths = [paths[int(i * (len(paths)-1) / max(1, args.count-1))] for i in range(min(args.count, len(paths)))]
    images = [cv2.imread(str(path)) for path in paths]
    report = []
    for threads in [1, 2, 4]:
        engine = RapidOCR(det_limit_side_len=960, det_limit_type='max', intra_op_num_threads=threads, inter_op_num_threads=1)
        engine(images[0])
        start = time.perf_counter()
        texts = []
        for image in images:
            result, _ = engine(image)
            texts.append(''.join(row[1] for row in (result or [])))
        result = {'threads': threads, 'seconds': round(time.perf_counter()-start, 3), 'texts': texts}
        report.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        del engine
        gc.collect()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'images': [str(p) for p in paths], 'results': report}, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
