"""Two-model, source-bound review before borrowing across caption boundaries.

This checks one-speaker source turns, not silence. Approved turns never create
silence/reset anchors. Provider errors or disagreement grant no permission.
"""
import hashlib
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.translation.memory import atomic_json

VERSION='source-window-v1'
PROMPT=(
    'Kiểm tra cửa sổ NGUỒN để căn lồng tiếng; không dịch và không sửa mốc. '
    'Nghe audio độc lập rồi chép spoken_zh, không dùng chữ ảnh để tự điền tiếng. '
    'Kiểm tất cả ảnh full/crop. Chỉ cho phép nếu toàn cửa sổ có đúng MỘT người nói '
    'nhất quán, không thoại người khác/nền, không âm thanh quan trọng cần giữ. '
    'Phân biệt lời kể narration với hội thoại dialogue. Nếu dialogue thì ảnh phải '
    'xác nhận người đang nói nhất quán; không chắc thì từ chối. '
    'Cho allowed_cue_ids chỉ khi nghe rõ đầy đủ câu nguồn đó trong cửa sổ. '
    'Trả JSON: spoken_zh(string),spoken_speakers(int),consistent_speaker(bool),'
    'other_dialogue(bool),protected_audio(bool),uncertain(bool),visual_checked(bool),'
    'visual_single_speaker(bool),audio_role(dialogue/narration/unknown),'
    'allowed_cue_ids(list int),reason(string).')


def normalized_source(text):
    return ''.join(re.findall(r'[\u3400-\u9fff0-9]',text or ''))


def covered_cues(votes,cues):
    for vote in votes:
        if not isinstance(vote,dict):return []
        if not (type(vote.get('spoken_speakers')) is int and vote['spoken_speakers']==1
                and vote.get('consistent_speaker') is True and vote.get('other_dialogue') is False
                and vote.get('protected_audio') is False and vote.get('uncertain') is False
                and vote.get('visual_checked') is True
                and vote.get('audio_role') in ('dialogue','narration')
                and isinstance(vote.get('spoken_zh'),str)
                and isinstance(vote.get('allowed_cue_ids'),list)
                and all(type(i) is int for i in vote['allowed_cue_ids'])):
            return []
        if vote['audio_role']=='dialogue' and vote.get('visual_single_speaker') is not True:return []
    if len({v['audio_role'] for v in votes})!=1:return []
    result=[]
    for cue in cues:
        text=normalized_source(cue.effective_source_text)
        if (len(text)>=2 and all(cue.index in vote['allowed_cue_ids']
                and text in normalized_source(vote['spoken_zh']) for vote in votes)):
            result.append(cue.index)
    return result


def accepted_cues(observations,cues):
    if len(observations)!=2 or any(o.get('status')!='observed' for o in observations):return []
    models=[o.get('actual_model') for o in observations]
    if not all(isinstance(m,str) and m for m in models) or len(set(models))!=2:return []
    return covered_cues([o.get('vote') for o in observations],cues)


def assets_match(project,record):
    assets=record.get('assets',[])
    if len(assets)<4:return False
    root=Path(project).resolve()
    for asset in assets:
        path=(root/asset['path']).resolve()
        if (not path.is_relative_to(root) or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest()!=asset['sha256']):return False
    return True


def independent_votes(parts,models,project_id,cues,request,should_continue=None,fallback_models=(),accept_one=None,prior_observation=None,recovery_deadline=None):
    """At most four real calls and one shared60s deadline. One refusal suffices;
    permission still requires two independently accepted actual model IDs."""
    deadline=min(time.monotonic()+60, recovery_deadline if recovery_deadline is not None else float("inf"))
    pending=list(dict.fromkeys(list(models)+list(fallback_models)))
    selected=[];attempts=[]
    while (pending or prior_observation) and len(attempts)<4 and len(selected)<2 and time.monotonic()<deadline:
        reused = prior_observation is not None
        if reused:
            observation = prior_observation
            prior_observation = None
        else:
            model=pending.pop(0)
            observation=request(parts,model,project_id,should_continue,deadline=deadline)
            attempts.append(observation)
        if observation.get('status')!='observed':continue
        if not isinstance(observation.get('actual_model'),str) or not observation['actual_model']:continue
        vote=observation.get('vote')
        if accept_one is None:
            required={'spoken_zh','spoken_speakers','consistent_speaker','other_dialogue',
                      'protected_audio','uncertain','visual_checked','audio_role','allowed_cue_ids'}
            if not isinstance(vote,dict) or not required.issubset(vote):continue
            accepted=bool(covered_cues([vote],cues))
        else:
            accepted=accept_one(vote)
            if accepted is None:continue
        # A single observation may reject, never approve a source permission.
        if not accepted:
            if reused:
                continue
            return dict(observations=[observation],attempts=attempts,complete_rejection=True)
        if any(o.get('actual_model')==observation.get('actual_model') for o in selected):continue
        selected.append(observation)
        if reused:
            pending = [model for model in pending if model not in
                       (observation.get('requested_model'), observation['actual_model'])]
    return dict(observations=selected,attempts=attempts,complete_rejection=False)


def select_source_windows(windows, limit):
    """Prefer windows covering the most blocked cues within the Auto review budget."""
    ranked = sorted(windows, key=lambda window: (-len(window[2]), window[1] - window[0], window[0]))
    return sorted(ranked[:limit], key=lambda window: window[0])


def review_source_windows(runner,project_id,cues,affected_ids,source_sha256,models,
                          should_continue=None,progress_callback=None,attempted=None,recovery_deadline=None):
    import cv2
    import subprocess
    import io
    import soundfile as sf
    from google.genai import types
    from src.jobs.logger import TaskCancelled
    from src.pipeline.render import get_ffmpeg_bin
    from src.ocr.caption_role_review import request_vote
    from src.ocr.source_recheck import source_pixel_rect
    from config.settings import settings

    if len(set(models))!=2:return 0
    project=runner.get_project_dir(project_id)
    source=runner.get_source_video_path(project_id)
    roi=runner.get_roi_set(project_id).get_primary_subtitle_roi()
    stat=source.stat()
    attempted=attempted if attempted is not None else set()
    selected=[c for c in cues if c.index in set(affected_ids)]
    protected_file=project/'source/protected_audio.json'
    protected=json.loads(protected_file.read_text(encoding='utf8')).get('intervals',[]) if protected_file.is_file() else []
    if any(not isinstance(span,list) or len(span)!=2
           or not all(type(x) in (int,float) and math.isfinite(x) for x in span)
           or not 0<=span[0]<span[1] for span in protected):
        raise ValueError('Invalid protected source-audio intervals')
    windows=[]
    for cue in sorted(selected,key=lambda c:c.start):
        left=max(0.,cue.start-.8);right=cue.end+.8
        if windows and left<=windows[-1][1] and right-windows[-1][0]<=8:
            windows[-1][1]=max(windows[-1][1],right);windows[-1][2].append(cue)
        else:windows.append([left,right,[cue]])
    total_windows = len(windows)
    windows = select_source_windows(windows, settings.BUDGET_SOURCE_WINDOW_LIMIT)
    if progress_callback and len(windows) < total_windows:
        progress_callback(87, f'Đối chiếu {len(windows)}/{total_windows} đoạn nguồn ưu tiên cho lỗi timing')
    root=project/'audit/source_windows';root.mkdir(parents=True,exist_ok=True)
    prompt_sha=hashlib.sha256(PROMPT.encode()).hexdigest()

    def check():
        if should_continue and not should_continue():raise TaskCancelled('Source-window review cancelled')

    def process(window):
        check();left,right,targets=window
        if any(a<right and left<b for a,b in protected):return None
        bindings=[dict(cue_id=c.index,start=c.start,end=c.end,text=c.effective_source_text) for c in targets]
        identity=dict(version=VERSION,source_sha256=source_sha256,start=left,end=right,
                      cue_bindings=bindings,roi=roi.model_dump() if roi else None,
                      models=models,prompt_sha256=prompt_sha)
        key=hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        if key in attempted:return None
        attempted.add(key)
        folder=root/key;folder.mkdir(exist_ok=True);record_file=folder/'review.json'
        observations=None;old_attempts=[];prior=None;saved=None
        if record_file.is_file():
            saved=json.loads(record_file.read_text(encoding='utf8'))
            old_attempts=saved.get('attempts',[])
            if (saved.get('identity')==identity and assets_match(project,saved)
                    and (saved.get('complete_rejection') is True or
                         all(x.get('status')=='observed' for x in saved.get('observations',[]))
                         and len(saved.get('observations',[]))==2)):
                observations=saved['observations']
            elif (saved.get('identity')==identity and assets_match(project,saved)
                  and len(saved.get('observations',[]))==1):
                prior=saved['observations'][0]
        if observations is None:
            cap=cv2.VideoCapture(str(source));parts=[types.Part.from_text(text=PROMPT+'\n'+json.dumps(identity,ensure_ascii=False))]
            assets=[]
            try:
                fps=cap.get(cv2.CAP_PROP_FPS)
                if not cap.isOpened() or not math.isfinite(fps) or fps<=0:return None
                duration=cap.get(cv2.CAP_PROP_FRAME_COUNT)/fps
                right=min(right,duration)
                if right<=left:return None
                for number,fraction in enumerate((.1,.5,.9)):
                    frame_id=math.floor((left+(right-left)*fraction)*fps)
                    cap.set(cv2.CAP_PROP_POS_FRAMES,frame_id);ok,image=cap.read()
                    if not ok or round(cap.get(cv2.CAP_PROP_POS_FRAMES))-1!=frame_id:return None
                    variants=[('full',image)]
                    if roi:
                        x,y,w,h=source_pixel_rect(roi,image.shape[1],image.shape[0])
                        variants.append(('crop',image[y:y+h,x:x+w]))
                    for kind,pixels in variants:
                        if kind=='full' and pixels.shape[1]>1280:
                            pixels=cv2.resize(pixels,(1280,round(pixels.shape[0]*1280/pixels.shape[1])))
                        ok,encoded=cv2.imencode('.jpg',pixels)
                        if not ok:return None
                        payload=encoded.tobytes();path=folder/f'{number}_{kind}.jpg';path.write_bytes(payload)
                        assets.append(dict(path=path.relative_to(project).as_posix(),sha256=hashlib.sha256(payload).hexdigest(),frame_id=frame_id))
                        parts.append(types.Part.from_bytes(data=payload,mime_type='image/jpeg'))
            finally:cap.release()
            check()
            audio=subprocess.run([str(get_ffmpeg_bin()),'-nostdin','-v','error','-ss',str(left),
                '-i',str(source),'-t',str(right-left),'-vn','-ac','1','-ar','16000','-c:a','pcm_s16le',
                '-f','wav','pipe:1'],capture_output=True,timeout=30,check=True).stdout
            if len(audio)<=44:return None
            audio_info=sf.info(io.BytesIO(audio))
            if audio_info.samplerate!=16000 or audio_info.channels!=1 or audio_info.frames<=0:return None
            observed_range=[left,min(right,left+audio_info.frames/16000)]
            audio_path=folder/'source.wav';audio_path.write_bytes(audio)
            assets.append(dict(path=audio_path.relative_to(project).as_posix(),sha256=hashlib.sha256(audio).hexdigest()))
            parts.append(types.Part.from_bytes(data=audio,mime_type='audio/wav'))
            selection=independent_votes(parts,models,project_id,targets,request_vote,should_continue,
                fallback_models=[m.strip() for m in settings.TRANSLATION_FALLBACK_MODELS.split(',') if m.strip()],
                recovery_deadline=recovery_deadline, prior_observation=prior if prior and saved.get('assets')==assets
                and saved.get('observed_range')==observed_range else None)
            observations=selection['observations']
            old_attempts.append(selection['attempts'])
            atomic_json(record_file,dict(identity=identity,observations=observations,
                                         attempts=old_attempts,assets=assets,observed_range=observed_range,
                                         complete_rejection=selection['complete_rejection']))
        ids=accepted_cues(observations,targets)
        if not ids:return None
        persisted=json.loads(record_file.read_text(encoding='utf8'))
        observed=persisted.get('observed_range')
        if not isinstance(observed,list) or len(observed)!=2:return None
        return dict(kind='single_speaker_turn',start=observed[0],end=observed[1],single_speaker=True,
            image_checked=True,protected_audio_absent=True,
            cue_bindings=[b for b in bindings if b['cue_id'] in ids],
            evidence_path=record_file.relative_to(project).as_posix(),
            evidence_sha256=hashlib.sha256(record_file.read_bytes()).hexdigest())

    intervals=[]
    with ThreadPoolExecutor(max_workers=2,thread_name_prefix='source-window') as pool:
        for number,result in enumerate(pool.map(process,windows),1):
            if result:intervals.append(result)
            if progress_callback:progress_callback(87,f'Đối chiếu cửa sổ nguồn {number}/{len(windows)} cho câu còn vướng')
    check()
    current=source.stat()
    if (current.st_size,current.st_mtime_ns)!=(stat.st_size,stat.st_mtime_ns):
        raise ValueError('Source changed during speech-window review')
    with source.open('rb') as stream:
        if hashlib.file_digest(stream,'sha256').hexdigest()!=source_sha256:
            raise ValueError('Source hash changed during speech-window review')
    if not intervals:return 0
    target=project/'source/timing_evidence.json'
    existing=json.loads(target.read_text(encoding='utf8')) if target.is_file() else {}
    old=existing.get('intervals',[]) if existing.get('source_sha256')==source_sha256 else []
    known={i.get('evidence_sha256') for i in old}
    added=[i for i in intervals if i['evidence_sha256'] not in known]
    if not added:return 0
    atomic_json(target,dict(version=1,source_sha256=source_sha256,intervals=old+added))
    return len(added)
