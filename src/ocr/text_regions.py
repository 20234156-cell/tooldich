"""Conservative subtitle/side-text separation inside an established lower ROI.

One view is only a candidate. Source recovery must verify all six views.
"""
import re
import numpy as np

VERSION = 'caption-side-text-v3-complete-evidence'


def scene_texture_candidate(text, alternatives=()):
    """Short non-Han OCR and explicitly marked raw-image textures."""
    text = text.strip()
    return bool(re.fullmatch(r"[!-~]{1,3}", text)
                or any(row.get('raw_only_texture') or row.get('variant') in {'raw_only', 'button_texture'}
                       for row in alternatives))


def suspected_creator_credit(text):
    """A source-role review signal, never permission to remove writing."""
    return bool(re.search(r'(?:原创|原創|版权|版權|出品)\s*[@＠]\s*\S', text or ''))


def has_temporal_disagreement(cue):
    """Middle-of-cue samples cannot verify short, differently worded intervals."""
    variants = {str(row.get('text', '')).strip() for row in cue.ocr_alternatives
                if row.get('variant') == 'temporal_vote' and str(row.get('text', '')).strip()}
    return bool(variants) and len(variants | {cue.text.strip()}) > 1


def reading_order(result):
    """Stable line grouping then left-to-right order, independent of detector order.

    Group only substantially overlapping glyph rows; two caption lines must not
    interleave just because one box's top corner is a few pixels higher.
    """
    if not result:
        return result
    located = []
    for item in result:
        if len(item) < 3:
            return result
        box = np.asarray(item[0], dtype=float)
        if box.shape != (4, 2) or not np.isfinite(box).all():
            return result
        located.append((float(box[:,1].min()), float(box[:,1].max()),
                        float(box[:,0].min()), item))
    lines = []
    for top, bottom, left, item in sorted(located, key=lambda r:(r[0],r[2])):
        matches = [line for line in lines if
                   min(bottom,line['bottom'])-max(top,line['top']) >= .5*min(bottom-top,line['bottom']-line['top'])]
        if len(matches) == 1:
            line = matches[0]
            # Keep the intersection to prevent a tall scene box chaining rows.
            line['top'],line['bottom'] = max(top,line['top']),min(bottom,line['bottom'])
            line['items'].append((left,item))
        else:
            lines.append(dict(top=top,bottom=bottom,items=[(left,item)]))
    return [item for line in sorted(lines,key=lambda l:l['top'])
            for _,item in sorted(line['items'],key=lambda r:r[0])]


def layout_review_alternatives(result, image_shape, text, confidence, *, raw_only=False):
    """Flag spatially disconnected writing even when its font size is identical.

    No text is deleted or selected here. High recognition confidence says nothing
    about whether a sign belongs to the spoken caption.
    """
    height, width = image_shape[:2]
    boxes = []
    for item in result:
        if len(item) < 3 or not str(item[1]).strip():
            continue
        box = np.asarray(item[0], dtype=float)
        if box.shape != (4, 2) or not np.isfinite(box).all():
            return []
        boxes.append((box[:,0].min(), box[:,1].min(), box[:,0].max(), box[:,1].max()))
    suspicious = raw_texture = False
    if len(boxes) == 1:
        x1,y1,x2,y2 = boxes[0]
        suspicious = (x2 < .35*width or x1 > .65*width) and x2-x1 < .35*width
        # A small repeated glyph detected only on the raw image can be
        # clothing detail. Preserve it for review even at the center.
        if (raw_only and confidence < 90 and x2-x1 < .12*width and y2-y1 < .35*height
                and re.fullmatch(r'([A-Za-z])\1{2,5}', text, flags=re.IGNORECASE)):
            suspicious = raw_texture = True
    for i, (x1,y1,x2,y2) in enumerate(boxes):
        for a1,b1,a2,b2 in boxes[i+1:]:
            horizontal_gap = max(x1-a2, a1-x2, 0)
            vertical_gap = max(y1-b2, b1-y2, 0)
            if horizontal_gap >= .08*width or vertical_gap >= .4*min(y2-y1,b2-b1):
                suspicious = True
    if not suspicious:
        return []
    return [dict(text=text, confidence=confidence, variant='scene_layout_review',
                 reason='Chữ ngoài dòng hoặc nhiều vùng chữ tách rời; cần xác minh phụ đề thay vì dựa vào confidence.',
                 boxes=[list(map(float,b)) for b in boxes], **({'raw_only_texture': True} if raw_texture else {}))]


def separate_side_text(rows, width, height):
    if width <= 0 or height <= 0 or len(rows) < 2:
        return None
    def bounds(row):
        box = np.asarray(row['box'], dtype=float)
        return box[:, 0].min(), box[:, 1].min(), box[:, 0].max(), box[:, 1].max()
    candidates = []
    for row in rows:
        x1,y1,x2,y2 = bounds(row)
        letters = re.findall(r'\w', row['text'])
        chinese = len(re.findall(r'[\u3400-\u9fff]', row['text']))
        if (letters and chinese/len(letters) >= .7 and row['confidence'] >= 90
                and x1 <= width/2 <= x2 and .18*height <= y2-y1 <= .8*height
                and y1 > 1 and y2 < height-1):
            candidates.append(row)
    if len(candidates) != 1:
        return None
    main = candidates[0]
    mx1,my1,mx2,my2 = bounds(main)
    others = [row for row in rows if row is not main]
    for row in others:
        x1,y1,x2,y2 = bounds(row)
        if not ((x2 <= .4*width and mx1-x2 >= .04*width)
                or (x1 >= .6*width and x1-mx2 >= .04*width)):
            return None
        if y2-y1 > .85*(my2-my1):
            return None
    return main, others
