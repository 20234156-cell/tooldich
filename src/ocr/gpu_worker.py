"""JSON-line OCR worker; CUDA packages and DLLs stay outside the API process."""
import base64
import contextlib
import json
import os
from pathlib import Path
import sys


def _decode_image(payload):
    import cv2
    import numpy as np
    if not isinstance(payload, dict):
        raise ValueError('Invalid OCR image payload')
    if 'path' in payload:
        image_path = payload.get('path')
        if not isinstance(image_path, str) or not image_path or not Path(image_path).is_file():
            raise ValueError('Invalid OCR image path')
        image = cv2.imread(image_path)
    elif 'raw_b64' in payload:
        raw_b64 = payload.get('raw_b64')
        shape = payload.get('shape')
        dtype = payload.get('dtype', 'uint8')
        if (not isinstance(raw_b64, str) or len(raw_b64) > 120_000_000
                or not isinstance(shape, list) or len(shape) not in (2, 3)
                or not all(isinstance(dim, int) and dim > 0 for dim in shape)
                or dtype != 'uint8'):
            raise ValueError('Invalid OCR raw image encoding')
        raw = base64.b64decode(raw_b64, validate=True)
        expected_len = 1
        for dim in shape:
            expected_len *= dim
        if len(raw) != expected_len:
            raise ValueError('Mismatched OCR raw image length')
        image = np.frombuffer(raw, dtype=np.uint8).reshape(tuple(shape)).copy()
    else:
        png = payload.get('png')
        if not isinstance(png, str) or len(png) > 90_000_000:
            raise ValueError('Invalid OCR image encoding')
        image = cv2.imdecode(np.frombuffer(base64.b64decode(png, validate=True), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None or not image.size:
        raise ValueError('Invalid OCR image')
    return image


def handle_request(engine, request):
    """One bounded request; supports single recognize/boxes and recognize_batch."""
    if not isinstance(request, dict):
        raise ValueError('Invalid OCR request')
    operation = request.get('operation', 'recognize')
    if operation not in ('recognize', 'boxes', 'recognize_batch'):
        raise ValueError('Unknown OCR operation')
    if operation == 'recognize_batch':
        items = request.get('items')
        if not isinstance(items, list) or not (1 <= len(items) <= 32):
            raise ValueError('Invalid OCR batch items')
        results = []
        with contextlib.redirect_stdout(sys.stderr):
            for item in items:
                image = _decode_image(item)
                text, confidence = engine.recognize(image)
                results.append({'text': text, 'confidence': confidence, 'alternatives': engine.last_alternatives})
        return {'ok': True, 'results': results, 'device': 'cuda'}
    if operation == 'boxes' and not isinstance(request.get('preprocess', False), bool):
        raise ValueError('Invalid preprocessing flag')
    image = _decode_image(request)
    with contextlib.redirect_stdout(sys.stderr):
        if operation == 'boxes':
            boxes = engine.read_boxes(image, preprocess=request.get('preprocess', False))
            return {'ok': True, 'boxes': boxes, 'device': 'cuda'}
        text, confidence = engine.recognize(image)
        return {'ok': True, 'text': text, 'confidence': confidence, 'alternatives': engine.last_alternatives, 'device': 'cuda'}


def main():
    base = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(base))
    from src.security.offline import install_offline_guard
    install_offline_guard()
    import cv2
    import numpy as np
    with contextlib.redirect_stdout(sys.stderr):
        import onnxruntime as ort
        ort.preload_dlls(directory=str(base / 'tools/vieneu-gpu/Lib/site-packages/torch/lib'))
        from src.ocr.rapidocr_engine import RapidOCREngine
        engine = RapidOCREngine(use_cuda=True)
        engine._get_engine()
    for line in sys.stdin:
        try:
            request = json.loads(line)
            response = handle_request(engine, request)
        except Exception as exc:
            response = {'ok': False, 'error': type(exc).__name__}
        print(json.dumps(response, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
