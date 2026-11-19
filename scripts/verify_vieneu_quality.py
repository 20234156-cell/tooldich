"""Offline quality screening of existing benchmark WAVs; no synthesis/default edits."""
import argparse
import hashlib
import html
import json
from pathlib import Path
import secrets
import shutil
import time

import benchmark_vieneu as baseline
from benchmark_vieneu_batches import word_error_rate


def compare_transcripts(expected, reference, candidate):
    old=word_error_rate(expected,reference)
    new=word_error_rate(expected,candidate)
    return {'baseline_wer':old,'candidate_wer':new,
            'pair_wer':word_error_rate(reference,candidate),
            'candidate_higher_wer':new>old+1e-9,
            'candidate_different_when_baseline_matches':old==0 and new>0,
            'interpretation':'screening_only_not_confirmed_tts_regression'}


def duration_rows(cases, references, candidates):
    import statistics
    results=[]
    for case in cases:
        cue_id=case['id']
        ref=[next(i['audio_seconds'] for i in r['items'] if i['id']==cue_id) for r in references]
        cand=[next(i['audio_seconds'] for i in r['items'] if i['id']==cue_id) for r in candidates]
        center=statistics.median(ref)
        deltas=[v-center for v in cand]
        worst=max(deltas,key=abs)
        results.append({'id':cue_id,'text':case['text'],'category':case['category'],
                        'baseline_seconds':ref,'candidate_seconds':cand,
                        'baseline_median_seconds':center,
                        'worst_signed_delta_seconds':worst,
                        'worst_absolute_delta_seconds':abs(worst),
                        'worst_relative_delta':worst/center})
    return results


def reconstructed_groups(normalization, size=8, max_chars=1200):
    order=sorted(range(len(normalization)),key=lambda i:len(normalization[i]['text']))
    groups=[]
    while order:
        group=[]
        chars=0
        while order and len(group)<size and (not group or chars+len(normalization[order[0]]['text'])<=max_chars):
            i=order.pop(0)
            entry=normalization[i]
            group.append({'chunk_index':i,'id':entry['id'],'text':entry['text'],
                          'chars':len(entry['text']),'words':len(entry['text'].split())})
            chars+=len(entry['text'])
        groups.append(group)
    return groups


def audit_cue_join(output,candidate_path,cue_id):
    import io
    import numpy as np
    import soundfile as sf
    from src.tts.vieneu_worker import split_text
    corpus,_=baseline.load_corpus(baseline.BASE/'tests/fixtures/vieneu_benchmark.json')
    case=next(c for c in corpus['cases'] if c['id']==cue_id)
    summary=json.loads(candidate_path.read_text(encoding='utf-8'))
    results=[]
    for row in summary['rounds']:
        entries=[(i,e) for i,e in enumerate(row['normalization']) if e['id']==cue_id]
        if [e['text'] for i,e in entries]!=split_text(case['text']):
            raise ValueError('Recorded chunk text/order differs from corpus')
        folder=candidate_path.parent/f"batch_{row['batch_size']}_round_{row['round']:02d}"
        parts=[]
        for n,(i,e) in enumerate(entries):
            wav,rate=sf.read(folder/'chunks'/f'{i:04d}.wav',dtype='float32')
            if rate!=48000 or wav.ndim!=1 or not len(wav): raise ValueError('Invalid chunk')
            if n: parts.append(np.zeros(3840,dtype=np.float32))
            parts.append(wav)
        audio=np.concatenate(parts)
        audio*=min(1.,.95/max(float(np.max(np.abs(audio))),1e-9))
        buffer=io.BytesIO()
        sf.write(buffer,audio,48000,format='WAV',subtype='PCM_16')
        published=(folder/(cue_id+'.wav')).read_bytes()
        expected_sha=next(i['sha256'] for i in row['items'] if i['id']==cue_id)
        if hashlib.sha256(published).hexdigest()!=expected_sha: raise ValueError('Published WAV checksum mismatch')
        identical=buffer.getvalue()==published
        results.append({'batch_size':row['batch_size'],'round':row['round'],
                        'chunks':[{'global_chunk_index':i,'cue_chunk_index':n,'text':e['text']} for n,(i,e) in enumerate(entries)],
                        'rebuilt_wav_identical':identical})
        if not identical: raise ValueError('Cue rebuild differs from published WAV')
    report={'id':cue_id,'text':case['text'],'results':results,
            'interpretation':'Actual chunk WAVs reassembled by recorded source index; verifies order/join, not pronunciation quality.'}
    (output/(cue_id+'_join.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=[f'Kiểm tra ghép đoạn {cue_id}','',case['text'],'',
           'Tách theo dấu câu bằng hàm split_text hiện tại; đối chiếu với danh sách chuẩn hóa đã lưu. Cùng cue_id nhưng mỗi đoạn có chỉ số nguồn riêng.','',
           '| Batch | Lượt | Chỉ số nguồn theo thứ tự | WAV dựng lại khớp byte |','|---:|---:|---|---|']
    for row in results:
        lines.append(f"| {row['batch_size']} | {row['round']} | "+' → '.join(str(c['global_chunk_index']) for c in row['chunks'])+' | Có |')
    lines+=['','Các đoạn theo thứ tự:']
    for c in results[0]['chunks']:
        lines.append(f"- Đoạn {c['cue_chunk_index']}, chỉ số nguồn {c['global_chunk_index']}: {c['text']}")
    lines+=['','Đã đọc WAV FLOAT đoạn thật, nối theo chỉ số nguồn với khoảng nghỉ0,08s, giữ chuẩn hóa đỉnh và PCM16 như benchmark. Khớp toàn bộ byte với WAV đã xuất. Xác minh việc ghép; chưa chứng minh phát âm đúng và không thay thế nghe.','']
    (output/(cue_id+'_JOIN.md')).write_text('\n'.join(lines),encoding='utf-8')
    return report


def audit_repeatability(output, reference_path, candidate_path):
    reference=json.loads(reference_path.read_text(encoding='utf-8'))
    candidate=json.loads(candidate_path.read_text(encoding='utf-8'))
    if reference['corpus_sha256']!=candidate['corpus_sha256']:
        raise ValueError('Corpus mismatch')
    seeds={r.get('seed') for r in reference['rounds']}
    if len(seeds)!=1 or None in seeds:
        raise ValueError('Reference seed metadata missing or inconsistent')
    seed=next(iter(seeds))
    for filename in ['benchmark_vieneu.py','benchmark_vieneu_batches.py']:
        source=(Path(__file__).parent/filename).read_text(encoding='utf-8')
        if f'torch.manual_seed({seed})' not in source or f'np.random.seed({seed})' not in source:
            raise ValueError('Current seed code differs from reference metadata; inspect historical run before claiming fixed seed')
    report={'branches':{},'seed':seed,'seed_source':'Baseline seed metadata plus current benchmark source; historical worker source hash was not recorded.',
            'interpretation':'Fixed-seed repeated WAVs are not independent stochastic controls. No natural-noise flip rate established.',
            'batch_group_provenance':'Reconstructed from recorded normalization entries and current benchmark grouping code; no historical per-call batch trace was saved.'}
    for name,rows,root,pattern in [('baseline',reference['rounds'],reference_path.parent,'round_{round:02d}'),
                                  ('batch8',[r for r in candidate['rounds'] if r['batch_size']==8],candidate_path.parent,'batch_8_round_{round:02d}')]:
        if len(rows)!=3 or any(not r['technical_passed'] for r in rows):
            raise ValueError('Require three successful rounds')
        ids=[i['id'] for i in rows[0]['items']]
        if len(ids)!=40 or len(set(ids))!=40 or any({i['id'] for i in r['items']}!=set(ids) or len(r['items'])!=40 for r in rows):
            raise ValueError('Incomplete/duplicate round IDs')
        items=[]
        for cue_id in ids:
            hashes=[]
            for row in rows:
                item=next(i for i in row['items'] if i['id']==cue_id)
                path=root/pattern.format(round=row['round'])/(cue_id+'.wav')
                digest=hashlib.sha256(path.read_bytes()).hexdigest()
                if digest!=item['sha256']:
                    raise ValueError('Audio checksum mismatch: '+str(path))
                hashes.append(digest)
            items.append({'id':cue_id,'sha256_by_round':hashes,'unique_audio_count':len(set(hashes))})
        report['branches'][name]={'checked_files':len(items)*3,'identical_cue_count':sum(i['unique_audio_count']==1 for i in items),'items':items}
    row=next(r for r in candidate['rounds'] if r['batch_size']==8)
    groups=reconstructed_groups(row['normalization'])
    report['short_case_groups']=[{'group_number':n,'members':g} for n,g in enumerate(groups,1) if any(i['id'] in ['s02','s05','s06'] for i in g)]
    (output/'repeatability.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['Kiểm tra ba lượt và nhóm câu ngắn','',
           f'Seed cố định{seed} theo metadata baseline và mã benchmark đang có. Đã đọc lại checksum240 file thực.','']
    for name,branch in report['branches'].items():
        lines.append(f"- {name}: {branch['checked_files']} file; {branch['identical_cue_count']}/40 câu giống hệt byte ở cả3 lượt.")
    lines+=['','Không coi0 khác biệt trong bộ fixed-seed này là tỷ lệ nhiễu tự nhiên bằng0. Không có đối chứng sinh độc lập bằng các seed khác. Chạy lại ASR trên bản sao WAV không bổ sung bằng chứng về nhiễu TTS. Cùng seed nhưng thứ tự câu và cách gom batch khác nhau không bảo đảm mỗi câu nhận cùng chuỗi số ngẫu nhiên.','',
            'Các nhóm dưới đây được dựng lại từ danh sách chuẩn hóa đã lưu và thuật toán gom nhóm của benchmark; không phải log thực thi từng batch đã ghi tại thời điểm chạy. Ngoài gom theo độ dài ký tự, thư viện còn xếp theo độ dài phoneme bên trong nhóm.']
    for group in report['short_case_groups']:
        lines+=['',f"Nhóm {group['group_number']}:",'','| ID | Từ | Ký tự | Văn bản |','|---|---:|---:|---|']
        for i in group['members']:
            lines.append(f"| {i['id']} | {i['words']} | {i['chars']} | {i['text']} |")
    lines+=['','Chưa xác nhận padding gây lỗi. Sai khác ASR lặp trên cùng WAV cũng chưa phân biệt được lỗi ASR và lỗi TTS. Chênh theo bội0,08s không chứng minh không mất khung âm thanh hoặc mất chữ. Phân bố9 câu WER tăng/8 giảm chưa loại trừ ảnh hưởng riêng ở câu ngắn.','']
    (output/'REPEATABILITY.md').write_text('\n'.join(lines),encoding='utf-8')
    return report


def enhance_listening_form(page):
    if 'id="reviewer"' in page:
        return page
    help_text='''<div id="review_protocol"><p><b>Cụt cuối:</b> âm cuối bị cắt hoặc câu dừng đột ngột trước khi đọc hết. <b>Đổi giọng:</b> âm sắc thay đổi rõ giữa các đoạn trong cùng một câu.</p>
<p>Ghi mốc nghi vấn theo thời gian của từng bản A/B, ví dụ 0:01.20–0:01.60, kèm từ nghe sai. Sáu mẫu nghi vấn cần người thứ hai nghe lại hoặc cùng người nghe ở một buổi khác trước khi mở khóa. Hai phiếu bất đồng thì để chưa kết luận.</p>
<label>Người nghe / mã người nghe: <input id="reviewer" type="text"></label><label>Lượt/buổi nghe: <input id="session" type="text" placeholder="lượt 1 hoặc lượt 2"></label></div>'''
    page=page.replace('</h1>','</h1>'+help_text,1)
    for label in ['A','B']:
        extra='<p><label>Mốc nghi vấn '+label+': <input name="'+label+'_timecodes" type="text" placeholder="0:01.20–0:01.60; từ nghe sai"></label><label>Kết luận '+label+': <select name="'+label+'_decision"><option value="unresolved">Chưa kết luận</option><option value="no_issue_detected">Không phát hiện lỗi</option><option value="issue_detected">Có lỗi, xem ghi chú</option></select></label></p>'
        page=page.replace('<p>Bản '+label+'</p>','<p>Bản '+label+'</p>'+extra)
    page=page.replace("const data={status:","const data={schema_version:2,reviewer:document.getElementById('reviewer').value,session:document.getElementById('session').value,status:")
    page=page.replace("s.querySelectorAll('input,textarea')","s.querySelectorAll('input,textarea,select')")
    return page


def upgrade_listening_forms(output):
    import re
    upgraded=[]
    for name in ['blind_review','blind_review_asr']:
        path=output/name/'index.html'
        if not path.exists(): continue
        before=path.read_text(encoding='utf-8')
        after=enhance_listening_form(before)
        if re.findall(r'<audio[^>]*src="([^"]+)"',before)!=re.findall(r'<audio[^>]*src="([^"]+)"',after):
            raise ValueError('Refusing to alter blind audio mapping')
        path.write_text(after,encoding='utf-8')
        upgraded.append(str(path))
    return upgraded


def write_blind_review(output, items, selected, name='blind_review'):
    if name not in ['blind_review','blind_review_asr']:
        raise ValueError('Unsupported review directory')
    folder=output/name
    folder.mkdir()
    cards=[]
    key=[]
    for number,cue_id in enumerate(selected,1):
        item=next(i for i in items if i['id']==cue_id)
        roles=['baseline','candidate']
        secrets.SystemRandom().shuffle(roles)
        sources={}
        players=[]
        for label,role in zip(['A','B'],roles):
            filename=secrets.token_hex(12)+'.wav'
            source=Path(item[role+'_path'])
            shutil.copyfile(source,folder/filename)
            sources[label]={'role':role,'sha256':item[role+'_sha256'],'file':filename}
            flags=''.join('<label><input type="checkbox" name="'+label+'_'+field+'">'+title+'</label> ' for field,title in [('listened','Đã nghe'),('cut_tail','Cụt cuối câu'),('voice_change','Đổi giọng'),('pronunciation','Sai tên/từ'),('odd_rhythm','Nhịp lạ')])
            players.append('<p>Bản '+label+'</p><audio controls preload="none" src="'+filename+'"></audio><p>'+flags+'</p>')
        key.append({'sample':number,'cue_id':cue_id,'labels':sources})
        cards.append('<section data-sample="'+str(number)+'"><h2>Mẫu '+str(number)+'</h2><p>'+html.escape(item['text'])+'</p>'+''.join(players)+'<label>Ghi chú: <textarea name="notes"></textarea></label></section>')
    page='''<!doctype html><html lang="vi"><meta charset="utf-8"><title>Nghe mù giọng đọc</title>
<style>body{max-width:850px;margin:24px auto;padding:12px;font:18px sans-serif}section{border-top:1px solid #aaa;padding:16px 0}audio,textarea{width:100%}label{display:inline-block;margin:5px}button{padding:14px}</style>
<h1>Nghe mù 10 cặp giọng đọc</h1><p>Hai bản A/B đã được đảo ngẫu nhiên từng cặp. Nghe đủ cả hai bản, đánh dấu lỗi cụ thể. Chưa biết bản nào dùng batch lớn. Chưa nghe không được tính là đạt.</p>
'''+''.join(cards)+'''<button id="save">Tải phiếu đánh giá</button><p id="status"></p>
<script>document.getElementById('save').onclick=()=>{const samples=Array.from(document.querySelectorAll('section')).map(s=>({sample:Number(s.dataset.sample),ratings:Object.fromEntries(Array.from(s.querySelectorAll('input,textarea')).map(e=>[e.name,e.type==='checkbox'?e.checked:e.value]))}));const data={status:'human_ratings_require_validation',created_at:new Date().toISOString(),samples};const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));a.download='blind_ratings.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);document.getElementById('status').textContent='Đã xuất phiếu; chưa tự kết luận đạt chất lượng.';};</script></html>'''
    (folder/'index.html').write_text(enhance_listening_form(page),encoding='utf-8')
    key_path=output/('blind_key.json' if name=='blind_review' else name+'_key.json')
    key_path.write_text(json.dumps(key,ensure_ascii=False,indent=2),encoding='utf-8')


def select_asr_review(report):
    flagged=[i for i in report['items'] if i.get('comparison',{}).get('candidate_different_when_baseline_matches')]
    flagged.sort(key=lambda i:i['comparison']['candidate_wer']-i['comparison']['baseline_wer'],reverse=True)
    ordered=[i['id'] for i in flagged]+['n04','n01','p01','l01','t03']+[r['id'] for r in report['duration_rank_by_relative']]
    return list(dict.fromkeys(ordered))[:10]


def prepare_asr_blind(output):
    path=output/'verification.json'
    report=json.loads(path.read_text(encoding='utf-8'))
    if report['asr_status']!='completed_diagnostic_only' or report['asr_completed_pairs']!=40:
        raise ValueError('Require all40 ASR pairs before prioritizing listening')
    selected=select_asr_review(report)
    for item in report['items']:
        if item['id'] not in selected: continue
        for role in ['baseline','candidate']:
            if hashlib.sha256(Path(item[role+'_path']).read_bytes()).hexdigest()!=item[role+'_sha256']:
                raise ValueError('Audio changed since verification')
    write_blind_review(output,report['items'],selected,name='blind_review_asr')
    report['asr_prioritized_blind_sample_ids']=selected
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    write_text_report(output,report)
    return selected


def write_text_report(output,report):
    def clean(text):
        return str(text).replace('|','/').replace('\n',' ')
    comparisons=[i for i in report['items'] if 'comparison' in i]
    higher=[i for i in comparisons if i['comparison']['candidate_higher_wer']]
    new=[i for i in comparisons if i['comparison']['candidate_different_when_baseline_matches']]
    lower=[i for i in comparisons if i['comparison']['candidate_wer']<i['comparison']['baseline_wer']-1e-9]
    lines=['Kiểm chứng chất lượng Ngọc Huyền trước khi tích hợp batch8','',
           f"Đã đối chiếu checksum/định dạng/độ dài {report['checked_wavs']} WAV; nhận dạng lại {len(comparisons)}/40 cặp của lượt1.",
           f"ASR: {report['asr_status']}; thời gian {report.get('asr_seconds',0):.2f} giây. Nghe mù: chưa hoàn tất. Chưa cho phép tích hợp từ báo cáo này.",'',
           f"WER thô của batch8 cao hơn ở {len(higher)} câu, thấp hơn ở {len(lower)} câu, bằng ở {len(comparisons)-len(higher)-len(lower)} câu. Có {len(new)} câu baseline được nhận đúng toàn văn nhưng candidate khác. Đây là dấu hiệu sàng lọc, không chứng minh lỗi TTS; số chữ/chữ số và sai chính tả của ASR có thể làm tăng WER.",'',
           'Các câu lệch độ dài tương đối nhiều nhất (xét đủ3 lượt candidate):','',
           '| ID | Mốc batch1 (s) | Chênh có dấu (s) | Chênh (%) |','|---|---:|---:|---:|']
    for r in report['duration_rank_by_relative'][:10]:
        lines.append(f"| {r['id']} | {r['baseline_median_seconds']:.2f} | {r['worst_signed_delta_seconds']:+.2f} | {r['worst_relative_delta']*100:+.2f} |")
    lines+=['','Các câu baseline được ASR nhận đúng nhưng batch8 khác:','',
            '| ID | Văn bản gốc | ASR batch1 | ASR batch8 |','|---|---|---|---|']
    for r in new:
        lines.append('| '+' | '.join(clean(r[k]) for k in ['id','text','baseline_asr','candidate_asr'])+' |')
    lines+=['','Nghe mù10 cặp ưu tiên ASR tại blind_review_asr/index.html; không mở khóa A/B trước khi chấm. Đánh dấu đã nghe và lỗi cụt cuối/đổi giọng/đọc sai/nhịp lạ, tải phiếu đánh giá. Chưa có phiếu nghe thì không tự công nhận đạt.','',
            'Đối chiếu đủ40 cặp ASR (WER chưa chuẩn hóa chữ số):','',
            '| ID | ASR batch1 | ASR batch8 | WER1 | WER8 |','|---|---|---|---:|---:|']
    for r in comparisons:
        c=r['comparison']
        lines.append('| '+clean(r['id'])+' | '+clean(r['baseline_asr'])+' | '+clean(r['candidate_asr'])+f" | {c['baseline_wer']:.3f} | {c['candidate_wer']:.3f} |")
    (output/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def verify(output, reference_path, candidate_path):
    import soundfile as sf
    corpus,digest=baseline.load_corpus(baseline.BASE/'tests/fixtures/vieneu_benchmark.json')
    reference=json.loads(reference_path.read_text(encoding='utf-8'))
    candidate=json.loads(candidate_path.read_text(encoding='utf-8'))
    if reference['corpus_sha256']!=digest or candidate['corpus_sha256']!=digest:
        raise ValueError('Corpus identity mismatch')
    refs=reference['rounds']
    candidates=[r for r in candidate['rounds'] if r['batch_size']==8]
    expected={c['id'] for c in corpus['cases']}
    if len(refs)!=3 or len(candidates)!=3 or any(not r['technical_passed'] or len(r['items'])!=len(expected) or {i['id'] for i in r['items']}!=expected for r in refs+candidates):
        raise ValueError('Expected three complete rounds per configuration')
    output.mkdir(parents=True,exist_ok=False)
    report={'corpus_sha256':digest,'asr_status':'pending','listening_status':'pending_human_review',
            'integration_allowed':False,'items':[],
            'limitations':['Paired ASR differences cannot alone distinguish TTS regression from ASR error.',
                           'Digit/word spelling and names may inflate unnormalized WER.',
                           'No human listening completed; no production changes authorized by this report.',
                           '3.08x is observed synthesis throughput on this corpus, not a proven upper bound or full-video speedup.']}
    rankings=duration_rows(corpus['cases'],refs,candidates)
    report['duration_rank_by_relative']=sorted(rankings,key=lambda r:abs(r['worst_relative_delta']),reverse=True)
    report['duration_rank_by_seconds']=sorted(rankings,key=lambda r:r['worst_absolute_delta_seconds'],reverse=True)
    report['checked_wavs']=0
    for rows,root,pattern in [(refs,reference_path.parent,'round_{round:02d}'),(candidates,candidate_path.parent,'batch_8_round_{round:02d}')]:
        for row in rows:
            for item in row['items']:
                path=root/pattern.format(round=row['round'])/(item['id']+'.wav')
                if hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:
                    raise ValueError('Audio checksum mismatch: '+str(path))
                info=sf.info(path)
                if info.samplerate!=48000 or info.channels!=1 or abs(info.duration-item['audio_seconds'])>1/48000:
                    raise ValueError('Audio duration/format mismatch: '+str(path))
                report['checked_wavs']+=1
    for case in corpus['cases']:
        item={'id':case['id'],'text':case['text'],'category':case['category']}
        for label,rows,root,pattern in [('baseline',refs,reference_path.parent,'round_{round:02d}'),('candidate',candidates,candidate_path.parent,'batch_8_round_{round:02d}')]:
            row=rows[0]
            path=root/pattern.format(round=row['round'])/(case['id']+'.wav')
            item[label+'_path']=str(path.resolve())
            item[label+'_sha256']=next(i['sha256'] for i in row['items'] if i['id']==case['id'])
        report['items'].append(item)
    selected=list(dict.fromkeys([r['id'] for r in report['duration_rank_by_relative'][:3]]+[r['id'] for r in report['duration_rank_by_seconds'][:2]]+['p01','p03','n01','n03','l01','l03','t03','q02']))[:10]
    report['blind_sample_ids']=selected
    write_blind_review(output,report['items'],selected)
    path=output/'verification.json'
    def publish():
        path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    started=time.perf_counter()
    try:
        report['asr_status']='running'
        report['asr_settings']={'model':'small','device':'cpu','compute_type':'int8','cpu_threads':2,'beam_size':5,'language':'vi','condition_on_previous_text':False,'vad_filter':False,'local_files_only':True}
        publish()
        from faster_whisper import WhisperModel
        model=WhisperModel('small',device='cpu',compute_type='int8',cpu_threads=2,local_files_only=True)
        report['asr_model_path']=str(model.model.model_path) if hasattr(model.model,'model_path') else None
        for number,item in enumerate(report['items'],1):
            for label in ['baseline','candidate']:
                segments,_=model.transcribe(item[label+'_path'],language='vi',beam_size=5,condition_on_previous_text=False,vad_filter=False)
                item[label+'_asr']=' '.join(s.text.strip() for s in segments)
            item['comparison']=compare_transcripts(item['text'],item['baseline_asr'],item['candidate_asr'])
            publish()
            print(json.dumps({'pair':number,'total':len(report['items']),'id':item['id'],'candidate_higher_wer':item['comparison']['candidate_higher_wer']}),flush=True)
        report['asr_status']='completed_diagnostic_only'
    except Exception as exc:
        report['asr_status']='failed'
        report['asr_error']=type(exc).__name__+': '+str(exc)
    report['asr_seconds']=time.perf_counter()-started
    report['asr_completed_pairs']=sum('comparison' in i for i in report['items'])
    publish()
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--reference',type=Path,default=baseline.BASE/'storage/maintenance/vieneu_batch_baseline_20261105/summary.json')
    parser.add_argument('--candidate',type=Path,default=baseline.BASE/'storage/maintenance/vieneu_batches_20261105/summary.json')
    parser.add_argument('--prepare-blind',action='store_true',help='Create a second blind set prioritizing completed ASR discrepancies')
    parser.add_argument('--audit-repeatability',action='store_true',help='Verify actual WAV hashes across three rounds without repeating ASR')
    parser.add_argument('--audit-cue',help='Rebuild an existing cue from retained float chunks and compare exact WAV bytes')
    parser.add_argument('--upgrade-listening',action='store_true',help='Add timestamp/second-listener fields without changing A/B mappings')
    args=parser.parse_args()
    if args.audit_cue:
        report=audit_cue_join(args.output,args.candidate,args.audit_cue)
        print(json.dumps({'id':args.audit_cue,'matching_runs':len(report['results'])}),flush=True)
        return 0
    if args.upgrade_listening:
        print(json.dumps({'upgraded':upgrade_listening_forms(args.output)}),flush=True)
        return 0
    if args.audit_repeatability:
        report=audit_repeatability(args.output,args.reference,args.candidate)
        print(json.dumps({k:v['identical_cue_count'] for k,v in report['branches'].items()}),flush=True)
        return 0
    if args.prepare_blind:
        selected=prepare_asr_blind(args.output)
        print(json.dumps({'blind_samples':selected}),flush=True)
        return 0
    report=verify(args.output,args.reference,args.candidate)
    return 0 if report['asr_status']=='completed_diagnostic_only' else 1


if __name__=='__main__':
    raise SystemExit(main())
