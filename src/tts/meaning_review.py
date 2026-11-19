"""Rebuild a review table from applied changes, never label AI text as training truth."""
import json
from pathlib import Path

from src.translation.memory import atomic_json
from src.translation.preflight import speech_units


def write_meaning_review(project_dir,cues):
    root=Path(project_dir);known={c.index:c for c in cues}
    entries=[];baselines={}
    for path in sorted((root/'audit').glob('timing_shortening_*.json')):
        plan=json.loads(path.read_text(encoding='utf8'))
        if plan.get('status')!='applied':continue
        for change in plan.get('changes',[]):
            source=change.get('source')
            key=(change['cue_index'],source)
            baseline=baselines.setdefault(key,change['before'])
            after=change['after'];count=speech_units(baseline)
            reduction=1-speech_units(after)/count if count else 0
            strategy=change.get('variant_strategy') or {}
            current=known.get(change['cue_index'])
            entries.append(dict(cue_id=change['cue_index'],source=source,
                baseline=baseline,previous=change['before'],candidate=after,
                strategy=strategy.get('tier'),spoken_unit_reduction=reduction,
                strong_shortening=reduction>=.3 or strategy.get('tier')=='alternative_expression',
                current=bool(current and current.vi_text==after and (source is None or current.effective_source_text==source)),
                human_confirmed=False,training_label=False,plan=path.relative_to(root).as_posix()))
    latest={}
    for row in entries:
        if row['current']:latest[row['cue_id']]=row
    atomic_json(root/'audit/meaning_review.json',dict(entries=entries,current=list(latest.values())))
    def cell(value):return str(value if value is not None else 'Nguồn chưa được lưu trong plan cũ').replace('|','\\|').replace('\n',' ')
    lines=['# Bảng kiểm nghĩa — đề xuất AI, chưa phải nhãn train','',
           'Mức giảm đơn vị đọc là thống kê, không chứng minh mất hay giữ nghĩa.','',
           '| Cue | Nguồn | Việt trước phục hồi | Việt hiện tại | Cách thử | Cần kiểm rút mạnh |',
           '|---|---|---|---|---|---|']
    for row in latest.values():
        lines.append('| '+' | '.join(cell(v) for v in (row['cue_id'],row['source'],row['baseline'],
            row['candidate'],row['strategy'],'Có' if row['strong_shortening'] else 'Kiểm nghĩa thông thường'))+' |')
    (root/'audit/meaning_review.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
    return list(latest.values())
