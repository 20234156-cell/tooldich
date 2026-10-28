import os
import subprocess
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union

# Giới hạn số luồng MKL/OpenMP để tránh mkl_malloc chiếm dụng quá nhiều heap khi RAM cạn
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")

from config.settings import settings
from src.gpu import whisper_runtime
from src.jobs.logger import SystemErrorCodes, PipelineException, TaskCancelled
from src.stt.base import STTProvider, TranscriptResult, TranscriptSegment, collapse_repeats


def get_system_memory_diag() -> str:
    """Thu thập thông tin VRAM và RAM hệ thống khi gặp sự cố cấp phát bộ nhớ."""
    diag_parts = []
    try:
        import psutil
        vm = psutil.virtual_memory()
        diag_parts.append(
            f"RAM: Total={vm.total / 1024**3:.2f}GB, Avail={vm.available / 1024**3:.2f}GB, "
            f"Used={vm.used / 1024**3:.2f}GB ({vm.percent}%)"
        )
    except Exception as ex:
        diag_parts.append(f"RAM: Không lấy được thông tin ({ex})")

    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            diag_parts.append(f"GPU: {res.stdout.strip()}")
        else:
            diag_parts.append("GPU: nvidia-smi trả về rỗng")
    except Exception as ex:
        diag_parts.append(f"GPU: nvidia-smi không khả dụng ({ex})")

    return " | ".join(diag_parts)


OOM_OR_CUDA_ERRORS = (
    "bad allocation",
    "out of memory",
    "cuda",
    "cublas",
    "cudnn",
    "onnxruntime",
    "allocat",
    "memory",
    "resource exhausted",
    "std::bad_alloc",
)


# Nạp model mất vài giây (GPU còn phải khởi tạo CUDA) - giữ lại dùng cho mọi dự án
# sau thay vì nạp lại mỗi lần. Khoá để hai dự án chạy song song không tranh GPU.
_MODEL_CACHE: Dict[Tuple[str, str, str], object] = {}
_MODEL_LOCK = threading.Lock()
_RUN_LOCK = threading.Lock()


class WhisperSTTProvider(STTProvider):
    """Triển khai STT bằng faster-whisper chạy cục bộ (offline, không gửi dữ liệu ra ngoài)."""

    name = "faster-whisper"

    def __init__(
        self,
        model_size: Optional[str] = None,
        device: Optional[str] = None,
        compute_type: Optional[str] = None,
    ):
        auto_device, auto_compute = whisper_runtime()
        self.device = device or auto_device
        self.compute_type = compute_type or auto_compute

        req_size = model_size or settings.WHISPER_MODEL_SIZE
        if not req_size or req_size in ("auto", "base"):
            self.model_size = "large-v3-turbo" if self.device == "cuda" else "small"
        else:
            self.model_size = req_size
        self._model = None

    def _load_model(self, logger=None):
        """Nạp model faster-whisper (lazy import để không bắt buộc cài khi chỉ dùng phase khác)."""
        if self._model is not None:
            return self._model

        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise PipelineException(
                SystemErrorCodes.STT_MODEL_UNAVAILABLE,
                "Chưa cài đặt faster-whisper. Chạy: pip install faster-whisper",
            )

        key = (self.model_size, self.device, self.compute_type)
        try:
            with _MODEL_LOCK:
                if key not in _MODEL_CACHE:
                    _MODEL_CACHE[key] = WhisperModel(
                        self.model_size,
                        device=self.device,
                        compute_type=self.compute_type,
                        cpu_threads=2 if self.device == "cuda" else _cpu_threads(),
                    )
                self._model = _MODEL_CACHE[key]
        except Exception as e:
            if self.device == "cuda":
                # GPU hỏng (thiếu DLL CUDA, hết VRAM...) thì chạy CPU còn hơn dừng hẳn
                return self._fallback_to_cpu(reason=str(e), logger=logger)

            raise PipelineException(
                SystemErrorCodes.STT_MODEL_UNAVAILABLE,
                f"Không thể nạp model Whisper '{self.model_size}': {str(e)}",
            )

        return self._model

    def _fallback_to_cpu(self, reason: str = "", logger=None):
        diag = get_system_memory_diag()
        msg = (
            f"Phát hiện sự cố cấp phát bộ nhớ GPU/VRAM ({reason[:120]}). "
            f"Tài nguyên hệ thống: [{diag}]. "
            f"Tự động fallback sang CPU (device='cpu', compute_type='int8')."
        )
        if logger and hasattr(logger, "log"):
            logger.log(
                stage="TRANSCRIBING",
                status="WARNING",
                level="WARNING",
                message=msg,
            )

        # Giải phóng model cache CUDA cũ để trả lại VRAM
        with _MODEL_LOCK:
            to_del = [k for k in _MODEL_CACHE if k[1] == "cuda"]
            for k in to_del:
                _MODEL_CACHE.pop(k, None)
        import gc
        gc.collect()

        self.device, self.compute_type, self._model = "cpu", "int8", None
        try:
            return self._load_model()
        except Exception:
            key = (self.model_size, "cpu", "int8")
            with _MODEL_LOCK:
                _MODEL_CACHE.pop(key, None)
            from faster_whisper import WhisperModel
            self._model = WhisperModel(
                self.model_size,
                device="cpu",
                compute_type="int8",
                cpu_threads=1,
            )
            return self._model

    def transcribe(
        self,
        audio_path: Union[Path, str],
        language: Optional[str] = None,
        on_progress: Optional[Callable[[float], None]] = None,
        logger=None,
        should_continue: Optional[Callable[[], bool]] = None,
    ) -> TranscriptResult:
        try:
            with _RUN_LOCK:
                return self._transcribe(
                    audio_path, language, on_progress, logger=logger, should_continue=should_continue
                )
        except TaskCancelled:
            raise
        except Exception as e:
            msg = str(e).lower()
            if hasattr(e, "message"):
                msg += " " + str(getattr(e, "message", "")).lower()

            is_gpu_err = self.device == "cuda" and any(k in msg for k in OOM_OR_CUDA_ERRORS)
            if not is_gpu_err and self.device == "cuda":
                # Bất kỳ lỗi không xác định nào khi chạy GPU đều thử lại trên CPU
                is_gpu_err = True

            if not is_gpu_err:
                if isinstance(e, PipelineException):
                    raise
                raise PipelineException(SystemErrorCodes.STT_ERROR, str(e))

            self._fallback_to_cpu(reason=str(e), logger=logger)
            with _RUN_LOCK:
                return self._transcribe(
                    audio_path, language, on_progress, logger=logger, should_continue=should_continue
                )

    def _transcribe(
        self,
        audio_path: Union[Path, str],
        language: Optional[str],
        on_progress: Optional[Callable[[float], None]],
        logger=None,
        should_continue: Optional[Callable[[], bool]] = None,
    ) -> TranscriptResult:
        """Chuyển audio thành transcript có timestamp từng câu."""
        if should_continue is not None and not should_continue():
            raise TaskCancelled("Tác vụ nhận dạng giọng nói đã bị huỷ bởi người dùng")

        path = Path(audio_path).resolve()
        if not path.exists() or path.stat().st_size == 0:
            raise PipelineException(
                SystemErrorCodes.STT_ERROR,
                f"Tệp audio không tồn tại hoặc rỗng: {path}",
            )

        model = self._load_model(logger=logger)

        initial_prompt = None
        if not language or language.lower() in ("zh", "chinese"):
            initial_prompt = "以下是普通话的句子，带标点符号。"

        try:
            raw_segments, info = model.transcribe(
                str(path),
                language=language,
                beam_size=settings.WHISPER_BEAM_SIZE,
                vad_filter=settings.WHISPER_VAD_FILTER,
                vad_parameters=dict(
                    threshold=settings.WHISPER_VAD_THRESHOLD,
                    min_silence_duration_ms=settings.WHISPER_VAD_MIN_SILENCE_MS,
                    speech_pad_ms=settings.WHISPER_VAD_SPEECH_PAD_MS,
                ),
                word_timestamps=True,
                hallucination_silence_threshold=2.0,
                condition_on_previous_text=False,
                initial_prompt=initial_prompt,
            )
            segments: List[TranscriptSegment] = []
            total = float(getattr(info, "duration", 0.0) or 0.0)
            for seg in raw_segments:
                if should_continue is not None and not should_continue():
                    raise TaskCancelled("Tác vụ nhận dạng giọng nói đã bị huỷ bởi người dùng")
                if on_progress and total > 0:
                    on_progress(min(1.0, float(seg.end) / total))
                text = (getattr(seg, "text", "") or "").strip()
                if not text:
                    continue

                seg_start = round(float(seg.start), 3)
                words = getattr(seg, "words", None)
                if words and len(words) > 0:
                    seg_end = round(float(words[-1].end), 3)
                else:
                    seg_end = round(float(seg.end), 3)

                if seg_end < seg_start:
                    seg_end = round(float(seg.end), 3)

                segments.append(
                    TranscriptSegment(
                        start=seg_start,
                        end=seg_end,
                        original=text,
                    )
                )
        except TaskCancelled:
            raise
        except PipelineException:
            raise
        except Exception as e:
            raise PipelineException(
                SystemErrorCodes.STT_ERROR,
                f"Lỗi khi nhận dạng giọng nói: {str(e)}",
            )

        segments = collapse_repeats(segments)
        if not segments:
            raise PipelineException(
                SystemErrorCodes.STT_ERROR,
                "Không nhận dạng được nội dung lời thoại nào trong audio",
            )

        detected = (getattr(info, "language", None) or language or "unknown").lower()
        duration = float(getattr(info, "duration", 0.0) or 0.0)
        if duration <= 0:
            duration = segments[-1].end

        return TranscriptResult(
            language=detected,
            duration=round(duration, 2),
            segments=segments,
        )


def _cpu_threads() -> int:
    import os

    return max(1, min(4, (os.cpu_count() or 4)))
