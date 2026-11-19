"""Produce a temporary restored video or compact 1-pass ROI patch for FFmpeg."""
import gc
import json
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

from src.video.lama import Restorer, TemporalRestorer, active_box, scan


def roi_union_bounds(
    rows: List[dict], width: int, height: int, pad: int = 16
) -> Optional[Tuple[int, int, int, int]]:
    """Compute minimal even-aligned (x, y, w, h) ROI union across active caption boxes."""
    boxes = [r["box"] for r in rows if r.get("box")]
    if not boxes or width < 16 or height < 16:
        return None
    x0 = max(0, min(int(b[0]) for b in boxes) - pad)
    y0 = max(0, min(int(b[1]) for b in boxes) - pad)
    x1 = min(width, max(int(b[2]) for b in boxes) + pad)
    y1 = min(height, max(int(b[3]) for b in boxes) + pad)
    x0 -= x0 % 2
    y0 -= y0 % 2
    cw = min(width - x0, max(16, ((x1 - x0 + 1) // 2) * 2))
    ch = min(height - y0, max(16, ((y1 - y0 + 1) // 2) * 2))
    cw -= cw % 2
    ch -= ch % 2
    if cw < 16 or ch < 16:
        return None
    return x0, y0, cw, ch


def active_intervals(rows: List[dict], padding: float = 0.08) -> List[List[float]]:
    """Merge active caption windows for FFmpeg overlay enable expressions."""
    spans = sorted(
        (max(0.0, float(r["start"]) - padding), float(r["end"]) + padding)
        for r in rows
        if r.get("box") and float(r["end"]) > float(r["start"])
    )
    merged: List[List[float]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1] + 0.05:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([round(start, 3), round(end, 3)])
    return merged


def _release_vram(restorer_obj) -> None:
    try:
        if restorer_obj is not None:
            restorer_obj.previous = None
            base = getattr(restorer_obj, "restorer", None)
            if base is not None and hasattr(base, "model"):
                base.model = None
    except Exception:
        pass
    gc.collect()
    torch_mod = sys.modules.get("torch")
    if torch_mod is not None and hasattr(torch_mod, "cuda") and torch_mod.cuda.is_available():
        try:
            torch_mod.cuda.synchronize()
            torch_mod.cuda.empty_cache()
        except Exception:
            pass


def main(source, output, manifest, model, ffmpeg):
    data = json.loads(Path(manifest).read_text(encoding="utf-8"))
    rows = scan(source, data["cues"], data["band"])
    if not any(row["box"] for row in rows):
        print("NO_CAPTIONS", flush=True)
        return
    roi_only = bool(data.get("roi_only"))
    roi_manifest_path = Path(data["roi_manifest"]) if data.get("roi_manifest") else None
    restore = TemporalRestorer(Restorer(model))
    cap = cv2.VideoCapture(source)
    fps, width, height, count = [cap.get(prop) for prop in (
        cv2.CAP_PROP_FPS, cv2.CAP_PROP_FRAME_WIDTH, cv2.CAP_PROP_FRAME_HEIGHT,
        cv2.CAP_PROP_FRAME_COUNT)]
    if fps <= 0 or count <= 0:
        cap.release()
        _release_vram(restore)
        raise ValueError("Cannot read source frame rate or frame count")

    frame_w, frame_h = int(width), int(height)
    bounds = roi_union_bounds(rows, frame_w, frame_h) if roi_only else None
    if roi_only and bounds is None:
        cap.release()
        _release_vram(restore)
        print("NO_CAPTIONS", flush=True)
        return

    if roi_only and bounds is not None:
        rx, ry, rw, rh = bounds
        command = [
            ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{rw}x{rh}", "-r", str(fps), "-i", "-", "-an",
            "-c:v", "libx264", "-threads:v", "2", "-preset", "ultrafast",
            "-crf", "10", "-pix_fmt", "yuv420p", output,
        ]
    else:
        rx, ry, rw, rh = 0, 0, frame_w, frame_h
        command = [ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                   "-s", f"{frame_w}x{frame_h}", "-r", str(fps), "-i", "-",
                   "-i", source, "-map", "0:v", "-map", "1:a:0?", "-c:a", "copy",
                   "-c:v", "libx264", "-threads:v", "2", "-preset", "ultrafast",
                   "-crf", "16", "-pix_fmt", "yuv420p", output]

    with subprocess.Popen(command, stdin=subprocess.PIPE) as proc:
        try:
            for index in range(int(count)):
                ok, frame = cap.read()
                if not ok:
                    raise RuntimeError(f"Source decode ended at frame {index}/{int(count)}")
                box = active_box(rows, index / fps)
                if box:
                    frame = restore.apply(frame, box)
                else:
                    restore.previous = None
                patch = frame[ry:ry + rh, rx:rx + rw] if roi_only else frame
                proc.stdin.write(np.ascontiguousarray(patch).tobytes())
                if index % 150 == 0:
                    print(f"FRAME {index} {int(count)}", flush=True)
            proc.stdin.close()
            if proc.wait():
                raise RuntimeError("Temporary video encoding failed")
            if roi_only and roi_manifest_path is not None:
                roi_manifest_path.write_text(
                    json.dumps(
                        {
                            "mode": "roi_patch",
                            "x": rx,
                            "y": ry,
                            "w": rw,
                            "h": rh,
                            "intervals": active_intervals(rows),
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
        finally:
            cap.release()
            if proc.poll() is None:
                proc.kill()
            _release_vram(restore)


if __name__ == "__main__":
    main(*sys.argv[1:])
