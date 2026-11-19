"""Run LaMa in the installed GPU Python environment."""
from dataclasses import dataclass
import gc
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

from src.jobs.resources import check_disk


@dataclass
class LamaRoiPatch:
    """Compact 1-pass ROI patch emitted by LaMa worker (avoids full-frame intermediate MP4)."""

    path: Path
    x: int
    y: int
    w: int
    h: int
    intervals: List[Tuple[float, float]]


def _release_lama_cuda_host() -> None:
    """Ensure LaMa worker handle & host CUDA cache are cleared before NVENC begins."""
    gc.collect()
    torch_mod = sys.modules.get("torch")
    if torch_mod is not None and hasattr(torch_mod, "cuda") and torch_mod.cuda.is_available():
        try:
            torch_mod.cuda.empty_cache()
        except Exception:
            pass


def _run_lama_worker(source, directory, ffmpeg, cues, band, progress, should_continue, *, roi_only: bool):
    from src.pipeline.render import TaskCancelled

    root = Path(__file__).resolve().parents[2]
    python = root / "tools/vieneu-gpu/Scripts/python.exe"
    model = root / "storage/models/lama/big-lama.pt"
    if not model.is_file():
        raise RuntimeError("Thiếu mô hình LaMa tại storage/models/lama/big-lama.pt")
    manifest = directory / "captions.json"
    roi_manifest = directory / "roi_manifest.json"
    payload = {
        "cues": [cue.model_dump() for cue in cues],
        "band": band,
        "roi_only": roi_only,
    }
    if roi_only:
        payload["roi_manifest"] = str(roi_manifest)
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    output = directory / ("roi_patch.mp4" if roi_only else "restored.mp4")
    log = directory / "restore.log"
    if progress:
        progress(14, "LaMa đang dò vị trí chữ Trung")
    with log.open("wb") as writer, log.open(encoding="utf-8", errors="replace") as reader:
        proc = subprocess.Popen(
            [str(python) if python.is_file() else sys.executable, "-u", "-m",
             "src.video.lama_worker", str(source.resolve()), str(output), str(manifest),
             str(model), str(ffmpeg.resolve())],
            cwd=root, stdout=writer, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            while proc.poll() is None:
                check_disk()
                if should_continue is not None and not should_continue():
                    raise TaskCancelled()
                for line in reader.readlines():
                    if progress and line.startswith("Scan "):
                        current, total = line.split()[1].split("/")
                        progress(14 + int(4 * int(current) / int(total)),
                                 f"LaMa đang dò chữ Trung ({current}/{total} câu)")
                    if progress and line.startswith("FRAME "):
                        _, current, total = line.split()
                        progress(18 + int(6 * int(current) / int(total)),
                                 f"LaMa đang phục hồi nền ROI ({current}/{total} khung hình)")
                time.sleep(.5)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            else:
                proc.wait()
            _release_lama_cuda_host()
    if proc.returncode:
        raise RuntimeError("LaMa không xử lý được video: " + log.read_text(
            encoding="utf-8", errors="replace")[-1200:])
    if output.is_file() and output.stat().st_size:
        if roi_only:
            meta = json.loads(roi_manifest.read_text(encoding="utf-8"))
            intervals = [(float(s), float(e)) for s, e in meta.get("intervals", []) if float(e) > float(s)]
            return LamaRoiPatch(
                path=output,
                x=int(meta["x"]),
                y=int(meta["y"]),
                w=int(meta["w"]),
                h=int(meta["h"]),
                intervals=intervals,
            )
        return output
    if "NO_CAPTIONS" in log.read_text(encoding="utf-8", errors="replace"):
        return None if roi_only else source
    raise RuntimeError("LaMa chưa xuất được vùng phục hồi ROI")


def restore_roi_patch(source, directory, ffmpeg, cues, band, progress, should_continue) -> Optional[LamaRoiPatch]:
    """Run LaMa on ONLY the compact caption ROI strip, freeing GPU VRAM upon exit."""
    return _run_lama_worker(source, directory, ffmpeg, cues, band, progress, should_continue, roi_only=True)


def restore_video(source, directory, ffmpeg, cues, band, progress, should_continue, *, roi_only: bool = False):
    """Backward-compatible entrypoint supporting full restore or 1-pass ROI-only mode."""
    return _run_lama_worker(source, directory, ffmpeg, cues, band, progress, should_continue, roi_only=roi_only)
