"""VideoSubFinder adapter with seamless fallback to internal CueDetector."""

import logging
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import threading
from collections import deque
import cv2
import numpy as np
from pathlib import Path
from typing import Callable, List, Optional

from config.settings import settings
from src.jobs.logger import TaskCancelled
from src.jobs.resources import check_disk,TaskInterrupted,storage_reason
from src.ocr.base import Cue
from src.ocr.cue_detector import CueDetector
from src.ocr.deduplicate import deduplicate_cues
from src.ocr.rapidocr_engine import RapidOCREngine
from src.pipeline.roi import ROIBox, to_pixel_rect

logger = logging.getLogger(__name__)


def _legacy_wxw(binary):
    # Bundled 6.10 returns wxWidgets' -1 after its synchronous CLI work.
    # Do not extend this exception to unknown/custom executables.
    try:
        with Path(binary).open('rb') as stream:
            return hashlib.file_digest(stream,'sha256').hexdigest() == '7d8eb55f08f489aad9067c179806b98ac61a77f57272a9e6a688204fdc95abbe'
    except OSError:
        return False


def _native_error(path):
    try:return path.read_bytes()
    except FileNotFoundError:return b''


def _find_vsf(vsf_path=None):
    if vsf_path:
        path=Path(vsf_path)
        return path if path.is_file() else None
    for name in ("VideoSubFinderWXW.exe", "VideoSubFinder.exe", "VideoSubFinderWXW", "VideoSubFinder"):
        found=shutil.which(name)
        if found:return Path(found)
    bundled_root=settings.BASE_DIR/'tools/videosubfinder'
    if bundled_root.exists():
        for name in ("VideoSubFinderWXW.exe", "VideoSubFinder.exe", "VideoSubFinderCli.exe"):
            match=next(bundled_root.rglob(name),None)
            if match and match.is_file():return match
    return None


def _drain_output(stream, tail):
    if stream is not None:
        for line in stream:
            tail.append(line[-4096:])


def _stop_process(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


class VideoSubFinderAdapter:
    """Adapter for VideoSubFinder CLI with automated fallback to OpenCV CueDetector."""

    def __init__(self, vsf_path: Optional[str] = None):
        self.vsf_path = vsf_path or getattr(settings, "VSF_PATH", None)
        self.fallback_detector = CueDetector()
        self.warnings: List[str] = []
        self.engine = 'vsf-rapidocr'

    def get_executable(self) -> Optional[Path]:
        """Locate VideoSubFinder executable if installed."""
        return _find_vsf(self.vsf_path)

    @staticmethod
    def scan_cache_key(video_path: Path, roi: ROIBox, start_time=0.0, max_duration=None, vsf_path=None):
        stat = video_path.stat()
        with video_path.open('rb') as stream:
            source_sha256 = hashlib.file_digest(stream,'sha256').hexdigest()
        binary=_find_vsf(vsf_path or settings.VSF_PATH)
        binary_hash=settings_hash=None
        if binary:
            with binary.open('rb') as stream:binary_hash=hashlib.file_digest(stream,'sha256').hexdigest()
            cfg=binary.parent/'settings/general.cfg'
            if cfg.is_file():settings_hash=hashlib.sha256(cfg.read_bytes()).hexdigest()
        return {"version": "vsf-scan-v3-completion", "source": str(video_path.resolve()), "sha256":source_sha256,"size": stat.st_size,
                "binary_sha256":binary_hash,"native_settings_sha256":settings_hash,
                "cuda":settings.VSF_USE_CUDA,"threads":settings.VSF_THREADS,
                "mtime_ns": stat.st_mtime_ns, "roi": roi.model_dump(), "start": start_time,
                "duration": max_duration, "proxy_width": settings.VSF_PROXY_WIDTH,
                "proxy": settings.VSF_USE_SCAN_PROXY}

    def extract_cues(
        self,
        video_path: Path,
        roi: ROIBox,
        output_dir: Path,
        start_time: float = 0.0,
        max_duration: Optional[float] = None,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        should_continue: Optional[Callable[[], bool]] = None,
    ) -> List[Cue]:
        """Extract hardsub cues using VideoSubFinder if available, otherwise use CueDetector fallback."""
        cache_key = self.scan_cache_key(video_path, roi, start_time, max_duration, self.vsf_path) if video_path.is_file() else None
        manifest = output_dir / "vsf_scan.json"
        if cache_key and manifest.is_file():
            try:
                saved = json.loads(manifest.read_text(encoding="utf-8"))
                if not isinstance(saved, dict):
                    raise ValueError('VSF manifest must be an object')
                images = list((output_dir / "RGBImages").glob("*"))
                if saved.get("key") == cache_key and saved.get("image_count", 0) > 0 and len(images) == saved["image_count"]:
                    logger.info("Reusing completed VSF scan; recognizing existing subtitle frames")
                    cues = self.recognize_vsf_images(output_dir, progress_callback, should_continue)
                    offset = float(saved.get("time_offset", 0))
                    for cue in cues:
                        cue.start = round(cue.start + offset, 3)
                        cue.end = round(cue.end + offset, 3)
                    return deduplicate_cues(cues)
            except (OSError, ValueError, TypeError):
                logger.warning("Ignoring invalid VSF scan manifest")
        vsf_bin = self.get_executable()

        if vsf_bin is not None:
            logger.info(f"VideoSubFinder binary found at {vsf_bin}, attempting VSF extraction")
            # If VSF execution succeeds, parse results; if not or fails, fallback
            scan_video = video_path
            scan_start = start_time
            scan_duration = max_duration
            proxy_path: Optional[Path] = None
            try:
                if getattr(settings, "VSF_USE_SCAN_PROXY", True):
                    proxy_path = self._create_scan_proxy(
                        video_path=video_path,
                        output_dir=output_dir,
                        start_time=start_time,
                        max_duration=max_duration,
                        progress_callback=progress_callback,
                        should_continue=should_continue,
                    )
                    if proxy_path is not None:
                        scan_video = proxy_path
                        scan_start = 0.0
                        scan_duration = None
                cues = self._run_vsf(
                    vsf_bin,
                    scan_video,
                    roi,
                    output_dir,
                    scan_start,
                    scan_duration,
                    progress_callback=progress_callback,
                    should_continue=should_continue,
                    scan_cache_key=cache_key,
                )
                if proxy_path is not None and start_time:
                    for cue in cues:
                        cue.start = round(cue.start + start_time, 3)
                        cue.end = round(cue.end + start_time, 3)
                if cues:
                    return deduplicate_cues(cues)
            except Exception as e:
                if isinstance(e,TaskInterrupted) or storage_reason(e):raise
                check_disk()
                if should_continue and not should_continue():
                    raise TaskCancelled("Đã huỷ bóc sub theo yêu cầu") from e
                logger.warning(f"VideoSubFinder execution failed: {e}. Falling back to internal CueDetector.")
                self.warnings.append("VideoSubFinder gặp lỗi; đã chuyển sang dò thay đổi khung hình bằng OpenCV.")
            finally:
                if proxy_path is not None:
                    try:
                        proxy_path.unlink(missing_ok=True)
                    except OSError:
                        logger.warning("Could not remove VSF scan proxy: %s", proxy_path)

        logger.info("Using internal CueDetector (frame difference + RapidOCR)")
        self.engine = 'opencv-rapidocr'
        if vsf_bin is None:
            self.warnings.append("Không tìm thấy VideoSubFinder; dùng dò thay đổi khung hình bằng OpenCV.")
        raw_cues = self.fallback_detector.detect_cues(
            video_path=video_path,
            roi=roi,
            output_dir=output_dir,
            start_time=start_time,
            max_duration=max_duration,
            progress_callback=progress_callback,
            should_continue=should_continue,
        )
        return deduplicate_cues(raw_cues)

    def _create_scan_proxy(
        self,
        video_path: Path,
        output_dir: Path,
        start_time: float,
        max_duration: Optional[float],
        progress_callback: Optional[Callable[[int, str], None]] = None,
        should_continue: Optional[Callable[[], bool]] = None,
    ) -> Optional[Path]:
        """Create a low-resolution scan proxy; return None when unnecessary or unavailable."""
        proxy_width = max(480, int(getattr(settings, "VSF_PROXY_WIDTH", 960)))
        cap = cv2.VideoCapture(str(video_path))
        try:
            source_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0) if cap.isOpened() else 0
            source_duration = (float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                               / max(float(cap.get(cv2.CAP_PROP_FPS) or 0), 0.001)) if cap.isOpened() else 0
        finally:
            cap.release()
        if source_width and source_width <= proxy_width:
            return None

        try:
            # Reuse the renderer's FFmpeg selector: it chooses a binary whose
            # NVENC probe succeeds on the current driver, not merely one that
            # advertises the encoder.
            from src.pipeline.render import get_ffmpeg_bin
            ffmpeg_bin = get_ffmpeg_bin()
        except Exception as exc:
            logger.warning("Cannot locate FFmpeg for VSF proxy: %s", exc)
            return None

        output_dir.mkdir(parents=True, exist_ok=True)
        proxy_path = output_dir / "_vsf_scan_proxy.mp4"
        proxy_path.unlink(missing_ok=True)
        base_cmd = [
            str(ffmpeg_bin), "-hide_banner", "-loglevel", "error", "-y",
            "-progress", "pipe:1", "-nostats",
            "-ss", f"{max(0.0, start_time):.3f}", "-i", str(video_path),
        ]
        if max_duration is not None:
            base_cmd.extend(["-t", f"{max(0.001, max_duration):.3f}"])
        base_cmd.extend(["-an", "-vf", f"scale={proxy_width}:-2"])

        encoder_variants = [
            ["-c:v", "h264_nvenc", "-preset", "p1", "-cq", "26", "-rc", "vbr"],
            ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28"],
        ]
        if progress_callback:
            progress_callback(2, f"Đang tạo proxy {proxy_width}px để tăng tốc dò phụ đề...")

        for enc_args in encoder_variants:
            encoded = [0.0]
            proc = subprocess.Popen(
                base_cmd + enc_args + [str(proxy_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            def read_progress():
                for line in proc.stdout:
                    if line.startswith('out_time_us='):
                        try:
                            encoded[0] = max(0.0, float(line.split('=', 1)[1]) / 1_000_000)
                        except ValueError:
                            pass

            reader = threading.Thread(target=read_progress, daemon=True)
            errors = deque(maxlen=128)
            error_reader = threading.Thread(target=_drain_output, args=(proc.stderr, errors), daemon=True)
            reader.start()
            error_reader.start()
            scan_duration = min(max_duration, max(0, source_duration-start_time)) if max_duration is not None else max(0, source_duration-start_time)
            last_reported = -1.0
            deadline = time.monotonic() + settings.FFMPEG_TIMEOUT_SECONDS
            try:
                while proc.poll() is None:
                    check_disk()
                    if should_continue and not should_continue():
                        raise TaskCancelled("Đã huỷ tạo proxy dò phụ đề")
                    if time.monotonic() >= deadline:
                        raise TimeoutError('VSF proxy encoding deadline expired')
                    if progress_callback and encoded[0] - last_reported >= 1:
                        last_reported = encoded[0]
                        ratio = min(1.0, encoded[0] / scan_duration) if scan_duration else 0
                        progress_callback(2 + min(2, int(ratio * 3)),
                            f"Đang chuẩn bị video dò phụ đề {proxy_width}px: "
                            f"{encoded[0]:.0f}/{scan_duration:.0f} giây ({ratio*100:.0f}%)")
                    time.sleep(0.2)
            except BaseException:
                _stop_process(proc)
                proxy_path.unlink(missing_ok=True)
                raise
            finally:
                reader.join(timeout=2)
                error_reader.join(timeout=2)
            stderr = ''.join(errors)
            if proc.returncode == 0 and proxy_path.is_file() and proxy_path.stat().st_size > 0:
                logger.info("Created %sp VSF scan proxy with %s", proxy_width, enc_args[1])
                return proxy_path
            logger.warning("VSF proxy encoder %s failed: %s", enc_args[1], stderr[:300])
            proxy_path.unlink(missing_ok=True)

        logger.warning("VSF proxy creation failed; scanning the original video")
        return None

    def _run_vsf(
        self,
        vsf_bin: Path,
        video_path: Path,
        roi: ROIBox,
        output_dir: Path,
        start_time: float,
        max_duration: Optional[float],
        progress_callback: Optional[Callable[[int, str], None]] = None,
        should_continue: Optional[Callable[[], bool]] = None,
        scan_cache_key=None,
    ) -> List[Cue]:
        """Run VideoSubFinder process with live cancellation and progress updates."""
        def vsf_time(seconds: float) -> str:
            millis = max(0, int(round(seconds * 1000)))
            hours, rem = divmod(millis, 3_600_000)
            minutes, rem = divmod(rem, 60_000)
            secs, ms = divmod(rem, 1000)
            return f"{hours}:{minutes:02d}:{secs:02d}:{ms:03d}"

        # VideoSubFinder coordinates are offsets from the bottom for Y.
        top_from_bottom = 1.0 - roi.y
        bottom_from_bottom = 1.0 - (roi.y + roi.height)
        threads = int(getattr(settings, "VSF_THREADS", 0) or 0)
        if threads <= 0:
            threads = max(1, min(4, (os.cpu_count() or 2) - 1))
        cmd = [
            str(vsf_bin),
            "-c",
            "-r",
            "-i", str(video_path.resolve()),
            "-o", str(output_dir.resolve()),
            "-ovffmpeg",
            "-s", vsf_time(start_time),
            "-te", f"{top_from_bottom:.6f}",
            "-be", f"{bottom_from_bottom:.6f}",
            "-le", f"{roi.x:.6f}",
            "-re", f"{roi.x + roi.width:.6f}",
            "-nthr", str(threads),
        ]
        if max_duration:
            cmd.extend(["-e", vsf_time(start_time + max_duration)])
        if getattr(settings, "VSF_USE_CUDA", False):
            cmd.append("-uc")

        output_dir.mkdir(parents=True, exist_ok=True)
        completion = output_dir/'vsf_completed.srt'
        completion.unlink(missing_ok=True)
        cmd.extend(['-ces',str(completion.resolve())])
        native_errors = vsf_bin.parent/'error.log'
        errors_before = _native_error(native_errors)
        legacy_wxw = _legacy_wxw(vsf_bin)
        rgb_dir = output_dir / "RGBImages"
        txt_dir = output_dir / "TXTImages"
        ila_dir = output_dir / "ILAImages"
        # These folders are disposable VSF outputs. Keeping them across reruns
        # mixes old timestamps/images into the new OCR result.
        for stale_dir in (rgb_dir, txt_dir, ila_dir, output_dir / "cues"):
            if stale_dir.exists():
                shutil.rmtree(stale_dir, ignore_errors=True)
        (output_dir / "vsf_scan.json").unlink(missing_ok=True)
        if progress_callback:
            progress_callback(5, "Đang khởi động VideoSubFinder quét khung hình...")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(vsf_bin.parent),
        )
        stdout_tail, stderr_tail = deque(maxlen=128), deque(maxlen=128)
        readers = [threading.Thread(target=_drain_output, args=(stream, tail), daemon=True)
                   for stream, tail in ((proc.stdout, stdout_tail), (proc.stderr, stderr_tail))]
        for reader in readers:reader.start()

        timeout_seconds = getattr(settings, "FFMPEG_TIMEOUT_SECONDS", 1800)
        start_ts = time.time()
        cap = cv2.VideoCapture(str(video_path))
        try:
            source_duration = (
                float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                / max(float(cap.get(cv2.CAP_PROP_FPS) or 0), 0.001)
            ) if cap.isOpened() else 0.0
        finally:
            cap.release()
        scan_end = start_time + max_duration if max_duration else source_duration

        try:
            while proc.poll() is None:
                check_disk()
                if should_continue and not should_continue():
                    raise TaskCancelled("Đã dừng VideoSubFinder theo yêu cầu")

                elapsed = time.time() - start_ts
                if elapsed > timeout_seconds:
                    raise TimeoutError(f"VideoSubFinder quá thời gian chờ ({timeout_seconds}s)")

                if progress_callback:
                    found_count = 0
                    latest_time = start_time
                    image_files: List[Path] = []
                    if ila_dir.exists():
                        image_files = sorted(list(ila_dir.glob("*.jpeg")) + list(ila_dir.glob("*.png")))
                    elif rgb_dir.exists():
                        image_files = sorted(list(rgb_dir.glob("*.jpeg")) + list(rgb_dir.glob("*.png")))
                    found_count = len(image_files)
                    # Candidate filenames contain their real video time range.
                    # This is much more meaningful than the old image-count
                    # heuristic, which hit 40% early and appeared frozen.
                    for image_file in image_files[-8:]:
                        parsed = self._parse_vsf_time_range(image_file.stem)
                        if parsed:
                            latest_time = max(latest_time, parsed[1])
                    if scan_end > start_time and latest_time > start_time:
                        scan_ratio = min(1.0, (latest_time - start_time) / (scan_end - start_time))
                        pct = 5 + int(scan_ratio * 35)
                    else:
                        pct = 5
                    progress_callback(pct, f"VideoSubFinder đang dò phụ đề (đã thấy {found_count} khung hình xuất hiện chữ)...")

                time.sleep(0.8)

            for reader in readers:reader.join(timeout=2)
            stdout, stderr = ''.join(stdout_tail), ''.join(stderr_tail)
            native_error = _native_error(native_errors)
            new_native_error = native_error if native_error != errors_before else b''
            sentinel = completion.is_file() and '-->' in completion.read_text(encoding='utf-8-sig')
            completed = not new_native_error and (
                proc.returncode == 0 or (legacy_wxw and proc.returncode in {-1,4294967295} and sentinel))
            (output_dir/'vsf_process.json').write_text(json.dumps({
                'exit_code':proc.returncode,'elapsed_seconds':round(time.time()-start_ts,3),
                'stdout_tail':stdout,'stderr_tail':stderr,'completion_sentinel':sentinel,
                'known_legacy_wxw':legacy_wxw,'completed':completed,
                'native_error':new_native_error.decode('utf-8',errors='replace')[-3000:]},ensure_ascii=False),encoding='utf-8')
            if not completed:
                detail = (new_native_error.decode('utf-8',errors='replace') or stderr or stdout or f"exit code {proc.returncode}")[:500]
                raise RuntimeError(f"VSF error: {detail}")

        except BaseException:
            _stop_process(proc)
            raise
        finally:
            for reader in readers:reader.join(timeout=2)

        if should_continue and not should_continue():
            raise TaskCancelled("Đã dừng bóc phụ đề")

        if completed and scan_cache_key:
            manifest = output_dir / "vsf_scan.json"
            temporary = manifest.with_suffix(".tmp.json")
            temporary.write_text(json.dumps({"key": scan_cache_key, "image_count": len(list(rgb_dir.glob('*'))), "time_offset": float(scan_cache_key['start']) - start_time}), encoding="utf-8")
            os.replace(temporary, manifest)

        return self.recognize_vsf_images(output_dir, progress_callback, should_continue)

    def recognize_vsf_images(self, output_dir: Path, progress_callback=None, should_continue=None) -> List[Cue]:
        """Recognize an already completed VSF scan without scanning the film again."""
        txt_dir = output_dir / "TXTImages"
        rgb_dir = output_dir / "RGBImages"
        # RapidOCR recognition
        engine = RapidOCREngine()
        evidence_dir = output_dir / "cues"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        cues: List[Cue] = []
        source_dir = txt_dir if any(txt_dir.glob("*")) else rgb_dir
        candidates = sorted({
            p for pattern in ("*.png", "*.jpg", "*.jpeg", "*.bmp")
            for p in source_dir.glob(pattern)
        })

        total = len(candidates)
        if total == 0:
            if progress_callback:
                progress_callback(95, "Không phát hiện khung hình chứa chữ trong vùng ROI đã chọn")
            return cues

        prev_hash: Optional[int] = None
        prev_image: Optional[np.ndarray] = None
        for idx, image_path in enumerate(candidates):
            check_disk()
            if should_continue and not should_continue():
                raise TaskCancelled("Đã dừng trong lúc nhận diện OCR")

            if progress_callback:
                pct = 40 + int(((idx + 1) / total) * 55)
                if idx % 2 == 0 or idx == total - 1:
                    progress_callback(pct, f"RapidOCR nhận diện chữ ({idx + 1}/{total} ảnh, đã trích {len(cues)} câu)...")

            time_range = self._parse_vsf_time_range(image_path.stem)
            if time_range is None:
                continue
            image = cv2.imread(str(image_path))
            if image is None or image.size == 0:
                continue

            # Perceptual hash pre-filtering: merge adjacent duplicate frames to skip redundant OCR
            img_hash = self._compute_dhash(image)
            if cues and prev_hash is not None and self._hamming_distance(prev_hash, img_hash) <= 4 and prev_image is not None and np.array_equal(prev_image, image):
                prev_cue = cues[-1]
                if abs(time_range[0] - prev_cue.end) <= 0.5:
                    prev_cue.end = max(prev_cue.end, round(time_range[1], 3))
                    prev_hash = img_hash
                    continue

            import hashlib,importlib.metadata,tempfile
            try:engine_version=importlib.metadata.version('rapidocr-onnxruntime')
            except importlib.metadata.PackageNotFoundError:engine_version='unknown'
            identity=hashlib.sha256(image_path.read_bytes()+('rapid-cache-v1:'+engine_version+':'+settings.OCR_LANGUAGES).encode()).hexdigest()
            cache_root=settings.storage_path.parent/'ocr_cache';cache_root.mkdir(parents=True,exist_ok=True)
            cache_file=cache_root/(identity+'.json');record=None
            if cache_file.is_file():
                try:
                    cached=json.loads(cache_file.read_text(encoding='utf-8'))
                    encoded=json.dumps(cached['result'],sort_keys=True,ensure_ascii=False).encode()
                    if hashlib.sha256(encoded).hexdigest()==cached['sha256']:record=cached['result']
                except (ValueError,KeyError,OSError,TypeError):pass
            if record is None:
                text,confidence=engine.recognize(image)
                alternatives=engine.last_alternatives if isinstance(getattr(engine,'last_alternatives',None),list) else []
                record={'text':text,'confidence':float(confidence),'alternatives':alternatives}
                encoded=json.dumps(record,sort_keys=True,ensure_ascii=False).encode()
                with tempfile.NamedTemporaryFile('w',dir=cache_root,delete=False,encoding='utf-8') as stream:
                    json.dump({'result':record,'sha256':hashlib.sha256(encoded).hexdigest()},stream,ensure_ascii=False);stream.flush();os.fsync(stream.fileno());temporary=stream.name
                os.replace(temporary,cache_file)
            text,confidence=record['text'],record['confidence']
            alternatives=record['alternatives']
            check_disk()
            if not text.strip():
                continue
            number = len(cues) + 1
            evidence_name = f"cue_{number:04d}{image_path.suffix.lower()}"
            shutil.copy2(image_path, evidence_dir / evidence_name)
            cues.append(Cue(
                index=number,
                start=round(time_range[0], 3),
                end=round(time_range[1], 3),
                text=text.strip(),
                confidence=round(confidence, 1),
                ocr_alternatives=alternatives,
                image_path=f"cues/{evidence_name}",
            ))
            prev_hash = img_hash
            prev_image = image.copy()

        if progress_callback:
            progress_callback(98, f"Hoàn tất bóc tách {len(cues)} câu thoại từ {total} khung hình")

        return cues

    @staticmethod
    def _compute_dhash(img: np.ndarray, hash_size: int = 8) -> int:
        """Compute difference hash (dHash) for fast perceptual image comparison."""
        if len(img.shape) == 3:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        else:
            gray = img
        resized = cv2.resize(gray, (hash_size + 1, hash_size))
        diff = resized[:, 1:] > resized[:, :-1]
        return sum([2 ** i for (i, v) in enumerate(diff.flatten()) if v])

    @staticmethod
    def _hamming_distance(h1: int, h2: int) -> int:
        return bin(h1 ^ h2).count("1")

    @staticmethod
    def _parse_vsf_time_range(stem: str):
        """Parse VideoSubFinder's HH_MM_SS_mmm__HH_MM_SS_mmm filenames."""
        matches = re.findall(r"(\d{1,2})_(\d{2})_(\d{2})_(\d{3})", stem)
        if len(matches) < 2:
            return None

        def seconds(parts):
            hours, minutes, secs, millis = (int(value) for value in parts)
            return hours * 3600 + minutes * 60 + secs + millis / 1000.0

        start, end = seconds(matches[0]), seconds(matches[1])
        return (start, end) if end > start else None

