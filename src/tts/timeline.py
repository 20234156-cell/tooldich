from src.jobs.resources import check_disk,TaskInterrupted,storage_reason
"""Per-cue slot fitting, silence trimming, and audio timeline assembly (Section 5, Step 7)."""

import io
import json
import logging
import math
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import soundfile as sf
import numpy as np

from config.settings import settings
from src.jobs.logger import PipelineException, SystemErrorCodes, TaskCancelled
from src.ocr.base import Cue, cues_to_srt
from src.tts.base import BaseTTSProvider
from src.tts.cache import TTSCache
from src.tts.edge import EdgeTTSProvider, EdgeSynthesisError
from src.tts.normalizer_vi import normalize_vietnamese_text
from src.tts.policy import TimingPolicy, rate_multiplier
from src.tts.speech_text import spoken_text

logger = logging.getLogger(__name__)


def _provider_failure(cue_index, error):
    """Keep diagnostic types/status codes without URLs, tokens or cue text."""
    record = {'cue_index': cue_index, 'error_chain': []}
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        record['error_chain'].append(type(error).__name__)
        for name in ('status', 'errno', 'winerror'):
            value = getattr(error, name, None)
            if isinstance(value, int):
                record[name] = value
        error = error.__cause__
    return record


def get_ffmpeg_bin() -> Path:
    if getattr(settings, "FFMPEG_PATH", None):
        p = Path(settings.FFMPEG_PATH)
        if p.is_file():
            return p
    found = shutil.which("ffmpeg")
    if found:
        return Path(found)
    raise PipelineException(
        SystemErrorCodes.FFMPEG_NOT_FOUND,
        "Không tìm thấy công cụ ffmpeg.",
    )


def apply_atempo(audio_bytes: bytes, tempo: float) -> bytes:
    """Adjust a clip within the shared nominal speed ceiling."""
    tempo = max(0.8, min(TimingPolicy.current().max_speed, tempo))
    if abs(tempo - 1.0) < 0.02:
        return audio_bytes

    ffmpeg_bin = get_ffmpeg_bin()
    cmd = [
        str(ffmpeg_bin),
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", "pipe:0",
        "-filter:a", f"atempo={tempo:.3f}",
        "-f", "wav",
        "pipe:1",
    ]
    try:
        res = subprocess.run(cmd, input=audio_bytes, capture_output=True, check=True)
        return res.stdout
    except Exception as e:
        raise PipelineException(SystemErrorCodes.TTS_FAILED, f'Không căn được tốc độ audio (atempo={tempo:.3f}).') from e


class AudioTimelineBuilder:
    """Builds aligned voiceover audio track matching cue timeline without drifting."""

    def __init__(
        self,
        provider: Optional[BaseTTSProvider] = None,
        cache: Optional[TTSCache] = None,
        max_atempo: Optional[float] = None,
        max_concurrency: Optional[int] = None,
    ):
        self.provider = provider or EdgeTTSProvider()
        self.cache = cache or TTSCache()
        self.policy = TimingPolicy.current()
        self.max_atempo = min(max_atempo or self.policy.max_speed, self.policy.max_speed)
        self.max_concurrency = max_concurrency

    def synthesize_cues(
        self,
        cues: List[Cue],
        output_dir: Path,
        voice: Optional[str] = None,
        rate: str = "+0%",
        pitch: str = "+0Hz",
        progress_callback: Optional[Callable[[int, str], None]] = None,
        should_continue: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """Synthesize voiceover for all cues, fit to slots, and build unified voiceover.wav."""
        from src.tts.processing import parse_voice, trim_rms, apply_color
        base_voice, color = parse_voice(voice)
        if (voice and voice.startswith('vieneu:')) or self.provider.name == 'vieneu':
            from src.tts.registry import provider_for_voice
            self.provider = provider_for_voice(voice)
        cues = sorted(cues, key=lambda cue: (cue.start, cue.index))
        try:
            if any(rate_multiplier(cue.tts_rate or rate) > self.policy.max_speed + 1e-9 for cue in cues):
                raise ValueError('Tốc độ tổng vượt trần 1,3 lần; hãy rút gọn lời thoại.')
        except ValueError as exc:
            raise PipelineException(SystemErrorCodes.TTS_FAILED, str(exc)) from exc
        if len({cue.index for cue in cues}) != len(cues) or any(
            not math.isfinite(cue.start) or not math.isfinite(cue.end) or cue.end <= cue.start
            for cue in cues
        ):
            raise PipelineException(SystemErrorCodes.TTS_FAILED, 'Cue có ID trùng hoặc khoảng thời gian không hợp lệ; hãy sửa phụ đề trước khi tạo giọng.')
        output_dir.mkdir(parents=True, exist_ok=True)
        vo_dir = output_dir / "voiceover"
        vo_dir.mkdir(parents=True, exist_ok=True)
        clips_dir = vo_dir / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)

        total_cues = len(cues)
        if total_cues == 0:
            return {"timeline": [], "voiceover_wav": None, "total_duration": 0.0, "max_drift": 0.0}

        sample_rate = getattr(self.provider, 'sample_rate', 24000)
        total_video_duration = max(c.end for c in cues) + 0.25
        # Initialize full audio timeline buffer with silence
        # A 132-minute timeline occupies ~724 MiB as float32. Keep it on disk
        # rather than allocating that buffer in RAM alongside OCR/TTS models.
        timeline_store = tempfile.TemporaryFile(dir=vo_dir)
        timeline_samples = np.memmap(timeline_store, dtype=np.float32, mode='w+', shape=(max(1, int(total_video_duration * sample_rate)),))

        try:
            timeline_meta = []
            max_drift = 0.0
            drifts = []
            last_voice_end = 0.0
            abort_tts = threading.Event()
            def continue_tts():
                return not abort_tts.is_set() and (should_continue is None or should_continue())

            cache_miss = object()
            key_locks = {}
            key_locks_guard = threading.Lock()
            budgets = {c.index: {'attempts': settings.TTS_MAX_RETRIES,
                                'seconds': settings.TTS_TOTAL_DEADLINE_SECONDS} for c in cues}
            def synthesize_one(cue: Cue, cache_only=False):
                if not continue_tts():
                    raise TaskCancelled("TTS cancelled")
                text = spoken_text(cue)
                if not text or not re.search(r"[\w\dÀ-ỹa-zA-Z]", text):
                    return None
                normalized = normalize_vietnamese_text(text)
                if not normalized or not re.search(r"[\w\dÀ-ỹa-zA-Z]", normalized):
                    return None
                cache_key = self.cache.compute_key(
                    provider=self.provider.name,
                    voice=base_voice or (settings.EDGE_TTS_VOICE if self.provider.name == "edge" else "default"),
                    normalized_text=normalized,
                    rate=cue.tts_rate or rate,
                    pitch=pitch,
                    sample_rate=sample_rate,
                    model_version=getattr(self.provider, 'cache_version', 'v1'),
                )
                # Serialize identical cache misses, not different utterances.
                with key_locks_guard:
                    key_lock = key_locks.setdefault(cache_key, threading.Lock())
                while not key_lock.acquire(timeout=.1):
                    if not continue_tts():
                        raise TaskCancelled('TTS cancelled waiting for identical audio')
                try:
                    raw_audio = self.cache.get(cache_key)
                    def valid_audio(payload):
                        if not payload:
                            return False
                        try:
                            samples, sr = sf.read(io.BytesIO(payload), dtype='float32')
                            return sr > 0 and samples.size > 0 and bool(np.all(np.isfinite(samples))) and float(np.max(np.abs(samples))) >= 1e-5
                        except (ValueError, RuntimeError, OSError):
                            return False
                    if raw_audio and not valid_audio(raw_audio):
                        logger.warning('Invalid cached audio; regenerating cue %s', cue.index)
                        raw_audio = None
                    if not raw_audio:
                        if cache_only:
                            return cache_miss
                        options = {'should_continue': continue_tts} if isinstance(self.provider, EdgeTTSProvider) or getattr(self.provider, 'supports_cancel', False) else {}
                        budget = budgets[cue.index]
                        if isinstance(self.provider, EdgeTTSProvider):
                            attempt = settings.TTS_MAX_RETRIES-budget['attempts']
                            if budget['attempts'] <= 0 or budget['seconds'] <= 0:
                                raise EdgeSynthesisError('Cue hết ngân sách TTS; cache được giữ.', retryable=False)
                            budget['attempts'] -= 1
                            options.update(max_attempts=1, total_deadline_seconds=budget['seconds'],
                                           boundary='WordBoundary' if attempt % 2 else 'SentenceBoundary')
                        started = time.monotonic()
                        try:
                            raw_audio, _ = self.provider.synthesize(normalized, voice=base_voice, rate=cue.tts_rate or rate, pitch=pitch, **options)
                        finally:
                            budget['seconds'] -= time.monotonic()-started
                        if raw_audio:
                            if not valid_audio(raw_audio):
                                raise PipelineException(SystemErrorCodes.TTS_FAILED, f'Cue {cue.index} trả audio hỏng hoặc im lặng; không lưu cache.')
                            check_disk()
                            self.cache.put(cache_key, raw_audio)
                            check_disk()
                    # Keep only the durable identity in the queue, not every WAV.
                    # This bounds live audio memory by worker count rather than film length.
                    return cache_key if raw_audio else None
                finally:
                    key_lock.release()

            synthesized: Dict[int, Optional[str]] = {}
            workers = max(1, min(self.max_concurrency or int(getattr(settings, "TTS_CONCURRENCY", 8)), total_cues))
            workers = min(workers, getattr(self.provider, 'max_concurrency', workers))
            failed_indices = []
            retried_indices = set()
            failures = []
            completed = 0
            failure_run = 0
            # Inspect every matching cache before spending time on network requests.
            missing = []
            for idx, cue in enumerate(cues):
                cached = synthesize_one(cue, cache_only=True)
                if cached is cache_miss:
                    missing.append((idx, cue))
                else:
                    synthesized[idx] = cached
                    completed += 1
            # A partial matching cache is a resume. Start conservatively rather
            # than immediately repeating the failed concurrent service wave.
            serial_resume = isinstance(self.provider, EdgeTTSProvider) and bool(synthesized) and bool(missing)
            def report_progress():
                if progress_callback:
                    progress_callback(int(len(synthesized) / total_cues * 70),
                        f'Giọng hợp lệ {len(synthesized)}/{total_cues}; lỗi {len(failed_indices)}; '
                        f'lần gọi lỗi {len(failures)}; chờ thử lại {sum(idx in retried_indices for idx, _ in work)}; '
                        f'luồng {active_workers}; bộ đọc {self.provider.name}'
                        + (f"; lỗi gần nhất {' -> '.join(failures[-1]['error_chain'])}" if failures else '')
                        + (f" (HTTP {failures[-1]['status']})" if failures and 'status' in failures[-1] else ''))
            active_workers = 1 if serial_resume else workers
            healthy_run = 0
            work = deque(missing)
            report_progress()
            cooldown_until = 0.0
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tts") as pool:
                pending = {}
                def submit_next():
                    item = work.popleft() if work else None
                    if item is not None:
                        idx, cue = item
                        pending[pool.submit(synthesize_one, cue)] = idx
                for _ in range(active_workers):
                    submit_next()
                while pending or work:
                    check_disk()
                    if should_continue and not should_continue():
                        abort_tts.set()
                        for future in pending:
                            future.cancel()
                        raise TaskCancelled('TTS cancelled')
                    ready, _ = wait(pending, timeout=.25, return_when=FIRST_COMPLETED) if pending else (set(), set())
                    for future in ready:
                        idx = pending.pop(future)
                        try:
                            synthesized[idx] = future.result()
                            failure_run = 0
                            healthy_run += 1
                            if healthy_run >= 4 and active_workers < workers:
                                active_workers += 1
                                healthy_run = 0
                        except TaskCancelled:
                            raise
                        except Exception as exc:
                            if isinstance(exc,TaskInterrupted) or storage_reason(exc):
                                abort_tts.set()
                                for waiting in pending:waiting.cancel()
                                raise
                            failure = _provider_failure(cues[idx].index, exc)
                            failures.append(failure)
                            cause = ' → '.join(failure['error_chain'])
                            logger.warning('TTS cue %s failed: %s', cues[idx].index, cause)
                            failure_run += 1
                            healthy_run = 0
                            active_workers = max(1, active_workers // 2)
                            budget = budgets[cues[idx].index]
                            retryable = isinstance(exc, EdgeSynthesisError) and exc.retryable
                            if retryable and budget['attempts'] > 0 and budget['seconds'] > 0:
                                work.append((idx, cues[idx]))
                                retried_indices.add(idx)
                                cooldown_until = time.monotonic()+min(2.0, .4*failure_run)
                            else:
                                failed_indices.append(idx)
                            stop_after = max(3, workers)*(settings.TTS_MAX_RETRIES if retryable else 1)
                            if failure_run >= stop_after:
                                abort_tts.set()
                                for queued in pending:
                                    queued.cancel()
                                raise PipelineException(SystemErrorCodes.TTS_FAILED, f'Dịch vụ TTS lỗi liên tiếp; đã có {len(synthesized)}/{total_cues} giọng hợp lệ. Cache được giữ; chạy tiếp để xử lý phần còn thiếu. Lỗi dịch vụ: {cause}.', details={'retryable': False, 'cue_index': cues[idx].index, 'cue_indices': sorted({f['cue_index'] for f in failures}), 'failures': failures}) from exc
                        completed += 1
                        report_progress()
                    if work and time.monotonic() < cooldown_until:
                        abort_tts.wait(min(.1, cooldown_until-time.monotonic()))
                        continue
                    while len(pending) < active_workers:
                        previous = len(pending)
                        submit_next()
                        if len(pending) == previous:
                            break
            # Retry only isolated failures after the concurrent wave has drained.
            # Healthy cached cues never need a second provider call.
            for idx in failed_indices:
                # Edge already exhausts its own bounded request/deadline budget.
                # Do not multiply that budget by another sequential recovery wave.
                if isinstance(self.provider, EdgeTTSProvider):
                    raise PipelineException(SystemErrorCodes.TTS_FAILED,
                        f"Đã có {len(synthesized)}/{total_cues} giọng hợp lệ; {len(failed_indices)} câu lỗi. Chạy tiếp để tạo phần còn thiếu. Lỗi dịch vụ: {' → '.join(failures[-1]['error_chain'])}.",
                        details={'retryable': False, 'cue_indices': [cues[i].index for i in failed_indices], 'failures': failures})
                if progress_callback:
                    progress_callback(70, f'Thử lại riêng cue {cues[idx].index} sau lỗi TTS')
                try:
                    synthesized[idx] = synthesize_one(cues[idx])
                except TaskCancelled:
                    raise
                except Exception as exc:
                    if isinstance(exc,TaskInterrupted) or storage_reason(exc):raise
                    raise PipelineException(SystemErrorCodes.TTS_FAILED, f'Cue {cues[idx].index} vẫn lỗi TTS sau lượt phục hồi riêng: {exc}', details={'retryable': True, 'cue_index': cues[idx].index}) from exc

            for idx, cue in enumerate(cues):
                check_disk()
                if should_continue and not should_continue():
                    raise TaskCancelled("TTS cancelled")

                text = spoken_text(cue)
                slot_duration = cue.end - cue.start
                cache_key = synthesized.get(idx)
                raw_audio = self.cache.get(cache_key) if cache_key else None
                if cache_key:
                    try:
                        data, sr = sf.read(io.BytesIO(raw_audio or b''), dtype='float32')
                        valid = sr > 0 and data.size > 0 and bool(np.all(np.isfinite(data))) and float(np.max(np.abs(data))) >= 1e-5
                    except (ValueError, RuntimeError, OSError):
                        valid = False
                    if not valid:
                        raise PipelineException(SystemErrorCodes.TTS_FAILED, f'Cue {cue.index}: cache audio bị mất hoặc hỏng trước khi ghép; hãy chạy lại tạo giọng.')
                if not raw_audio or not text:
                    if text and re.search(r'[\wÀ-ỹ]', text):
                        raise PipelineException(SystemErrorCodes.UNKNOWN_ERROR, f'Cue {cue.index} thiếu âm thanh TTS; hãy tạo lại giọng.')
                    # Write a silent audio clip so cue clip path exists if user previews it
                    clip_file = clips_dir / f"clip_{cue.index:04d}.wav"
                    sf.write(str(clip_file), np.zeros(int(sample_rate * 0.1), dtype=np.float32), sample_rate, format="WAV")
                    cue.voice_audio_path = f"voiceover/clips/{clip_file.name}"
                    cue.voice_duration = 0.0
                    continue

                # Audio was decoded and validated once above.
                del raw_audio
                if len(data.shape) > 1:
                    data = data.mean(axis=1)  # convert to mono
                if sr != sample_rate:
                    # Resample if needed
                    import scipy.signal
                    num_samps = int(len(data) * sample_rate / sr)
                    data = scipy.signal.resample(data, num_samps)

                if getattr(settings, "TTS_TRIM_SILENCE", True) and data.size:
                    peak = float(np.max(np.abs(data)))
                    if peak > 0:
                        absolute_floor = float(getattr(settings, "TTS_SILENCE_FLOOR", 300)) / 32768.0
                        data = trim_rms(data, sample_rate, absolute_floor)

                clip_duration = len(data) / sample_rate
                raw_duration = clip_duration
                if not data.size or float(np.max(np.abs(data))) < 1e-5:
                    raise PipelineException(SystemErrorCodes.TTS_FAILED, f'Cue {cue.index} chỉ có âm thanh im lặng.')

                # A preceding long cue may have consumed part of this window.
                # Fit against the remaining deadline, preserving nonoverlap.
                nominal_start = max(0.0, cue.start-self.policy.lead)
                actual_start = max(nominal_start, last_voice_end + (self.policy.pause if timeline_meta else 0))
                # A 333ms caption really has 333ms, not a fabricated 500ms.
                # The old floor under-compressed short clips and propagated
                # their avoidable delay into every following sentence.
                deadline = self.policy.deadline(cues, idx)
                remaining_slot = max(1 / sample_rate, deadline-actual_start)
                tempo_limit = min(self.max_atempo, self.policy.max_speed/rate_multiplier(cue.tts_rate or rate))
                tempo_limit = max(1.0, math.floor((tempo_limit+1e-9)*1000)/1000)
                tempo = 1.0
                if clip_duration > remaining_slot:
                    tempo = min(tempo_limit, clip_duration / remaining_slot)
                    if tempo > 1.02:
                        # Apply atempo
                        wav_io = io.BytesIO()
                        sf.write(wav_io, data, sample_rate, format="WAV")
                        adjusted_wav = apply_atempo(wav_io.getvalue(), tempo)
                        data, _ = sf.read(io.BytesIO(adjusted_wav), dtype="float32")
                        if len(data.shape) > 1:
                            data = data.mean(axis=1)
                        clip_duration = len(data) / sample_rate

                data = apply_color(data, sample_rate, color)
                # Save clip file
                clip_file = clips_dir / f"clip_{cue.index:04d}.wav"
                sf.write(str(clip_file), data, sample_rate, format="WAV")

                # Place into full timeline
                # Never mix two spoken sentences over one another. Carry delay
                # into the next available gap and report it as real drift.
                start_sample = int(round(actual_start * sample_rate))
                end_sample = start_sample + len(data)
                last_voice_end = end_sample / sample_rate

                if end_sample > len(timeline_samples):
                    # Expand buffer
                    timeline_samples.flush()
                    timeline_samples._mmap.close()
                    timeline_store.truncate(end_sample * np.dtype(np.float32).itemsize)
                    timeline_samples = np.memmap(timeline_store, dtype=np.float32, mode='r+', shape=(end_sample,))

                # Mix into timeline
                timeline_samples[start_sample:end_sample] += data

                drift = max(0.0, last_voice_end - cue.end, abs(actual_start - cue.start))
                drifts.append(drift)
                max_drift = max(max_drift, drift)

                cue.voice_audio_path = f"voiceover/clips/{clip_file.name}"
                cue.voice_duration = round(clip_duration, 2)

                timeline_meta.append({
                    "cue_index": cue.index,
                    "start": round(cue.start, 3),
                    "actual_start": round(actual_start, 3),
                    "actual_end": round(last_voice_end, 3),
                    "end": round(cue.end, 3),
                    "slot_duration": round(slot_duration, 3),
                    "clip_duration": round(clip_duration, 3),
                    "raw_duration": round(raw_duration, 3),
                    "spoken_text": spoken_text(cue),
                    "scene_label_excluded": spoken_text(cue) != (cue.vi_text or '').strip(),
                    "tempo": round(tempo, 3),
                    "tempo_limit": tempo_limit,
                    "fit_window": round(deadline-nominal_start, 3),
                    "deadline": round(deadline, 3),
                    "synthesis_rate": cue.tts_rate or rate,
                    "drift": round(drift, 3),
                    "clip_path": cue.voice_audio_path,
                })

                if progress_callback:
                    pct = 70 + int((idx + 1) / total_cues * 29)
                    progress_callback(pct, f"Đã ghép giọng {idx + 1}/{total_cues}; chờ kiểm tra timing")

            # Normalize audio volume to prevent clipping
            block_size = sample_rate * 30
            max_peak = max(float(np.max(np.abs(timeline_samples[start:start + block_size]))) for start in range(0, len(timeline_samples), block_size))
            if max_peak > 0.95:
                for start in range(0, len(timeline_samples), block_size):
                    timeline_samples[start:start + block_size] *= 0.95 / max_peak

            # Write master voiceover.wav
            vo_wav_path = vo_dir / "voiceover.wav"
            sf.write(str(vo_wav_path), timeline_samples, sample_rate, format="WAV")
            total_samples = len(timeline_samples)
        finally:
            timeline_samples._mmap.close()
            timeline_store.close()

        # Write timeline.json
        (vo_dir / "timeline.json").write_text(json.dumps(timeline_meta, indent=2), encoding="utf-8")

        # Write vi.dub.srt with updated timing
        aligned = {row['cue_index']: row for row in timeline_meta}
        dubbed_cues = [cue.model_copy(update={'start': aligned[cue.index]['actual_start'], 'end': aligned[cue.index]['actual_end']})
                      if cue.index in aligned else cue for cue in cues]
        (vo_dir / "vi.dub.srt").write_text(cues_to_srt(dubbed_cues, lang="vi"), encoding="utf-8")
        transcript_dir = output_dir / "transcript"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        (transcript_dir / "cues.json").write_text(
            json.dumps([cue.model_dump() for cue in cues], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        mean_drift = float(np.mean(drifts)) if drifts else 0.0

        return {
            "timeline": timeline_meta,
            "voiceover_wav": str(vo_wav_path),
            "total_duration": round(total_samples / sample_rate, 2),
            "max_drift": round(max_drift, 3),
            "mean_drift": round(mean_drift, 3),
            "retried_cues": [cues[idx].index for idx in sorted(set(failed_indices) | retried_indices)],
        }

