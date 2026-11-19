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


def available_ram_bytes():
    try:
        import psutil
        return psutil.virtual_memory().available
    except (ImportError, OSError):
        return None


def review_cuda_runtime(minimum_vram_mib):
    device, compute = whisper_runtime()
    if device != 'cuda':
        return None
    try:
        import ctranslate2
        if compute not in ('auto', 'default') and compute not in ctranslate2.get_supported_compute_types('cuda'):
            return None
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=memory.free', '--format=csv,noheader,nounits'],
            capture_output=True, text=True, check=True, timeout=3,
        )
        # CTranslate2 uses its default CUDA device, so require enough memory
        # on every listed device when physical/logical ordering can differ.
        if min(map(int, result.stdout.splitlines())) >= minimum_vram_mib:
            return device, compute
    except (ImportError, OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        pass
    return None


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
_MODEL_CACHE: Dict[Tuple[str, ...], object] = {}
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
        self.model_fingerprint = None
        self.cpu_threads = None
        self.local_files_only = settings.OFFLINE_MODE
        self.isolated_review = False

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
        if self.model_fingerprint:
            key += (self.model_fingerprint,)
        try:
            with _MODEL_LOCK:
                if key not in _MODEL_CACHE:
                    _MODEL_CACHE[key] = WhisperModel(
                        self.model_size,
                        device=self.device,
                        compute_type=self.compute_type,
                        cpu_threads=2 if self.device == "cuda" else (self.cpu_threads or _cpu_threads()),
                        num_workers=1,
                        local_files_only=self.local_files_only,
                    )
                self._model = _MODEL_CACHE[key]
        except Exception as e:
            if self.device == "cuda" and not getattr(self, 'review_chunk_length', None):
                # GPU hỏng (thiếu DLL CUDA, hết VRAM...) thì chạy CPU còn hơn dừng hẳn
                return self._fallback_to_cpu(reason=str(e), logger=logger)

            raise PipelineException(
                SystemErrorCodes.STT_MODEL_UNAVAILABLE,
                f"Không thể nạp model Whisper '{self.model_size}': {type(e).__name__}: {str(e)}",
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
                local_files_only=settings.OFFLINE_MODE,
            )
            return self._model

    def transcribe_review_batch(self, audio_paths, language='zh', should_continue=None,
                                on_progress=None, on_result=None):
        """Run one isolated recognizer and checkpoint valid results as they arrive."""
        import json
        import sys
        import tempfile
        from src.jobs.resources import run_media

        paths = [str(Path(path).resolve()) for path in audio_paths]
        if not paths:
            raise ValueError('Speech review requires at least one window')
        manifest = result_path = None
        found = {}
        last_error = ''
        offset = 0
        streaming = bool(on_progress or on_result)

        def consume(line):
            nonlocal last_error
            try:
                row = json.loads(line)
            except ValueError:
                return
            index = row.get('index') if isinstance(row, dict) else None
            if type(index) is not int or not 0 <= index < len(paths) or index in found:
                raise PipelineException(SystemErrorCodes.STT_MODEL_UNAVAILABLE, 'Invalid speech batch response')
            if 'result' in row:
                result = TranscriptResult.model_validate(row['result'])
                if on_result:
                    on_result(index, result)
            else:
                error = row.get('error', {})
                last_error = error.get('message', 'Speech review failed')
                result = PipelineException(error.get('code', SystemErrorCodes.STT_MODEL_UNAVAILABLE), last_error)
            found[index] = result
            if on_progress:
                on_progress(len(found))

        def poll():
            nonlocal offset
            if not result_path.is_file():
                return
            with result_path.open('rb') as stream:
                stream.seek(offset)
                for line in stream:
                    if not line.endswith(b'\n'):
                        break
                    consume(line)
                    offset += len(line)

        try:
            with tempfile.NamedTemporaryFile('w', encoding='utf8', suffix='.speech-batch.json',
                                             dir=Path(paths[0]).parent, delete=False) as stream:
                manifest = Path(stream.name)
                json.dump(dict(model=str(self.model_size), audios=paths, language=language,
                               device=self.device, compute_type=self.compute_type,
                               stream_results=streaming), stream)
            result_path = manifest.with_suffix('.results.jsonl')
            result_options = {'on_poll': poll} if streaming else {}
            # Keep the old timeout budget per 48 windows without reloading
            # the model when a long video crosses that boundary.
            timeout = settings.FFMPEG_TIMEOUT_SECONDS * ((len(paths) + 47) // 48)
            try:
                result = run_media([sys.executable, str(Path(__file__).with_name('review_worker.py')),
                                    '--batch', str(manifest)], timeout=timeout,
                                   stage='OCR_AUDIO_RECHECK', should_continue=should_continue, **result_options)
            except (TaskCancelled, subprocess.TimeoutExpired):
                poll()
                raise
            last_error = last_error or result.stderr[-3000:]
            poll()
            if not result_path.is_file():
                for line in result.stdout.splitlines():
                    consume(line)
            return [found.get(index, PipelineException(SystemErrorCodes.STT_MODEL_UNAVAILABLE,
                        last_error or 'Speech worker did not finish this window')) for index in range(len(paths))]
        finally:
            if manifest:
                manifest.unlink(missing_ok=True)
            if result_path:
                result_path.unlink(missing_ok=True)

    def transcribe(
        self,
        audio_path: Union[Path, str],
        language: Optional[str] = None,
        on_progress: Optional[Callable[[float], None]] = None,
        logger=None,
        should_continue: Optional[Callable[[], bool]] = None,
    ) -> TranscriptResult:
        if self.isolated_review:
            import sys
            from src.jobs.resources import run_media

            result = run_media(
                [sys.executable, str(Path(__file__).with_name('review_worker.py')),
                 str(self.model_size), str(audio_path), language or 'zh'],
                timeout=settings.FFMPEG_TIMEOUT_SECONDS, stage='OCR_AUDIO_RECHECK',
                should_continue=should_continue)
            if result.returncode:
                raise PipelineException(SystemErrorCodes.STT_MODEL_UNAVAILABLE, result.stderr[-3000:])
            return TranscriptResult.model_validate_json(result.stdout.strip().splitlines()[-1])
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

            is_gpu_err = (self.device == "cuda" and not getattr(self, "review_chunk_length", None)
                          and any(k in msg for k in OOM_OR_CUDA_ERRORS))
            if not is_gpu_err and self.device == "cuda" and not getattr(self, 'review_chunk_length', None):
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

        review_chunk = getattr(self, 'review_chunk_length', None)
        review_options = dict(chunk_length=review_chunk, best_of=1, temperature=0.0) if review_chunk else {}
        try:
            raw_segments, info = model.transcribe(
                str(path),
                language=language,
                beam_size=1 if review_chunk else settings.WHISPER_BEAM_SIZE,
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
                **review_options,
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
                        avg_logprob=getattr(seg, "avg_logprob", None),
                        no_speech_prob=getattr(seg, "no_speech_prob", None),
                        words=[dict(start=word.start, end=word.end, word=word.word,
                                    probability=word.probability) for word in (words or [])
                               if all(hasattr(word, name) for name in ('start', 'end', 'word', 'probability'))],
                    )
                )
        except TaskCancelled:
            raise
        except PipelineException:
            raise
        except Exception as e:
            raise PipelineException(
                SystemErrorCodes.STT_ERROR,
                f"Lỗi khi nhận dạng giọng nói: {type(e).__name__}: {str(e)}",
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
