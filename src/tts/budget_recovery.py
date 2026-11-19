"""Production recovery from measured, unaccelerated clips; no text edits here."""
import hashlib
import io
import json
import math
from pathlib import Path

import numpy as np
import soundfile as sf

from src.ocr.base import cues_to_srt
from src.translation.memory import atomic_json
from src.tts.cluster_schedule import MeasuredUtterance, solve_measured, source_clusters
from src.tts.policy import rate_multiplier
from src.tts.timeline import apply_atempo as _default_apply_atempo, apply_atempo_batch

apply_atempo = _default_apply_atempo

VERSION = 'cluster-budget-v1'


def _tempo_for_speed(total_speed,base):
    tempo=math.floor((min(total_speed,1.3)/base+1e-9)*1000)/1000
    remainder=tempo;factors=[]
    while remainder>1.3+.00001:
        factors.append(1.3);remainder/=1.3
    factors.append(math.floor((remainder+1e-9)*1000)/1000)
    return math.prod(factors),factors


def verified_source_gaps(project_dir, source_sha256, sample_rate, cues=None, *, speech_windows=False):
    """A gain setting or old VAD selector is not source timing permission."""
    path = Path(project_dir)/'source/timing_evidence.json'
    if not path.is_file():
        return {} if speech_windows else []
    record = json.loads(path.read_text(encoding='utf8'))
    if record.get('version') != 1 or record.get('source_sha256') != source_sha256:
        raise ValueError('Source timing evidence is stale or unsupported')
    intervals = {} if speech_windows else []
    known={c.index:c for c in (cues or [])}
    protected_path=Path(project_dir)/'source/protected_audio.json'
    protected=json.loads(protected_path.read_text(encoding='utf8')).get('intervals',[]) if protected_path.is_file() else []
    for item in record.get('intervals',[]):
        is_turn=item.get('kind')=='single_speaker_turn'
        if is_turn != speech_windows:continue
        if not ((item.get('speech_absent') is True if not is_turn else item.get('single_speaker') is True)
                and item.get('image_checked') is True and item.get('protected_audio_absent') is True
                and item.get('evidence_sha256')):
            continue
        # Evidence bytes must be project-local and unchanged. This record is
        # not automatically generated from volume, cue spacing or filenames.
        evidence = (Path(project_dir)/item['evidence_path']).resolve()
        if not evidence.is_relative_to(Path(project_dir).resolve()):
            raise ValueError('Source timing evidence escaped project')
        if hashlib.sha256(evidence.read_bytes()).hexdigest()!=item['evidence_sha256']:
            raise ValueError('Source timing evidence hash mismatch')
        if is_turn:
            from src.tts.source_windows import accepted_cues,assets_match
            proof=json.loads(evidence.read_text(encoding='utf8'))
            if proof.get('identity',{}).get('source_sha256')!=source_sha256 or not assets_match(project_dir,proof):
                continue
            allowed=set(accepted_cues(proof.get('observations',[]),list(known.values())))
        else:
            allowed=set()
        start,end = item['start'],item['end']
        if not all(type(x) in (int,float) and math.isfinite(x) for x in (start,end)) or not 0<=start<end:
            raise ValueError('Invalid source timing interval')
        if is_turn:
            observed=proof.get('observed_range',[])
            if (not isinstance(observed,list) or len(observed)!=2
                    or not all(type(x) in (int,float) and math.isfinite(x) for x in observed)
                    or not observed[0]<=start<end<=observed[1]):
                continue
        if any(a<end and start<b for a,b in protected):continue
        span=(math.ceil(start*sample_rate),math.floor(end*sample_rate))
        if is_turn:
            for binding in item.get('cue_bindings',[]):
                cue=known.get(binding.get('cue_id'))
                if cue and cue.index in allowed and binding==dict(cue_id=cue.index,start=cue.start,end=cue.end,text=cue.effective_source_text):
                    intervals.setdefault(cue.index,[]).append(span)
        else:
            intervals.append(span)
    return intervals


def recover_measured(staging, project_dir, cues, timeline, pause=.04,
                     source_sha256=None, source_duration_seconds=None,
                     should_continue=None, progress_callback=None,
                     sample_rate=None, total_frames=None):
    """Schedule first, then measured tempo steps; preserve raw text and voices.

    The caller alone may shorten text after this returns an unresolved report.
    Journals/variant WAVs survive retries, scoped to matching measured inputs.
    """
    from src.jobs.logger import TaskCancelled
    root,project = Path(staging).resolve(),Path(project_dir).resolve()
    master = root/'voiceover/voiceover.wav'
    if master.is_file() and master.stat().st_size > 0:
        info = sf.info(master);sr=info.samplerate
        master_frames=info.frames
    elif sample_rate and total_frames:
        sr=int(sample_rate)
        master_frames=int(total_frames)
    else:
        raise ValueError('Missing master audio metadata for budget recovery')
    duration_frames=master_frames
    if source_duration_seconds is not None:
        if not math.isfinite(source_duration_seconds) or source_duration_seconds<=0:
            raise ValueError('Invalid actual source duration')
        duration_frames=min(duration_frames,math.floor(source_duration_seconds*sr))
    known={c.index:c for c in cues}
    journal_dir=project/'audit/voice_budget';wave_dir=journal_dir/'waves'
    wave_dir.mkdir(parents=True,exist_ok=True)
    gaps=verified_source_gaps(project,source_sha256,sr,cues)
    speech_windows=verified_source_gaps(project,source_sha256,sr,cues,speech_windows=True)
    entries=[]
    for row in timeline:
        cue=known[row['cue_index']]
        path=(root/row['clip_path']).resolve()
        if not path.is_relative_to(root):raise ValueError('Measured clip escaped staging')
        data=path.read_bytes();audio,rate=sf.read(io.BytesIO(data),dtype='float32')
        if rate!=sr or audio.ndim!=1 or not len(audio) or not np.isfinite(audio).all() or not np.any(audio):
            raise ValueError('Invalid measured source clip')
        entries.append(dict(row=row,cue=cue,path=path,sha=hashlib.sha256(data).hexdigest(),
                            raw_frames=len(audio),frames=len(audio),base=rate_multiplier(row['synthesis_rate']),
                            tempo=1.,selected=path,attempts=[]))
    units=lambda: [MeasuredUtterance(e['cue'].index,round(e['cue'].start*sr),
                    round(e['cue'].end*sr),e['frames']) for e in entries]
    initial_units=units()
    blocks=source_clusters(initial_units,gaps,math.ceil(pause*sr))
    block_of={u.cue_id:i for i,block in enumerate(blocks) for u in block}
    identity=dict(version=VERSION,source_sha256=source_sha256,sample_rate=sr,
        duration_frames=duration_frames,pause=pause,verified_gaps=gaps,verified_speech_windows=speech_windows,
        cues=[(e['cue'].index,e['cue'].start,e['cue'].end,e['cue'].vi_text,
               e['sha'],e['base']) for e in entries])
    digest=hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    journal_path=journal_dir/(digest+'.json')
    report=dict(version=VERSION,input_hash=digest,source_sha256=source_sha256,
                verified_gap_count=len(gaps),verified_speech_window_cues=len(speech_windows),
                phases=[],clusters=[],status='running')
    evidence_file=project/'source/timing_evidence.json'
    report['timing_evidence_sha256']=(hashlib.sha256(evidence_file.read_bytes()).hexdigest()
                                     if evidence_file.is_file() else None)
    protected_file=project/'source/protected_audio.json'
    report['protected_audio_sha256']=(hashlib.sha256(protected_file.read_bytes()).hexdigest()
                                     if protected_file.is_file() else None)
    # Persist successful original placements in independent, unchanged blocks.
    fixed={}
    identity_by_id={row[0]:row for row in identity['cues']}
    for block in blocks:
        signature=hashlib.sha256(json.dumps(dict(version=VERSION,pause=pause,gaps=gaps,
            source=source_sha256,cues=[identity_by_id[u.cue_id] for u in block]),
            sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        saved=journal_dir/('block_'+signature+'.json')
        if saved.is_file():
            old=json.loads(saved.read_text(encoding='utf8'))
            if old.get('status')=='original_complete' and old.get('input_hash')==signature:
                fixed.update({int(k):v for k,v in old['starts'].items()})
                continue
        previous=-math.ceil(pause*sr);starts={};drifts=[]
        for unit in block:
            start=max(unit.start,previous+math.ceil(pause*sr));end=start+unit.frames
            if end>unit.end:break
            starts[unit.cue_id]=start;drifts.append((start-unit.start)/sr);previous=end
        if len(starts)==len(block) and max(drifts,default=0)<=1.5 and sum(drifts)/len(block)<=.3:
            fixed.update(starts)
            atomic_json(saved,dict(status='original_complete',input_hash=signature,starts=starts))

    def check():
        if should_continue and not should_continue():raise TaskCancelled('Voice budget recovery cancelled')

    def apply_tempo_variants(pairs):
        pending = []
        ready = []
        for entry, total_speed in pairs:
            check()
            tempo, factors = _tempo_for_speed(total_speed, entry['base'])
            if tempo <= entry['tempo'] + 1e-8:
                continue
            key = hashlib.sha256(f'{VERSION}:{entry["sha"]}:{tempo:.9f}'.encode()).hexdigest()
            path = wave_dir / (key + '.wav'); proof = wave_dir / (key + '.json')
            hit = False
            if path.is_file() and proof.is_file():
                record = json.loads(proof.read_text(encoding='utf8'))
                hit = record.get('sha256') == hashlib.sha256(path.read_bytes()).hexdigest()
            item = dict(entry=entry, tempo=tempo, factors=factors, path=path, proof=proof, hit=hit)
            ready.append(item)
            if not hit:
                pending.append(item)
        if pending:
            if apply_atempo is _default_apply_atempo and len(pending) > 1:
                stretched = apply_atempo_batch([(item['entry']['path'].read_bytes(), item['factors']) for item in pending])
            else:
                stretched = []
                for item in pending:
                    data = item['entry']['path'].read_bytes()
                    for factor in item['factors']:
                        data = apply_atempo(data, factor, force=True)
                    stretched.append(data)
            for item, data in zip(pending, stretched):
                temp = item['path'].with_suffix('.tmp'); temp.write_bytes(data); temp.replace(item['path'])
                atomic_json(item['proof'], dict(sha256=hashlib.sha256(data).hexdigest(), tempo=item['tempo'],
                                                raw_sha256=item['entry']['sha'], version=VERSION))
        changed_any = False
        for item in ready:
            entry = item['entry']
            audio, rate = sf.read(item['path'], dtype='float32')
            if rate != sr or audio.ndim != 1 or not len(audio) or not np.isfinite(audio).all() or not np.any(audio):
                raise ValueError('Tempo variant is not valid measured speech')
            entry.update(frames=len(audio), tempo=item['tempo'], selected=item['path'])
            entry['attempts'].append(dict(total_speed=entry['base'] * item['tempo'], tempo=item['tempo'],
                                          measured_seconds=len(audio) / sr, cache_hit=item['hit']))
            changed_any = True
        return changed_any

    def tempo_variant(entry, total_speed):
        return apply_tempo_variants([(entry, total_speed)])

    solution=None
    for phase in range(5):
        check()
        solution=solve_measured(units(),sr,duration_frames,math.ceil(pause*sr),gaps,
                                fixed_starts=fixed,verified_windows=speech_windows)
        report['phases'].append(dict(step='schedule' if phase==0 else 'tempo_then_schedule',
                                     round=phase,result=solution))
        report['status']=solution['status'];atomic_json(journal_path,report)
        if solution['status']=='candidate_meets_timing':break
        if phase==4:break
        failed={x['cue_id'] for x in solution.get('conflicts',[])}
        targets=[e for e in entries if e['cue'].index not in fixed
                 and (not failed or e['cue'].index in failed)]
        batch_pairs = []
        for entry in targets:
            levels=[v for v in (1.15,1.20,1.25,1.30)
                    if _tempo_for_speed(v,entry['base'])[0]>entry['tempo']+1e-8]
            if levels:
                batch_pairs.append((entry, levels[0]))
        changed = apply_tempo_variants(batch_pairs) if batch_pairs else False
        # Without reliable speaker IDs use a conservative neighboring-voice
        # bound within each block. Never smooth across a confirmed reset.
        for _ in range(len(entries)):
            check()
            smoothed=False
            for left,right in zip(entries,entries[1:]):
                check()
                if block_of[left['cue'].index]!=block_of[right['cue'].index]:continue
                fast,slow=sorted((left,right),key=lambda e:e['base']*e['tempo'],reverse=True)
                if fast['base']*fast['tempo']-slow['base']*slow['tempo']>.10001 and slow['cue'].index not in fixed:
                    wanted=fast['base']*fast['tempo']-.1
                    total=min(1.3,math.ceil((wanted-1e-7)*20)/20)
                    if slow['base']*_tempo_for_speed(total,slow['base'])[0]<wanted-1e-8:
                        total=min(1.3,total+.05)
                    smoothed=tempo_variant(slow,total) or smoothed
            changed=changed or smoothed
            if not smoothed:break
        if progress_callback:progress_callback(86,f'Phục hồi WAV theo cụm: bậc tốc độ {phase+1}/4')
        if not changed:break

    updated=[]
    placement={r['cue_id']:r for r in solution.get('placements',[])}
    # A failure in another block does not invalidate an independently verified
    # original block. Retain its measured placement in diagnostics and cache;
    # the complete master still cannot be published until global success.
    for unit in initial_units:
        if unit.cue_id in fixed and unit.cue_id not in placement:
            start=fixed[unit.cue_id];end=start+unit.frames
            placement[unit.cue_id]=dict(cue_id=unit.cue_id,start=start,end=end,
                drift=max(abs(start-unit.start),end-unit.end,0)/sr)
    conflicts={r['cue_id']:r for r in solution.get('conflicts',[])}
    legal_windows={r['cue_id']:r for r in solution.get('windows',[])}
    for entry in entries:
        row=dict(entry['row']);cue=entry['cue'];placed=placement.get(cue.index)
        window=legal_windows.get(cue.index,dict(start=round(cue.start*sr),end=round(cue.end*sr)))
        row.update(clip_duration=entry['frames']/sr,raw_duration=entry['raw_frames']/sr,
                   tempo=entry['tempo'],tempo_limit=math.floor((1.3/entry['base']+1e-9)*1000)/1000,
                   fit_window=(window['end']-window['start'])/sr,deadline=window['end']/sr,
                   schedule_policy=VERSION,schedule_status='placed' if placed else 'unplaced',
                   can_nghe_kiem=entry['base']*entry['tempo']>1.2+.001,
                   candidate_clip_path=str(entry['selected']))
        if placed:
            row.update(actual_start=placed['start']/sr,actual_end=placed['end']/sr,drift=placed['drift'])
            borrowed=[]
            for side,a,b in (('before',placed['start'],round(cue.start*sr)),
                             ('after',round(cue.end*sr),placed['end'])):
                if a>=b:continue
                kind=('verified_silence' if any(x<=a and b<=y for x,y in gaps)
                      else 'verified_same_speaker')
                borrowed.append(dict(side=side,start=a/sr,end=b/sr,
                    milliseconds=(b-a)*1000/sr,source_audio=kind))
            row['borrowed_intervals']=borrowed
        else:
            # These are diagnostic source anchors, never publishable starts.
            row.update(actual_start=cue.start,actual_end=cue.start+entry['frames']/sr,
                       drift=max(0,entry['frames']/sr-(cue.end-cue.start)),
                       unplaced_reason=conflicts.get(cue.index,{}).get('reason','order_or_pause_or_qa'))
        updated.append(row)
    entry_by_id={e['cue'].index:e for e in entries}
    updated_by_id={row['cue_index']:row for row in updated}
    for i,block in enumerate(blocks):
        check()
        ids=[u.cue_id for u in block]
        block_entries=[entry_by_id[cue_id] for cue_id in ids]
        left,right=block[0].start,block[-1].end
        for a,b in gaps:
            if a<left<=b:left=max(a,block[0].start-math.floor(.8*sr))
            if a<=right<b:right=min(b,block[-1].end+math.floor(.8*sr))
        needed=(sum(e['frames'] for e in block_entries)+(len(block)-1)*math.ceil(pause*sr))/sr
        report['clusters'].append(dict(cum_id=f'c-{ids[0]}-{ids[-1]}',cau=ids,
            trang_thai='dat' if all(x in placement for x in ids) else 'chua_dat',
            ngan_sach=dict(co_san_s=(right-left)/sr,can_s=needed,
                           thieu_tai_it_nhat_s=max(0,needed-(right-left)/sr),
                           loai='span_upper_bound_not_feasibility'),
            conflicts=[conflicts[x] for x in ids if x in conflicts],
            attempts={str(e['cue'].index):e['attempts'] for e in block_entries if e['attempts']},
            kept_original=[x for x in ids if x in fixed]))
        report['clusters'][-1]['muon_ngoai_cua_so']=[dict(cue_id=row['cue_index'],**span)
            for row in (updated_by_id[cue_id] for cue_id in ids)
            for span in row.get('borrowed_intervals',[])]
    report['status']=solution['status'];report['timeline']=updated
    atomic_json(journal_path,report)
    if solution['status']!='candidate_meets_timing':
        return dict(solution,timeline=updated,report=report,journal=str(journal_path))
    # No audio/clip publication before every unit has a complete legal place.
    target=master.with_name('voiceover.budget.wav')
    max_placed_end=max((r['end'] for r in placement.values()),default=0)
    assemble_frames=max(master_frames,max_placed_end)
    with sf.SoundFile(target,'w',samplerate=sr,channels=1,subtype='PCM_16') as stream:
        silence=np.zeros(sr*30,dtype=np.float32);remaining=assemble_frames
        while remaining:
            count=min(remaining,len(silence));stream.write(silence[:count]);remaining-=count
        for entry,row in zip(entries,updated):
            data,_=sf.read(entry['selected'],dtype='float32')
            stream.seek(round(row['actual_start']*sr));stream.write(data)
    for entry,row in zip(entries,updated):
        if entry['selected']!=entry['path']:entry['path'].write_bytes(entry['selected'].read_bytes())
        entry['cue'].voice_duration=row['clip_duration']
    target.replace(master)
    atomic_json(root/'voiceover/timeline.json',updated)
    atomic_json(root/'voiceover/budget_report.json',report)
    aligned={r['cue_index']:r for r in updated}
    dubbed=[c.model_copy(update={'start':aligned[c.index]['actual_start'],
                                'end':aligned[c.index]['actual_end']}) if c.index in aligned else c for c in cues]
    (root/'voiceover/vi.dub.srt').write_text(cues_to_srt(dubbed,lang='vi'),encoding='utf8')
    return dict(solution,timeline=updated,report=report,journal=str(journal_path))
