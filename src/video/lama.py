"""Restore source captions before burning translated subtitles."""
import json
import re

import cv2
import numpy as np


def expanded_box(boxes, width, height, pad=16):
    """Include dark outlines/shadows even when OCR only bounds bright strokes."""
    points = np.asarray(boxes).reshape(-1, 2)
    return [max(0, int(points[:, 0].min()) - pad),
            max(0, int(points[:, 1].min()) - pad),
            min(width, int(points[:, 0].max()) + pad),
            min(height, int(points[:, 1].max()) + pad)]


def active_box(rows, stamp, padding=.08):
    """Union overlapping cue windows so switching cues cannot drop edge frames."""
    boxes = [r['box'] for r in rows if r['box'] and
             r['start'] - padding <= stamp <= r['end'] + padding]
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def read_scan_frame(cap, stamp):
    """Advance over nearby frames without restarting the decoder."""
    fps = cap.get(cv2.CAP_PROP_FPS)
    position = cap.get(cv2.CAP_PROP_POS_FRAMES)
    target = round(stamp * fps)
    if fps > 0 and 0 <= target - position <= fps:
        for _ in range(target - int(position)):
            if not cap.grab():
                break
        else:
            ok, frame = cap.read()
            if ok and abs(cap.get(cv2.CAP_PROP_POS_MSEC) / 1000 - stamp) <= .5 / fps:
                return ok, frame
    cap.set(cv2.CAP_PROP_POS_MSEC, stamp * 1000)
    return cap.read()


def scan(source, cues, band, output=None):
    from rapidocr_onnxruntime import RapidOCR
    from src.ocr.gpu_client import GPUOCREngine, OCRGPUError, validated_runtime

    runtime = validated_runtime()
    gpu = GPUOCREngine(runtime) if runtime else None
    reader = None

    def recognize(crop):
        nonlocal gpu, reader
        if gpu is not None:
            try:
                return [(item['box'], item['text'], item['confidence'] / 100)
                        for item in gpu.read_boxes(crop)]
            except (OCRGPUError, OSError, ValueError, TimeoutError) as exc:
                print(f"OCR CUDA fallback: {type(exc).__name__}", flush=True)
                gpu.close()
                gpu = None
        if reader is None:
            reader = RapidOCR(intra_op_num_threads=4, inter_op_num_threads=1)
        return reader(crop)[0]

    try:
        return scan_frames(source, cues, band, recognize, output)
    finally:
        if gpu is not None:
            gpu.close()


def scan_frames(source, cues, band, recognize, output):
    cap = cv2.VideoCapture(str(source))
    width, height = int(cap.get(3)), int(cap.get(4))
    rows = []
    for i, cue in enumerate(cues):
        boxes = []
        for fraction in (.2, .5, .8):
            stamp = cue['start'] + (cue['end'] - cue['start']) * fraction
            ok, frame = read_scan_frame(cap, stamp)
            if not ok:
                raise RuntimeError(f'Cannot decode source at {stamp}')
            results = recognize(frame[band[0]:band[1]])
            for box, text, score in results or []:
                if score < .7 or not re.search('[\u4e00-\u9fff]', text):
                    continue
                box = np.asarray(box)
                box[:, 1] += band[0]
                boxes.append(box)
        rows.append({'start': cue['start'], 'end': cue['end'],
                     'index': cue['index'],
                     'box': expanded_box(boxes, width, height) if boxes else None})
        if i % 10 == 0 or i + 1 == len(cues):
            print(f'Scan {i + 1}/{len(cues)}', flush=True)
    cap.release()
    if output is not None:
        output.write_text(json.dumps(rows, indent=2), encoding='utf-8')
    return rows


class Restorer:
    def __init__(self, model_path):
        import torch
        self.torch = torch
        torch.set_num_threads(4)
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        self.model = torch.jit.load(str(model_path), map_location=self.device).eval()

    def apply(self, frame, box):
        torch = self.torch
        x1, y1, x2, y2 = box
        h, w = frame.shape[:2]
        # Context includes the table/clothing/background, never the burned-in VI.
        left, right = max(0, x1 - 128), min(w, x2 + 128)
        top, bottom = max(0, y1 - 160), min(h, y2 + 48)
        patch = frame[top:bottom, left:right]
        mask = np.zeros(patch.shape[:2], np.uint8)
        mask[y1-top:y2-top, x1-left:x2-left] = 255
        scale = min(1., 960 / patch.shape[1])
        pw, ph = (int(np.ceil(v * scale / 8)) * 8 for v in (patch.shape[1], patch.shape[0]))
        small = cv2.resize(patch, (pw, ph))
        smask = cv2.resize(mask, (pw, ph), interpolation=cv2.INTER_NEAREST)
        image = torch.from_numpy(small[:, :, ::-1].copy().transpose(2, 0, 1)).float()[None].to(self.device) / 255
        tensor_mask = torch.from_numpy(smask.copy()).float()[None, None].to(self.device) / 255
        with torch.inference_mode():
            result = self.model(image, tensor_mask)
        result = (result[0].permute(1, 2, 0).cpu().numpy() * 255).clip(0, 255).astype(np.uint8)[:, :, ::-1]
        result = cv2.resize(result, (patch.shape[1], patch.shape[0]))
        # Replace only the explicit mask; outside pixels are the original source.
        patch[mask > 0] = result[mask > 0]
        return frame


class TemporalRestorer:
    """LaMa anchors with short motion propagation, resetting at scene/mask changes.

    This is not a jointly trained video inpainting network. At most two frames
    use propagated pixels before a fresh anchor, avoiding long-range drift.
    """
    def __init__(self, restorer, stride=3):
        self.restorer, self.stride = restorer, stride
        self.previous = None
        self.cleaned = None
        self.box = None
        self.age = 0

    def apply(self, frame, box):
        top = max(0, box[1] - 128)
        source = frame[top:].copy()
        small = cv2.resize(cv2.cvtColor(source, cv2.COLOR_BGR2GRAY), None, fx=.5, fy=.5)
        changed = self.box != box or self.previous is None or self.previous.shape != small.shape
        difference = 255 if changed else float(np.mean(cv2.absdiff(small, self.previous)))
        if changed or self.age >= self.stride - 1 or difference > 18:
            frame = self.restorer.apply(frame, box)
            self.age = 0
        else:
            # Backward flow maps current destination pixels into the preceding
            # clean frame. Extend surrounding motion through the text region;
            # glyph motion itself is not evidence of background motion.
            flow = cv2.calcOpticalFlowFarneback(small, self.previous, None, .5, 3, 21, 3, 5, 1.2, 0)
            x1, y1, x2, y2 = box
            mask = np.zeros(small.shape, np.uint8)
            mask[max(0,(y1-top)//2-2):min(mask.shape[0],(y2-top+1)//2+2),
                 max(0,x1//2-2):min(mask.shape[1],(x2+1)//2+2)] = 255
            for channel in range(2):
                flow[:,:,channel] = cv2.inpaint(flow[:,:,channel].copy(), mask, 3, cv2.INPAINT_NS)
            flow = cv2.resize(flow, (source.shape[1], source.shape[0])) * 2
            # The output only consumes pixels inside the caption rectangle.
            dy = y1 - top
            ey = y2 - top
            region_flow = flow[dy:ey, x1:x2]
            xx = np.arange(x1, x2, dtype=np.float32)[None, :]
            yy = np.arange(dy, ey, dtype=np.float32)[:, None]
            frame[y1:y2, x1:x2] = cv2.remap(
                self.cleaned, xx + region_flow[:, :, 0], yy + region_flow[:, :, 1],
                cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
            self.age += 1
        self.previous, self.cleaned, self.box = small, frame[top:].copy(), list(box)
        return frame


