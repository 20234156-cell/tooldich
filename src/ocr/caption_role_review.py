"""Bounded opt-in hybrid review; provider failures preserve all speech."""
import hashlib
import json
import math
import subprocess
import time
import uuid
from pathlib import Path

import cv2
import soundfile as sf

from config.settings import settings
from src.jobs.logger import TaskCancelled, mask_secrets
from src.ocr.caption_roles import VERSION, binding, digest, select_candidates, validate_vote, verified_lyrics

PROMPT = (
    'Phân biệt nguồn lời nói và chữ. Ba ảnh full chỉ là ngữ cảnh; ba crop sau đó là DUY NHẤT '
    'dòng caption cần đọc. Audio chỉ thuộc đúng khoảng caption. Đọc độc lập từng crop, '
    'không gộp chữ khác trên full frame. Nghe độc lập, không dùng caption tự điền audio. '
    'Nếu có thoại nói dù kèm nhạc: không chọn lyrics. Chỉ subtitle_only khi chắc là lời '
    'bài hát đang được hát và không có thoại nói. Chữ thuật lại hành động không phải lyrics. '
    'Trả JSON: frame_reads(3string),visual_role(dialogue/lyrics/explanation/scene_text/unknown),'
    'audio_kind(speech/singing/music/nonverbal/unknown),has_spoken_dialogue(bool),spoken_zh(string,'
    'để trống nếu không nghe được thoại nói),decision(speak/subtitle_only/uncertain),'
    'uncertain(bool),reason(string ngắn).')


def _save(path, data):
    from src.pipeline.orchestrator import _atomic_text
    _atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False))


def _check(should_continue):
    if should_continue and not should_continue():
        raise TaskCancelled()


def _audio(video, path, start=None, duration=None):
    from src.pipeline.render import get_ffmpeg_bin
    command = [str(get_ffmpeg_bin()), '-nostdin', '-v', 'error', '-y']
    if start is not None:
        command += ['-ss', str(start)]
    command += ['-i', str(video)]
    if duration is not None:
        command += ['-t', str(duration)]
    command += ['-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(path)]
    subprocess.run(command, check=True, capture_output=True, timeout=300)


def speech_segments(video, directory, should_continue=None):
    """Bounded memory; only complete current-source/runtime/model cache is reusable."""
    from faster_whisper.vad import get_speech_timestamps, VadOptions
    from faster_whisper.utils import get_assets_path
    from importlib.metadata import version
    from src.ocr.learning import video_identity
    identity = dict(source=video_identity(video, fresh=True), version=VERSION,
                    implementation=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    runtime=version('faster-whisper'), threshold=settings.WHISPER_VAD_THRESHOLD,
                    min_silence=settings.WHISPER_VAD_MIN_SILENCE_MS,
                    pad=settings.WHISPER_VAD_SPEECH_PAD_MS,
                    chunk_seconds=60, overlap_seconds=.5)
    model = Path(get_assets_path()) / 'silero_vad_v6.onnx'
    if not model.is_file():
        model = Path(get_assets_path()) / 'silero_vad_v5.onnx'
    identity['model'] = hashlib.sha256(model.read_bytes()).hexdigest()
    cache = directory / 'vad.json'
    try:
        saved = json.loads(cache.read_text(encoding='utf-8'))
        if (saved['completed'] is True and saved['identity'] == identity
                and type(saved['frames']) is int and saved['frames'] > 0
                and saved['processed_frames'] == saved['frames']
                and saved['duration'] == saved['frames']/16000
                and saved['sha256'] == digest(saved['segments'])
                and all(len(s) == 2 and all(type(x) in (int,float) and math.isfinite(x) for x in s)
                        and 0 <= s[0] < s[1] <= saved['duration'] for s in saved['segments'])):
            return saved['segments'], identity
    except (OSError, ValueError, KeyError, TypeError):
        pass
    _check(should_continue)
    wav = directory / f'source_vad_{uuid.uuid4().hex[:12]}.wav'
    _audio(video, wav)
    segments = []
    with sf.SoundFile(wav) as audio:
        if audio.samplerate != 16000 or audio.channels != 1 or not audio.frames:
            raise ValueError('Invalid VAD audio')
        duration = audio.frames/16000
        frames = audio.frames
        for start in range(0, audio.frames, 60*16000):
            _check(should_continue)
            left = max(0, start-8000)
            audio.seek(left)
            stop = min(audio.frames,start+60*16000+8000)
            samples = audio.read(stop-left, dtype='float32')
            if len(samples) != stop-left:
                raise ValueError('Incomplete VAD audio read')
            spans = get_speech_timestamps(samples, VadOptions(
                threshold=identity['threshold'], min_silence_duration_ms=identity['min_silence'],
                speech_pad_ms=identity['pad']), sampling_rate=16000)
            segments.extend([(r['start']+left)/16000, (r['end']+left)/16000] for r in spans)
    _check(should_continue)
    if video_identity(video, fresh=True) != identity['source']:
        raise ValueError('Source changed during VAD')
    _save(cache, dict(completed=True, identity=identity, duration=duration,
                      frames=frames,processed_frames=frames,
                      segments=segments, sha256=digest(segments)))
    wav.unlink(missing_ok=True)
    return segments, identity


def request_vote(parts, model, project_id, should_continue=None, *, deadline=None):
    from google import genai
    from google.genai import types
    from src.ai.key_pool import key_pool
    from src.ai.usage import generate_logged, UsagePersistenceError
    key = None
    started = time.monotonic()
    def finish(row):
        return dict(row,seconds=time.monotonic()-started)
    try:
        _check(should_continue)
        key = key_pool.get_next_key(model=model)
        if key is None:
            return finish(dict(requested_model=model, status='failed', error='No available provider key'))
        if deadline is not None and time.monotonic()>=deadline:
            return finish(dict(requested_model=model,status='failed',error='Source review deadline exhausted'))
        key_pool.wait_translation_turn(key.id, min(started+30,deadline) if deadline is not None else started+30,
                                       should_continue, model=model)
        remaining=deadline-time.monotonic() if deadline is not None else 25.
        if remaining<=.05:
            return finish(dict(requested_model=model,status='failed',error='Source review deadline exhausted'))
        with genai.Client(api_key=key.key, http_options=types.HttpOptions(
                timeout=max(1,int(min(25.,remaining)*1000)), retry_options=types.HttpRetryOptions(attempts=1))) as client:
            response = generate_logged(client.models.generate_content, key=key, model=model,
                budget={}, video_id=project_id, contents=parts,
                config=types.GenerateContentConfig(temperature=0, response_mime_type='application/json',
                    max_output_tokens=1536, automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
        key_pool.record_success(key.id, (time.monotonic()-started)*1000, model=model)
        return finish(dict(requested_model=model, status='observed', actual_model=response.model_version,
                    vote=json.loads(response.text)))
    except (TaskCancelled, UsagePersistenceError):
        raise
    except Exception as exc:
        error = mask_secrets(str(exc))
        if key:
            error = error.replace(key.key, '***')
            key_pool.record_failure(key.id, getattr(exc, 'code', 503) or 503, error, model=model)
        return finish(dict(requested_model=model, status='failed', error=error))


def review(video, roi, cues, directory, engine, project_id, should_continue=None, cache_dir=None):
    """At most 8 candidates/12 provider calls; return copies, never write the DB."""
    from google.genai import types
    from src.ocr.learning import video_identity
    from src.ocr.source_recheck import source_pixel_rect
    from src.ocr.frame_reader import FrameReader
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(cache_dir) if cache_dir else directory
    cache_dir.mkdir(parents=True,exist_ok=True)
    segments, identity = speech_segments(video, cache_dir, should_continue)
    selected, deferred = select_candidates(cues, segments)
    report = dict(version=VERSION, source_sha256=identity['source'], vad_identity=identity,
                  selected=selected, deferred=deferred, decisions=[], resolved=0, provider_calls=0)
    after = [c.model_copy(deep=True) for c in cues]
    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS)
    reader = FrameReader(cap, lambda: _check(should_continue))
    models = list(dict.fromkeys([settings.TRANSLATION_MODEL]+[
        m.strip() for m in settings.TRANSLATION_FALLBACK_MODELS.split(',') if m.strip()]))[:4]
    deadline = time.monotonic()+120
    try:
        for candidate in selected:
            _check(should_continue)
            cue = next(c for c in after if c.index == candidate['cue_id'])
            decision = dict(cue_id=cue.index, status='pending', visual=[], votes=[])
            report['decisions'].append(decision)
            if report['provider_calls'] >= 12 or time.monotonic() >= deadline:
                decision['reason'] = 'provider_budget_exhausted'
                continue
            fulls, crops = [], []
            for fraction in (.25, .5, .75):
                _check(should_continue)
                wanted = math.floor((cue.start+cue.duration*fraction)*fps)
                ok, image = reader.read(wanted)
                actual = int(cap.get(cv2.CAP_PROP_POS_FRAMES))-1
                seconds = cap.get(cv2.CAP_PROP_POS_MSEC)/1000
                if not ok or image is None or actual != wanted or not cue.start <= seconds < cue.end:
                    break
                x,y,w,h = source_pixel_rect(roi, image.shape[1], image.shape[0])
                crop = image[y:y+h,x:x+w]
                if not crop.size:
                    break
                data = cv2.imencode('.png',crop)[1].tobytes()
                decision['visual'].append(dict(frame=actual, seconds=seconds,
                    image_sha256=hashlib.sha256(data).hexdigest(),
                    readings=[engine.read_boxes(crop,preprocess=p) for p in (False,True)]))
                fulls.append(cv2.imencode('.jpg',image)[1].tobytes())
                crops.append(data)
            visual = decision['visual']
            if (len(visual) != 3 or len({f['frame'] for f in visual}) != 3
                    or any(len(rows) != 1 or rows[0]['text'].strip() != cue.effective_source_text
                           or rows[0]['confidence'] < 90 for f in visual for rows in f['readings'])):
                decision['reason'] = 'visual_consensus_not_met'
                continue
            wav = directory / f'cue_{cue.index}.wav'
            _audio(video, wav, cue.start, cue.duration)
            parts = [types.Part.from_text(text=PROMPT)]
            parts += [types.Part.from_bytes(data=b,mime_type='image/jpeg') for b in fulls]
            parts.append(types.Part.from_text(text='BA CROP CAPTION CẦN ĐỌC:'))
            parts += [types.Part.from_bytes(data=b,mime_type='image/png') for b in crops]
            parts.append(types.Part.from_bytes(data=wav.read_bytes(),mime_type='audio/wav'))
            for model in models:
                if report['provider_calls'] >= 12 or time.monotonic() >= deadline:
                    break
                vote = request_vote(parts,model,project_id,should_continue)
                report['provider_calls'] += 1
                decision['votes'].append(vote)
                verdict = validate_vote(vote,cue.effective_source_text)
                _save(directory/'report.json',report)
                if verdict == 'speech':
                    decision.update(status='speech', reason='spoken_dialogue_present')
                    break
                if vote['status'] == 'observed' and verdict != 'lyrics':
                    decision['reason'] = 'ambiguous_or_conflicting_vote'
                    break
                proof = dict(version=VERSION, binding=binding(cue), source_sha256=identity['source'],
                    audio_sha256=hashlib.sha256(wav.read_bytes()).hexdigest(), visual=visual,
                    votes=decision['votes'])
                proof['sha256'] = digest(proof)
                if verified_lyrics(cue,proof):
                    cue.caption_role = 'background_lyrics'
                    cue.caption_role_proof = proof
                    # Existing AI shortening may have removed lyric content. It
                    # must be translated afresh; never overwrite manual content.
                    cue.vi_text = None
                    cue.voice_audio_path = None
                    cue.voice_duration = None
                    decision.update(status='lyrics', reason='two_model_source_bound_consensus')
                    report['resolved'] += 1
                    break
            _save(directory/'report.json',report)
        _check(should_continue)
        if video_identity(video, fresh=True) != identity['source']:
            raise ValueError('Source changed during caption review')
        report['completed'] = True
        _save(directory/'report.json',report)
        return after, report
    finally:
        cap.release()
