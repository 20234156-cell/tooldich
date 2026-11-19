"""Adjudicate uncertain OCR with timestamped speech and source-image evidence."""
import hashlib
import gc
from difflib import SequenceMatcher
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
import json
import math
from pathlib import Path
import re
import time
import unicodedata

import cv2
import soundfile as sf

from config.settings import settings
from src.jobs.resources import media_timeout, probe_media_duration
from src.jobs.logger import TaskCancelled, PipelineException, SystemErrorCodes
from src.jobs.resources import TaskInterrupted
from src.media.audio_extractor import AudioExtractor
from src.ocr.frame_reader import FrameReader
from src.ocr.source_recheck import source_pixel_rect
from src.ocr.text_regions import has_temporal_disagreement, scene_texture_candidate
from src.stt.whisper_provider import WhisperSTTProvider, available_ram_bytes, review_cuda_runtime
from src.stt.base import TranscriptResult
from src.translation.memory import atomic_json, fingerprint

VERSION = 'audio-visual-consensus-v1'
REPORT_BATCH_SIZE = 12
MAX_WINDOW_SECONDS = 15
SPEECH_CACHE_VERSION = 'ocr-review-whisper-v1'
# Previous worker produced the same validated transcript schema. Preserve its
# read identity for existing windows, while versioning new four-thread writes.


def read_speech_cache(path, key):
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        payload = record['speech']
        if record['key'] == key and record['sha256'] == fingerprint(payload):
            speech = TranscriptResult.model_validate(payload)
            if speech.segments or record.get('complete') is True:
                return speech
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def model_identity(directory):
    files = {}
    for name in ('model.bin', 'config.json', 'tokenizer.json', 'vocabulary.txt',
                 'vocabulary.json', 'preprocessor_config.json'):
        path = Path(directory) / name
        if path.is_file():
            with path.open('rb') as stream:
                files[name] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return files


def text_key(text):
    return ''.join(character for character in (text or '') if character.isalnum()).casefold()


HAN_DIGITS = dict(zip('零一二三四五六七八九', range(10)), 〇=0, 两=2)
SMALL_UNITS = {'十':10, '百':100, '千':1000}
LARGE_UNITS = {'万':10000, '亿':100000000}
VARIANTS = str.maketrans('眞爲衆萬億兩', '真为众万亿两')
NUMERAL = re.compile(r'[0-9零〇一二三四五六七八九两十百千万亿]+')
NUMBER_TOKEN = re.compile(r'<[0-9]+>')


def chinese_integer(text):
    if all(c.isdecimal() or c in HAN_DIGITS for c in text):
        return int(''.join(str(HAN_DIGITS[c]) if c in HAN_DIGITS else c for c in text))
    total = section = 0
    digits = ''
    previous_unit = 10000
    for c in text:
        if c.isdecimal() or c in HAN_DIGITS:
            digits += str(HAN_DIGITS[c]) if c in HAN_DIGITS else c
        elif c in SMALL_UNITS:
            unit = SMALL_UNITS[c]
            if unit >= previous_unit or not digits and section:
                return None
            section += (int(digits) if digits else 1) * unit
            digits = ''
            previous_unit = unit
        else:
            unit = LARGE_UNITS[c]
            value = section + int(digits or 0)
            if not value:
                return None
            if unit == 100000000:
                total = (total + value) * unit
            else:
                total += value * unit
            section = 0
            digits = ''
            previous_unit = 10000
    if digits and len(digits)==1 and digits != '0' and previous_unit > 10 and previous_unit < 10000:
        # Colloquial 一百二 is ambiguous. Do not infer a value.
        return None
    return total + section + int(digits or 0)


@lru_cache(maxsize=8192)
def alignment_key(text):
    text = unicodedata.normalize('NFKC', text or '').translate(VARIANTS)
    text = re.sub(r'(?<=\d),(?=\d{3}(?:\D|$))', '', text)
    tokens, pos = [], 0
    for match in NUMERAL.finditer(text):
        tokens.append(text_key(text[pos:match.start()]))
        raw = match.group()
        # Decimal points and signed values remain literal and distinct.
        left, right = match.span()
        ambiguous = ((left and text[left-1] in '.-−+' and
                      (text[left-1] != '.' or left>1 and text[left-2].isdigit()))
                     or (right+1<len(text) and text[right]=='.' and text[right+1].isdigit()))
        value = chinese_integer(raw) if not ambiguous and len(raw)<=24 else None
        tokens.append(f'<{value}>' if value is not None else f'[{text[max(0,left-1):right+1]}]')
        pos = right
    tokens.append(text_key(text[pos:]))
    return ''.join(tokens)


def label_variant(text, label):
    return (text == label or len(text)>=2 and len(label)-len(text) in (1,2) and label.endswith(text)
            or len(text)==len(label)>=4 and sum(a!=b for a,b in zip(text,label))==1)


def strip_label(text, labels):
    raw = text.strip()
    for label in sorted(labels, key=lambda value: (-len(value),value)):
        for size in range(len(label),max(2,len(label)-2)-1,-1):
            suffix = re.sub(r'[A-Za-z]{1,2}$','',raw).rstrip()
            prefix = re.sub(r'^[A-Za-z]{1,2}','',raw).lstrip()
            if len(suffix)>=size and label_variant(text_key(suffix[-size:]),label):
                return suffix[:-size].rstrip()
            if len(prefix)>=size and label_variant(text_key(prefix[:size]),label):
                return prefix[size:].lstrip()
    return raw

def eligible(cue):
    return (cue.source_kind == 'ocr' and not cue.corrected_text and not cue.vi_text and not cue.voice_audio_path
            and not cue.is_approved
            and not any(row.get('variant') in {'human_review_memory', 'adjacent_tail_review'}
                        for row in cue.ocr_alternatives)
            and (scene_texture_candidate(cue.text, cue.ocr_alternatives) or any(row.get('variant') in {'scene_layout_review', 'dominant_text_size',
                                          'low_confidence_box', 'temporal_vote'} for row in cue.ocr_alternatives)))


def audio_window(cue, cues):
    start, end = max(0, cue.start - 1.5), cue.end + 1.5
    position = next(index for index, item in enumerate(cues) if item.index == cue.index)
    if position:
        previous = cues[position-1]
        if previous.start < cue.start and 0 <= cue.start - previous.end <= 1.5:
            start = min(start, max(0, cue.start - 4, previous.start - .25))
    if position + 1 < len(cues):
        following = cues[position+1]
        if following.end > cue.end and 0 <= following.start - cue.end <= 1.5:
            end = max(end, min(cue.end + 4, following.end + .25))
    if end - start > MAX_WINDOW_SECONDS and cue.duration <= MAX_WINDOW_SECONDS:
        padding = min(1.0, (MAX_WINDOW_SECONDS - cue.duration) / 2)
        start, end = max(0, cue.start - padding), cue.end + padding
    return start, end - start


def write_pcm_window(pcm, start, duration, output):
    with sf.SoundFile(pcm) as stream:
        if (stream.samplerate, stream.channels, stream.subtype) != (16000, 1, 'PCM_16'):
            raise ValueError('Invalid shared speech PCM format')
        offset = min(stream.frames, round(start * 16000))
        stream.seek(offset)
        samples = stream.read(round(duration * 16000), dtype='int16')
    if not len(samples):
        raise ValueError('Speech window is outside source audio')
    sf.write(output, samples, 16000, subtype='PCM_16')


def grouped_audio_windows(targets, cues):
    positions = {cue.index: i for i, cue in enumerate(cues)}
    ordered = sorted(targets, key=lambda cue: (cue.start, cue.index))
    groups = []
    for cue in ordered:
        i = positions[cue.index]
        start, duration = audio_window(cue, cues[max(0, i-1):i+2])
        end = start + duration
        if groups:
            members, left, right = groups[-1]
            if start <= right and max(right, end) - min(left, start) <= MAX_WINDOW_SECONDS:
                members.append(cue.index)
                groups[-1] = members, min(left, start), max(right, end)
                continue
        groups.append(([cue.index], start, end))
    return {cue_id: (left, right-left) for members, left, right in groups for cue_id in members}


def bounded_audio_windows(start, duration):
    end = start + duration
    while end - start > MAX_WINDOW_SECONDS:
        yield start, MAX_WINDOW_SECONDS
        start += MAX_WINDOW_SECONDS - 1
    if end > start:
        yield start, end - start


def main_lines(observations, min_confidence=90, labels=(), original=''):
    """Find stable centered CJK lines independently of the speech hypothesis."""
    if len(observations) != 3 or len({row['frame_index'] for row in observations}) != 3:
        return {}
    common = None
    readings = []
    for observation in observations:
        if len(observation['variants']) != 2:
            return {}
        for boxes in observation['variants']:
            reading = {}
            for row in boxes:
                xs, ys = zip(*row['box'])
                peripheral = max(xs)<observation['width']*.4 or min(xs)>observation['width']*.6
                text = strip_label(row['text'], labels) if not peripheral else row['text'].strip()
                if not text:
                    continue
                key = text_key(text)
                xs, ys = zip(*row['box'])
                if (len(re.findall(r'[\u3400-\u9fff]', key)) < 1
                        or row['confidence'] < min_confidence or not min(xs) < observation['width']/2 < max(xs)
                        or min(xs) <= 2 or max(xs) >= observation['width']-2
                        or min(ys) <= 1 or max(ys) >= observation['height']-1):
                    continue
                if any(other is not row and
                       min(max(xs), max(p[0] for p in other['box'])) > max(min(xs), min(p[0] for p in other['box'])) and
                       min(max(ys), max(p[1] for p in other['box'])) > max(min(ys), min(p[1] for p in other['box']))
                       for other in boxes):
                    continue
                reading[text] = row['confidence']
            if labels:
                reading = {alignment_key(text): (text, confidence) for text, confidence in reading.items()}
                readings.append(reading)
                continue
            common = reading if common is None else {
                text: min(confidence, reading[text]) for text, confidence in common.items() if text in reading}
    if labels:
        candidates = {}
        for key in set().union(*readings):
            supports = [reading[key] for reading in readings if key in reading]
            distinct = sum(any(key in reading for reading in readings[i:i+2]) for i in (0,2,4))
            if len(supports) >= 5 or len(supports) >= 3 and distinct >= 2 and key == alignment_key(strip_label(original, labels)):
                candidates[supports[0][0]] = min(confidence for _,confidence in supports)
        return candidates
    return common or {}


PROTECTED = set('不没沒别別无無非未莫勿零〇一二三四五六七八九十百千万萬亿億两兩每都全只僅仅半第元角分块塊斤公斤克吨噸米厘米毫米公里秒小時时天年月日度升倍成')
NEAR_INITIALS = (('zh', 'z'), ('ch', 'c'), ('sh', 's'), ('n', 'l'))
NEAR_FINALS = (('ang', 'an'), ('eng', 'en'), ('ing', 'in'))


def near_syllable(left, right):
    return any(left.startswith(a) and right.startswith(b) and left[len(a):] == right[len(b):]
               or left.startswith(b) and right.startswith(a) and left[len(b):] == right[len(a):]
               for a, b in NEAR_INITIALS) or any(
        left.endswith(a) and right.endswith(b) and left[:-len(a)] == right[:-len(b)]
        or left.endswith(b) and right.endswith(a) and left[:-len(b)] == right[:-len(a)]
        for a, b in NEAR_FINALS)


def protected_tokens(text):
    return tuple(c for c in text if c in PROTECTED or c.isdigit())


@lru_cache(maxsize=4096)
def pinyin_syllables(text):
    from pypinyin import Style, lazy_pinyin
    return tuple(lazy_pinyin(text, style=Style.NORMAL, errors=list))


@lru_cache(maxsize=4096)
def phonetic_score(visual, spoken):
    """Compare ordered characters and syllables, allowing missing edge syllables."""
    if not visual or not spoken:
        return 0.0
    best = 0.0
    for leading in range(3):
        for trailing in range(3-leading):
            end = len(visual) - trailing
            if end-leading != len(spoken):
                continue
            trimmed = visual[leading:end]
            left = pinyin_syllables(trimmed)
            right = pinyin_syllables(spoken)
            if len(left) != len(trimmed) or len(right) != len(spoken):
                continue
            points = sum(1 if a == b or x == y else .8 if near_syllable(x,y) else 0
                         for a,b,x,y in zip(trimmed,spoken,left,right))
            best = max(best, points / len(visual))
    return best


def phonetic_match(visual, spoken):
    if (len(visual) < 4 or protected_tokens(visual) != protected_tokens(spoken)
            or not all('㐀' <= c <= '鿿' for c in visual+spoken)):
        return False
    for start in range(max(0, len(visual)-len(spoken))+1):
        if len(visual)-len(spoken) not in (0, 1, 2):
            continue
        part = visual[start:start+len(spoken)]
        if sum(a==b for a,b in zip(part,spoken)) < max(2,len(visual)*.55):
            continue
        left = pinyin_syllables(part)
        right = pinyin_syllables(spoken)
        if any(a!=b and x!=y and not near_syllable(x,y)
               for a,b,x,y in zip(part,spoken,left,right)):
            continue
        # Do not resolve a spelling dispute in a likely leading name.
        if any(a!=b and start+i<3 and visual[0] not in '我你他她它这那'
               for i,(a,b) in enumerate(zip(part,spoken))):
            continue
        if phonetic_score(visual, spoken) >= .75:
            return True
    return False


def alignment_match(visual, spoken, *, stable):
    if visual == spoken:
        return 1.0
    if not stable or protected_tokens(visual) != protected_tokens(spoken):
        return 0.0
    if NUMBER_TOKEN.findall(visual) != NUMBER_TOKEN.findall(spoken):
        return 0.0
    if any(c in visual+spoken for c in '[]'):
        return 0.0
    visual_text = NUMBER_TOKEN.sub('', visual)
    spoken_text = NUMBER_TOKEN.sub('', spoken)
    if not all('㐀' <= c <= '鿿' for c in visual_text+spoken_text):
        return 0.0
    if len(visual_text)==len(spoken_text) and len(visual_text)>=2:
        left,right = pinyin_syllables(visual_text),pinyin_syllables(spoken_text)
        if left == right:
            return .99
    if phonetic_match(visual_text,spoken_text):
        return .95
    # A single agreement gap is allowed only with at least five exact
    # characters, and a protected character may not move across the gap.
    from difflib import SequenceMatcher
    changes = [x for x in SequenceMatcher(None,visual,spoken,autojunk=False).get_opcodes() if x[0]!='equal']
    if len(changes)==1:
        op,i,j,k,l = changes[0]
        if (max(j-i,l-k)==1 and min(len(visual_text),len(spoken_text))-(op=='replace')>=5
                and (op != 'replace' or i>=1 or visual_text[0] not in '我你他她它')
                and not any(c in '<>[]' or c.isdigit() or c in PROTECTED for c in visual[i:j]+spoken[k:l])):
            return .8
    return 0.0


def source_candidates(cue, observations, labels):
    stable = main_lines(observations, min_confidence=72 if labels else 78,
                        labels=labels, original=cue.text)
    if not labels or has_temporal_disagreement(cue):
        return stable, set()
    candidates = [cue.text, *(row.get('text', '') for row in cue.ocr_alternatives
                              if row.get('variant') not in {'temporal_vote','adjacent_tail_review'})]
    from difflib import SequenceMatcher
    fallback = set()
    for raw in candidates:
        text = strip_label(raw or '', labels)
        key = alignment_key(text)
        if len(key)<4 or text in stable:
            continue
        supported = 0
        for view in observations:
            if any(row['confidence']>=72 and
                   min(p[0] for p in row['box']) < view['width']/2 < max(p[0] for p in row['box']) and
                   SequenceMatcher(None,key,alignment_key(strip_label(row['text'],labels)),
                                   autojunk=False).ratio()>=.75
                   for boxes in view['variants'] for row in boxes):
                supported += 1
        if supported>=2:
            stable[text]=75
            fallback.add(text)
    return stable,fallback


def bounded_text_repair(expected, spoken):
    """Repairs with explicit local evidence. Keep other numeric changes for review."""
    if expected and spoken and expected.replace('这<1>期','这期').replace('那<1>期','那期') == spoken.replace('这<1>期','这期').replace('那<1>期','那期'):
        return 'keep'
    if expected.endswith('<7>') and spoken.endswith('了') and expected[:-3]==spoken[:-1] and len(expected[:-3])>=5:
        return 'tail'
    if (len(expected)>1 and expected[1:]==spoken[1:] and NUMBER_TOKEN.search(expected)
            and expected[1:].startswith('共<') and spoken.startswith('总共')):
        return 'lead'
    if expected.endswith('呐') and spoken.endswith('哪') and expected[:-1]==spoken[:-1]:
        return 'keep'
    from difflib import SequenceMatcher
    edits=[row for row in SequenceMatcher(None,expected,spoken,autojunk=False).get_opcodes() if row[0]!='equal']
    if len(edits)==1:
        op,i,j,k,l=edits[0]
        if op=='replace' and len(expected)>=5 and j-i==l-k==1 and (expected[i:j],spoken[k:l]) in {('区','凶'),('區','凶')}:
            return 'speech'
    return None


def agreed_caption(cue, segments, observations, offset, labels=()):
    candidates, fallback_candidates = source_candidates(cue, observations, labels)
    if not candidates:
        return None
    matches = []
    matched_spelling = {}
    matched_rank = {}
    strict = main_lines(observations, min_confidence=78, labels=labels, original=cue.text)
    for segment in segments:
        if (segment.avg_logprob is None or segment.no_speech_prob is None
                or not math.isfinite(segment.avg_logprob) or not math.isfinite(segment.no_speech_prob)
                or segment.avg_logprob < (-.95 if labels else -.5)
                or segment.no_speech_prob > (.85 if labels else .45)):
            continue
        segment_matches = {}
        words = [w for w in segment.words if w.end + offset > cue.start-(.35 if labels else .08) and w.start + offset < cue.end+(.35 if labels else .08)]
        for text, confidence in candidates.items():
            expected = alignment_key(text)
            if len(expected) < (1 if labels else 4):
                continue
            if not segment.words:
                start,end = segment.start+offset,segment.end+offset
                if (end>start and min(end,cue.end)-max(start,cue.start)>=.8*(end-start)
                        and expected==alignment_key(segment.original) and segment.no_speech_prob<=.1 and confidence>=90):
                    matches.append(text)
                continue
            for first in range(len(words)):
                raw = ''
                probability = 0.0
                for last in range(first,len(words)):
                    word = words[last]
                    if (not all(math.isfinite(v) for v in (word.start,word.end,word.probability))
                            or word.end<=word.start or word.probability<(.12 if labels else .2)
                            or (last>first and word.start<words[last-1].end-.002)):
                        break
                    raw += word.word
                    probability += word.probability
                    key = alignment_key(raw)
                    if len(key)>len(expected)+12:
                        break
                    start,end = words[first].start+offset,word.end+offset
                    overlap = min(end,cue.end)-max(start,cue.start)
                    repair = bounded_text_repair(expected,key) if labels else None
                    short_exact = labels and key==expected and 1<=len(text_key(text))<=4
                    if overlap < (.68 if labels else .8)*(end-start) and not (
                            labels and expected==key and len(expected)>=2
                            and start>=cue.start-(.8 if len(expected)==2 else .48)
                            and end<=cue.end+.48 and overlap>=.29*(end-start)) and not (short_exact and overlap>0 and cue.start-.35<=(start+end)/2<=cue.end+.35):
                        continue
                    rank = alignment_match(expected,key,stable=text in strict and confidence>=84 or text in fallback_candidates) if labels else (
                        1 if expected==key else .95 if confidence>=90 and phonetic_match(expected,key) else 0)
                    if not rank and repair and text not in fallback_candidates:
                        rank = .82
                    if (not rank and labels and text not in fallback_candidates
                            and len(key)>=5 and expected.endswith(key)
                            and 1<=len(expected)-len(key)<=5 and start<=cue.start+.25):
                        prefix=expected[:-len(key)]
                        preceding=[w for w in segment.words if w.end<=words[first].start+.002
                                   and w.end+offset>=cue.start-1.25]
                        if alignment_key(''.join(w.word for w in preceding)).endswith(prefix):
                            rank=.93
                    if rank and probability/(last-first+1)>=(.6 if short_exact or labels and rank==1 and NUMBER_TOKEN.search(expected) and confidence>=95 else .7 if labels and rank==1 else .78 if labels else .86 if rank==1 else .8):
                        if labels and segment.no_speech_prob > .6 and (rank!=1 and repair not in {'tail','lead'} or confidence<75):
                            continue
                        if len(expected)<4 and (rank<1 or confidence<(72 if labels else 78)):
                            continue
                        if rank > segment_matches.get(text,0):
                            segment_matches[text] = rank
                            if rank < matched_rank.get(text,0):
                                continue
                            matched_rank[text] = rank
                            if repair == 'tail':
                                matched_spelling[text] = text.rstrip()[:-1]+'了'
                            elif repair == 'lead':
                                matched_spelling[text] = '总'+text[1:]
                            elif repair == 'speech':
                                matched_spelling[text] = text_key(raw)
                            elif (rank == .8 or len(key)==len(expected)+1) and not NUMBER_TOKEN.search(key):
                                # One non-phonetic OCR edit corroborated by
                                # the otherwise identical timed utterance.
                                matched_spelling[text] = text_key(raw)
                            else:
                                matched_spelling.pop(text,None)
        if segment_matches:
            best = max(segment_matches.values())
            matches.extend(text for text,score in segment_matches.items() if score==best)
    if labels and not matches:
        timeline = [(w, seg) for seg in segments for w in seg.words
                    if w.end+offset>cue.start and w.start+offset<cue.end]
        timeline.sort(key=lambda pair: pair[0].start)
        words = [w for w,_ in timeline]
        if (len({id(seg) for _,seg in timeline})>=2 and words
                and all(b.start>=a.end-.002 and b.start-a.end<=.22 for a,b in zip(words,words[1:]))
                and all(seg.avg_logprob is not None and seg.avg_logprob>=-.75 for _,seg in timeline)
                and sum(w.probability for w in words)/len(words)>=.8):
            key = alignment_key(''.join(w.word for w in words))
            start,end = words[0].start+offset,words[-1].end+offset
            if end>start and min(end,cue.end)-max(start,cue.start)>=.8*(end-start):
                matches.extend(text for text in candidates if alignment_key(text)==key)
    if labels:
        by_output = {}
        for text in matches:
            output = matched_spelling.get(text,text)
            key = alignment_key(output)
            choice = by_output.get(key)
            if choice is None or candidates[text]>candidates[choice]:
                by_output[key]=text
        matches = list(by_output.values())
    if len(matches)==1:
        return matched_spelling.get(matches[0],matches[0]),candidates[matches[0]]
    return None


INTERJECTIONS = frozenset('啊嗯哦呀呃唉哈')
NEGATIONS = frozenset('不没别无非未莫勿')
EDGE_SUFFIX = re.compile(r'(?<=[\u3400-\u9fff])[A-Za-z0-9]{1,3}$')
EDGE_PREFIX = re.compile(r'^[A-Za-z0-9]{1,3}(?=[\u3400-\u9fff])')
EDGE_UNIT = frozenset('个岁年月日次倍元万块号集分秒点')


def adjudicate_source(cue, decision, neighbors=(), labels=()):
    """Use complete, independently timed evidence from the existing review."""
    if 'speech' not in decision:
        return None
    views = sorted(decision['observations'], key=lambda row: row.get('seconds', row['frame_index']))
    if (len(views) != 3 or len({row['frame_index'] for row in views}) != 3
            or any(len(row['variants']) != 2 for row in views)):
        return None
    spoken = list(timed_words(decision, cue))
    independent = [text for start, end, text in spoken if not any(
        not other.needs_source_review and other.start <= start < end <= other.end
        for other in neighbors)]
    heard = alignment_key(''.join(independent))
    raw = strip_label(cue.text, labels).strip()
    reads = [boxes for view in views for boxes in view['variants']]
    if scene_texture_candidate(raw, cue.ocr_alternatives):
        key = alignment_key(raw)
        if key and key == heard and all(any(
                alignment_key(row['text']) == key and row['confidence'] >= 92 for row in boxes)
                for boxes in reads):
            return 'ocr_asr_verified', raw, 92
        return 'non_cjk_scene_texture_noise', None, None
    if raw in INTERJECTIONS:
        if any(alignment_key(word) == raw for word in independent):
            return 'ocr_asr_verified', raw, cue.confidence
        if not independent:
            return 'short_interjection_no_speech', None, None
    if has_temporal_disagreement(cue):
        return None
    supported = {}
    for index, boxes in enumerate(reads):
        width, height = views[index//2]['width'], views[index//2]['height']
        for row in boxes:
            text = strip_label(row['text'], labels).strip()
            xs, ys = zip(*row['box'])
            if (len(re.findall(r'[\u3400-\u9fff]', text)) >= 2
                    and min(xs) < width/2 < max(xs) and min(ys) > 1 and max(ys) < height-1):
                supported.setdefault(text, {})[index] = row['confidence']
    strict = {text: sum(scores.values())/len(scores) for text, scores in supported.items()
              if len(scores) >= 4 and {2,3} <= scores.keys() and sum(scores.values())/len(scores) >= 85}
    edge = EDGE_SUFFIX.sub('', raw)
    if edge == raw:
        prefix = EDGE_PREFIX.sub('', raw)
        if prefix and prefix[0] not in EDGE_UNIT:
            edge = prefix
    if (edge != raw and len(re.findall(r'[\u3400-\u9fff]', edge)) >= 3
            and heard == alignment_key(edge) and edge in strict):
        return 'ocr_asr_verified_edge_stripped', edge, strict[edge]
    if not independent and not strict and (len(text_key(raw)) == 1 or not any(
            len({index//2 for index, confidence in scores.items() if confidence >= 80}) >= 2
            for scores in supported.values())):
        return 'one_off_no_speech_noise', None, None
    if len(strict) != 1:
        corroborated = {}
        for text, scores in supported.items():
            key = alignment_key(text)
            if (not heard or len({index//2 for index in scores}) < 2 or min(scores.values()) < 85
                    or protected_tokens(key) != protected_tokens(heard)
                    or protected_tokens(key) != protected_tokens(alignment_key(raw))):
                continue
            if (key in heard or SequenceMatcher(None, key, heard, autojunk=False).ratio() >= .65
                    or phonetic_score(key, heard) >= .65):
                corroborated[text] = min(scores.values())
        if len(corroborated) == 1:
            text, confidence = next(iter(corroborated.items()))
            return 'ocr_asr_verified', text, confidence
        return None
    text, confidence = next(iter(strict.items()))
    expected = alignment_key(text)
    if (protected_tokens(expected) != protected_tokens(alignment_key(raw))
            or (heard and tuple(c for c in expected if c in NEGATIONS) !=
                tuple(c for c in heard if c in NEGATIONS))
            or (heard and NUMBER_TOKEN.findall(expected) != NUMBER_TOKEN.findall(heard))):
        return None
    if heard:
        score = max(SequenceMatcher(None, expected, heard, autojunk=False).ratio(),
                    phonetic_score(expected, heard))
        if score >= .65 or expected in heard:
            return 'ocr_asr_verified', text, confidence
    if SequenceMatcher(None, expected, alignment_key(raw), autojunk=False).ratio() < .8:
        return None
    return 'visual_consensus_recovered', text, confidence


def cached_observations(report, source_hash, roi, engine_version, *, source_report=False):
    if (not report or report.get('status') != 'completed'
            or report.get('source_sha256') != source_hash or report.get('roi') != roi
            or report.get('engine_version') != engine_version):
        return {}
    originals = {c['index']: c for c in report.get('input_cues', [])}
    decisions = report.get('decisions', [])
    if source_report:
        decisions = report.get('bilingual', {}).get('decisions', []) + decisions
    result = {}
    for decision in decisions:
        cue_id = decision['cue_id']
        original = decision.get('original', originals.get(cue_id))
        canonical_views = decision.get('audio_observations') if source_report else None
        views = canonical_views or decision.get('observations', [])
        if not original or len(views) != 3 or len({v['frame_index'] for v in views}) != 3:
            continue
        if not all(len(v.get('variants', [])) == 2 for v in views):
            continue
        if source_report and not canonical_views:
            if not all('pixel_rect' in v for v in views):
                continue
            views = [dict(v, width=v['pixel_rect'][2], height=v['pixel_rect'][3]) for v in views]
        result[cue_id] = (original['start'], original['end'], views)
    return result


def layout_noise(cue, segments, offset, observations, labels):
    """Require a recurring peripheral label in all six views and no timed speech."""
    if len(observations) != 3 or not labels:
        return False
    if any(min(w.end + offset, cue.end) > max(w.start + offset, cue.start)
           for s in segments for w in (s.words or [s])):
        return False
    for observation in observations:
        width = observation['width']
        if len(observation['variants']) != 2:
            return False
        for boxes in observation['variants']:
            if not boxes or any(text_key(row['text']) not in labels or row['confidence'] < 85
                or not (max(point[0] for point in row['box']) < width*.4
                        or min(point[0] for point in row['box']) > width*.6) for row in boxes):
                return False
    return True


def watermark_only(cue, decision, labels, neighbors):
    """Classify layout noise using all six views and independently timed speech."""
    views = decision['observations']
    if len(views) != 3 or len({v['frame_index'] for v in views}) != 3 or not labels:
        return None
    possible = set(labels)
    words = list(timed_words(decision,cue))
    original_label_only = not text_key(strip_label(cue.text, labels))
    leftovers = []
    for view in views:
        if len(view['variants']) != 2:
            return None
        for boxes in view['variants']:
            found = set()
            for row in boxes:
                key = text_key(row['text'])
                xs = [p[0] for p in row['box']]
                peripheral = max(xs)<view['width']*.4 or min(xs)>view['width']*.6
                variants = {label for label in labels if label_variant(re.sub(r'[a-z]{1,2}$','',key),label)}
                if peripheral and variants:
                    found.update(variants)
                elif key:
                    leftovers.append(key)
                    low_signal = row['confidence'] < 76 and (not key.isdecimal() or peripheral)
                    isolated = not words and original_label_only and len(key)==1 and row['confidence']<95
                    edge_glyph = peripheral and len(key)==1 and row['confidence']<85
                    ascii_noise = re.fullmatch(r'[a-z]{1,3}',key) and row['confidence']<75
                    if not (low_signal or isolated or edge_glyph or ascii_noise):
                        return None
            possible.intersection_update(found)
    if not possible:
        return None
    from collections import Counter
    if any(len(key)>1 and count>=4 and not key.isascii() for key,count in Counter(leftovers).items()):
        return None
    if not words:
        return 'no_speech_layout_noise', sorted(possible), None
    if len(''.join(text for _,_,text in words))>2:
        return None
    for neighbor in neighbors:
        gap = max(cue.start,neighbor.start)-min(cue.end,neighbor.end)
        if not -.003<=gap<=.08:
            continue
        if all((min(end,neighbor.end)>max(start,neighbor.start)
                or (min(abs(end-neighbor.end),abs(start-neighbor.start))<=.16
                    and text in text_key(strip_label(neighbor.effective_source_text, labels))))
               for start,end,text in words):
            return 'watermark_only_boundary_bleed', sorted(possible), neighbor.index
    # Accept a word tail that is part of a longer segment outside this cue.
    offset = decision.get('audio_offset',0)
    for seg in decision.get('speech', []):
        seg_start,seg_end = seg['start']+offset,seg['end']+offset
        if (len(text_key(seg['original']))>=4 and
                all(seg_start-.001<=start<end<=seg_end+.001 for start,end,_ in words)
                and (seg_start<cue.start-.25 and all(end<=cue.start+.45 for _,end,_ in words)
                     or seg_end>cue.end+.25 and all(start>=cue.end-.45 for start,_,_ in words))):
            return 'watermark_only_boundary_bleed', sorted(possible), None
    if original_label_only and all(
            (start < cue.start < end and end-cue.start<=.30)
            or (start < cue.end < end and cue.end-start<=.30)
            for start,end,_ in words):
        return 'watermark_only_boundary_bleed',sorted(possible),None
    return None


def timed_words(decision, cue):
    offset = decision.get('audio_offset', 0)
    for segment in decision.get('speech', []):
        for word in segment.get('words') or [segment]:
            text = text_key(word.get('word', word.get('original', '')))
            start, end = offset + word['start'], offset + word['end']
            if text and min(end, cue.end) > max(start, cue.start):
                yield start, end, text


def transition_views(decision, neighbor, labels):
    """A transition must still show the neighboring caption or verified label."""
    views = decision['observations']
    expected = text_key(neighbor.effective_source_text)
    if len(views) != 3 or len({view['frame_index'] for view in views}) != 3:
        return False
    evidence = False
    for view in views:
        if len(view['variants']) != 2:
            return False
        for boxes in view['variants']:
            for row in boxes:
                key = text_key(row['text'])
                xs = [point[0] for point in row['box']]
                if key in labels and (max(xs) < view['width']*.4 or min(xs) > view['width']*.6):
                    evidence = True
                    continue
                if not key or key not in expected or protected_tokens(key) != protected_tokens(expected):
                    return False
                evidence = True
    return evidence


def extend_verified_noise(cues, excluded, report, labels):
    """Accept a short label continuation with independent exact label reads."""
    decisions = {row['cue_id']: row for row in report['decisions']}
    ordered = sorted(cues, key=lambda cue: (cue.start, cue.index))
    for i, cue in enumerate(ordered):
        decision = decisions.get(cue.index)
        if (not decision or decision['action'] != 'pending' or cue.duration > .45
                or 'speech' not in decision or list(timed_words(decision,cue))
                or has_temporal_disagreement(cue)):
            continue
        views = decision['observations']
        if len(views) != 3 or len({v['frame_index'] for v in views}) != 3:
            continue
        for neighbor in ordered[max(0,i-1):i]+ordered[i+1:i+2]:
            prior = decisions.get(neighbor.index, {})
            gap = max(cue.start, neighbor.start)-min(cue.end,neighbor.end)
            if not 0 <= gap <= .05 or prior.get('reason_code') != 'no_speech_layout_noise':
                continue
            anchors = {text_key(t) for t in prior.get('recurring_labels', [])} & set(labels)
            exact = 0
            valid = True
            for view in views:
                if len(view['variants']) != 2:
                    valid = False
                    break
                for boxes in view['variants']:
                    if len(boxes) != 1:
                        valid = False
                        break
                    row = boxes[0]
                    key = text_key(row['text'])
                    xs = [p[0] for p in row['box']]
                    if not (max(xs) < view['width']*.4 or min(xs) > view['width']*.6):
                        valid = False
                    if key in anchors:
                        exact += 1
                    elif not any(len(key)==len(label) and len(key)>=4
                                 and sum(a!=b for a,b in zip(key,label))==1 for label in anchors):
                        valid = False
            if valid and exact >= 4:
                excluded.add(cue.index)
                report['excluded'] += 1
                decision.update(action='discarded_non_caption', reason_code='boundary_transition_artifact',
                                related_cue=neighbor.index, recurring_labels=sorted(anchors))
                break


def merge_confirmed_fragments(cues, replacements, excluded, report, labels=()):
    """Join a short boundary reading only when words belong to the adjacent cue."""
    decisions = {row['cue_id']: row for row in report['decisions']}
    ordered = sorted(cues, key=lambda cue: (cue.start, cue.index))
    for i, cue in enumerate(ordered):
        decision = decisions.get(cue.index)
        if (not decision or decision['action'] != 'pending' or cue.duration > .45
                or 'speech' not in decision or has_temporal_disagreement(cue)):
            continue
        keys = {text_key(text) for text in main_lines(decision['observations'])}
        for neighbor in ordered[max(0,i-1):i]+ordered[i+1:i+2]:
            neighbor = replacements.get(neighbor.index, neighbor)
            gap = max(cue.start, neighbor.start) - min(cue.end, neighbor.end)
            if (neighbor.index in excluded or neighbor.needs_source_review
                    or neighbor.source_kind != 'ocr' or neighbor.corrected_text or neighbor.vi_text
                    or neighbor.voice_audio_path or neighbor.is_approved or not 0 <= gap <= .05):
                continue
            expected = text_key(neighbor.text)
            words = list(timed_words(decision, cue))
            if any(min(end, neighbor.end)-max(start, neighbor.start) < .5*(end-start)
                   or text not in expected for start,end,text in words):
                continue
            duplicate = keys == {expected}
            if not duplicate and not transition_views(decision, neighbor, labels):
                continue
            if duplicate:
                replacements[neighbor.index] = neighbor.model_copy(update={
                    'start':min(cue.start,neighbor.start), 'end':max(cue.end,neighbor.end)})
            excluded.add(cue.index)
            report['excluded'] += 1
            decision.update(action='discarded_non_caption',
                            reason_code='adjacent_duplicate_fragment' if duplicate else 'boundary_transition_artifact',
                            related_cue=neighbor.index, related_text=neighbor.text)
            break


def recheck(video, roi, cues, output_dir, engine, should_continue=None, progress_callback=None, *, cache_dir=None,
            _retry_small=False, source_evidence=None, prior_audio=None):
    targets = [cue for cue in cues if eligible(cue)]
    pending_ids = {cue.index for cue in targets}
    report = dict(version=VERSION, status='not_needed', decisions=[], resolved=0,
                  deferred_cue_ids=sorted(pending_ids))
    if not targets:
        return cues, report
    directory = Path(output_dir) / 'audio_recheck'
    directory.mkdir(parents=True, exist_ok=True)

    def check():
        if should_continue and not should_continue():
            raise TaskCancelled('Đã dừng đối chiếu âm thanh và chữ trên hình.')

    def publish():
        report['deferred_cue_ids'] = sorted(pending_ids)
        temporary = directory / 'report.tmp.json'
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(directory / 'report.json')

    preflight_started = time.perf_counter()
    if progress_callback:
        progress_callback(92, 'Đối chiếu âm thanh: kiểm tra tài nguyên và model')
    report['status'] = 'running'
    with Path(video).open('rb') as stream:
        report['source_sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
    from collections import Counter
    views_by_id = cached_observations(prior_audio, report['source_sha256'], roi.model_dump(), engine.cache_version)
    source_views = cached_observations(source_evidence, report['source_sha256'], roi.model_dump(),
                                       engine.cache_version, source_report=True)
    prior_views = views_by_id.copy()
    views_by_id.update(source_views)
    labels = Counter(text_key(label) for cue in cues if not cue.needs_source_review
                     for label in set(cue.scene_texts))
    if source_views:
        for decision in source_evidence.get('bilingual', {}).get('decisions', []):
            if decision.get('action') == 'recovered':
                labels.update({text_key(label) for label in decision.get('scene_texts', [])})
    labels = {label for label, count in labels.items() if label and count >= 3}
    excluded = set()
    cue_positions = {cue.index:i for i,cue in enumerate(cues)}
    report['visual_cache_hits'] = 0
    report['excluded'] = 0
    report.update(roi=roi.model_dump(), engine_version=engine.cache_version,
                  input_cues=[cue.model_dump() for cue in cues], audio_filter=settings.STT_AUDIO_FILTER,
                  speech_device='cpu', speech_compute='int8')
    # A missing local model leaves the existing source review in place.
    # Never download a speech model implicitly during OCR.
    close = getattr(engine, 'close', None)
    if callable(close):
        close()
    gc.collect()
    provider = WhisperSTTProvider(device='cpu', compute_type='int8')
    provider.cpu_threads = 4
    provider.local_files_only = True
    provider.isolated_review = True
    provider.model_size = 'small'
    speech_identity = None
    try:
        model_directory = Path(settings.OCR_AUDIO_MODEL_DIR) if settings.OCR_AUDIO_MODEL_DIR and not _retry_small else None
        validated = settings.BASE_DIR / 'storage/models/whisper-large-v3-turbo/validated.json'
        expected_files = None
        prior_small = bool(prior_views and 'faster-whisper-small' in
                           str(prior_audio.get('speech_model', '')).lower())
        if model_directory is None and not _retry_small and not prior_small and validated.is_file():
            expected = json.loads(validated.read_text(encoding='utf-8'))
            if expected.get('version') == VERSION and expected.get('model_files'):
                expected_files = expected['model_files']
                model_directory = validated.parent
        large_model = False
        if model_directory is not None:
            if not all((model_directory / name).is_file() for name in ('model.bin', 'config.json', 'tokenizer.json')):
                raise ValueError('Local speech model is incomplete')
            large_model = ('large' in model_directory.name.lower()
                           or (model_directory / 'model.bin').stat().st_size >= 700 * 1024**2)
        runtime = None if _retry_small else review_cuda_runtime(3500 if large_model else 2000)
        if large_model and runtime is None:
            available = available_ram_bytes()
            report['speech_ram_available_bytes'] = available
            if available is None or available < 4 * 1024**3:
                report['speech_preflight'] = 'small_due_to_cpu_memory'
                model_directory = None
                runtime = review_cuda_runtime(2000)
        if model_directory is not None and expected_files:
            speech_identity = model_identity(model_directory)
            if speech_identity != expected_files:
                model_directory = None
                speech_identity = None
                runtime = review_cuda_runtime(2000)
        if model_directory is not None:
            model_path = str(model_directory.resolve())
        else:
            from faster_whisper.utils import download_model
            model_path = download_model('small', local_files_only=True)
        provider.model_size = model_path
        provider.device, provider.compute_type = runtime or ('cpu', 'int8')
        report.update(speech_device=provider.device, speech_compute=provider.compute_type,
                      speech_cpu_threads=4 if provider.device == 'cpu' else 2,
                      speech_beam_size=1, speech_temperature=0.0)
    except Exception as exc:
        report.update(status='unavailable', reason=type(exc).__name__)
        report['speech_preflight_seconds'] = round(time.perf_counter() - preflight_started, 3)
        report['elapsed_seconds'] = report['speech_preflight_seconds']
        publish()
        return cues, report
    report['speech_model'] = str(provider.model_size)
    report['speech_identity'] = speech_identity or model_identity(provider.model_size)
    report['speech_preflight_seconds'] = round(time.perf_counter() - preflight_started, 3)
    provider.model_fingerprint = fingerprint(report['speech_identity'])
    cache_policy = None
    if cache_dir is not None and {'model.bin', 'config.json', 'tokenizer.json'} <= report['speech_identity'].keys():
        try:
            runtime = {name: version(name) for name in ('faster-whisper', 'ctranslate2', 'av')}
        except PackageNotFoundError:
            runtime = None
    else:
        runtime = None
    if runtime:
        stt_dir = Path(__file__).resolve().parent.parent / 'stt'
        cache_policy = {
            'version': SPEECH_CACHE_VERSION, 'pcm_extraction': 'shared-filtered-v1', 'source': report['source_sha256'],
            'model': report['speech_identity'], 'language': 'zh', 'device': provider.device, 'compute': provider.compute_type,
            'decode': {'beam_size': 1, 'best_of': 1, 'temperature': 0.0,
                       'cpu_threads': report['speech_cpu_threads']},
            'runtime': runtime,
            'code': [hashlib.sha256((stt_dir / name).read_bytes()).hexdigest()
                     for name in ('base.py', 'whisper_provider.py', 'review_worker.py')],
            'settings': {name: getattr(settings, name) for name in (
                'WHISPER_VAD_FILTER', 'WHISPER_VAD_THRESHOLD',
                'WHISPER_VAD_MIN_SILENCE_MS', 'WHISPER_VAD_SPEECH_PAD_MS', 'STT_AUDIO_FILTER')},
        }
    pending_cache = {}
    report['speech_cache_hits'] = 0
    started = time.perf_counter()
    cap = None
    reader = None
    replacements = {}
    try:
        from src.jobs.resources import run_media

        if progress_callback:
            progress_callback(92, 'Đối chiếu âm thanh: đang giải mã PCM nguồn')
        pcm = directory / 'source.pcm.wav'
        command = [str(AudioExtractor().get_ffmpeg_executable()), '-nostdin', '-v', 'error',
                   '-y', '-xerror', '-i', str(video), '-vn']
        if settings.STT_AUDIO_FILTER.strip():
            command += ['-af', settings.STT_AUDIO_FILTER.strip()]
        command += ['-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(pcm)]
        run_media(command, timeout=media_timeout(settings.FFMPEG_TIMEOUT_SECONDS, probe_media_duration(video)), stage='OCR_AUDIO_RECHECK',
                  should_continue=should_continue)
        report['pcm_decode_seconds'] = round(time.perf_counter() - started, 3)
        windows = grouped_audio_windows(targets, cues)
        prepared = {}
        batch_method = getattr(type(provider), 'transcribe_review_batch', None)
        if callable(batch_method):
            # Finish speech while OCR is unloaded. All child processes exit
            # before visual evidence restarts the OCR model.
            speech_started = time.perf_counter()
            specs = sorted({part for start, duration in windows.values()
                            for part in bounded_audio_windows(start, duration)})
            report.update(speech_windows=len(specs), speech_batch_windows=len(specs),
                          speech_processes=0, speech_preexisting_cache_hits=0)
            requests = []
            for start, duration in specs:
                check()
                audio = directory / f'window_{round(start*16000)}_{round(duration*16000)}.wav'
                write_pcm_window(pcm, start, duration, audio)
                audio_hash = hashlib.sha256(audio.read_bytes()).hexdigest()
                key = fingerprint({**cache_policy, 'window': [start, duration], 'wav': audio_hash}) if cache_policy else None
                path = Path(cache_dir) / f'{key}.json' if key else None
                speech = read_speech_cache(path, key) if path else None
                if speech is None:
                    requests.append((start, duration, audio, key, path))
                else:
                    prepared[(start, duration)] = speech
            cached = len(prepared)
            report['speech_preexisting_cache_hits'] = cached

            def update_speech_progress(completed):
                if progress_callback:
                    done = cached + completed
                    progress_callback(93 + int(5 * done / max(1, len(specs))),
                                      f'Đối chiếu âm thanh ({done}/{len(specs)} cửa sổ)')

            update_speech_progress(0)
            if requests:
                report['speech_processes'] += 1

                def cache_completed_window(index, speech):
                    _, _, _, key, path = requests[index]
                    if path is None or path in pending_cache:
                        return
                    payload = speech.model_dump()
                    entry = dict(key=key, sha256=fingerprint(payload), speech=payload, complete=True)
                    atomic_json(path, entry)
                    pending_cache[path] = entry

                progress_options = {'on_progress': update_speech_progress} if progress_callback else {}
                if cache_policy:
                    progress_options['on_result'] = cache_completed_window
                results = provider.transcribe_review_batch([item[2] for item in requests],
                            language='zh', should_continue=should_continue, **progress_options)
                if len(results) != len(requests):
                    raise ValueError('Speech batch result count does not match requests')
                with Path(video).open('rb') as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != report['source_sha256']:
                        raise ValueError('Nguồn video đã đổi trong lúc đối chiếu âm thanh.')
                failure = None
                for index, ((start, duration, _, _, _), speech) in enumerate(zip(requests, results)):
                    prepared[(start, duration)] = speech
                    if isinstance(speech, PipelineException):
                        if not _retry_small and speech.code == SystemErrorCodes.STT_MODEL_UNAVAILABLE and failure is None:
                            failure = speech
                        continue
                    cache_completed_window(index, speech)
                if failure:
                    raise failure
            report['speech_windows_completed'] = sum(not isinstance(item, PipelineException)
                                                      for item in prepared.values())
            update_speech_progress(report['speech_windows_completed'] - cached)
            publish()
            report['speech_phase_seconds'] = round(time.perf_counter()-speech_started, 3)
        visual_started = time.perf_counter()

        def recognize_window(audio, start, duration):
            if prepared:
                result = prepared[(start, duration)]
                if isinstance(result, PipelineException):
                    raise result
                return result
            return provider.transcribe(audio, language='zh', should_continue=should_continue)

        previous_window = None
        previous_speech = None
        for position, cue in enumerate(sorted(targets, key=lambda item: (item.start, item.index))):
            check()
            decision = dict(cue_id=cue.index, action='pending', observations=[],
                            reason_code='unexamined_deferred')
            report['decisions'].append(decision)
            if progress_callback:
                progress_callback(99, f'Đối chiếu lời nói và ảnh nguồn ({position + 1}/{len(targets)} đoạn)')
            start, duration = windows[cue.index]
            if duration > MAX_WINDOW_SECONDS:
                segments = []
                decision['audio_windows'] = []
                for part_start, part_duration in bounded_audio_windows(start, duration):
                    check()
                    part = directory / f'cue_{cue.index}_{round(part_start*16000)}.wav'
                    write_pcm_window(pcm, part_start, part_duration, part)
                    try:
                        if not prepared and callable(close):
                            close()
                        if not prepared:
                            gc.collect()
                        result = recognize_window(part, part_start, part_duration)
                    except TaskInterrupted:
                        raise
                    except PipelineException as exc:
                        if exc.code == SystemErrorCodes.STT_MODEL_UNAVAILABLE:
                            report.update(status='unavailable', reason=str(exc))
                        decision['reason'] = str(exc)
                        break
                    decision['audio_windows'].append(dict(start=part_start, duration=part_duration,
                                                          audio=part.name,
                                                          sha256=hashlib.sha256(part.read_bytes()).hexdigest()))
                    segments.extend(dict(segment.model_dump(), audio_offset=part_start) for segment in result.segments)
                else:
                    pending_ids.remove(cue.index)
                    decision['reason'] = 'Long cue requires boundary review'
                decision['speech'] = segments
                decision['boundary_review_required'] = True
                decision['reason_code'] = 'long_cue_boundary_review'
                if report['status'] == 'unavailable':
                    if not _retry_small:
                        break
                    report['status'] = 'running'
                publish()
                continue
            audio = directory / f'cue_{cue.index}.wav'
            write_pcm_window(pcm, start, duration, audio)

            check()
            audio_hash = hashlib.sha256(audio.read_bytes()).hexdigest()
            cache_key = fingerprint({**cache_policy, 'window': [start, duration], 'wav': audio_hash}) if cache_policy else None
            cache_path = Path(cache_dir) / f'{cache_key}.json' if cache_key else None
            speech = previous_speech if previous_window == (start, duration) else None
            if speech is None and cache_path:
                speech = read_speech_cache(cache_path, cache_key)
            try:
                if speech is None:
                    # Visual evidence may have restarted the OCR worker after
                    # the previous cue. Do not overlap its model with Whisper.
                    if not prepared and callable(close):
                        close()
                    if not prepared:
                        gc.collect()
                    speech = recognize_window(audio, start, duration)
                    if cache_path:
                        payload = speech.model_dump()
                        pending_cache[cache_path] = dict(key=cache_key, sha256=fingerprint(payload), speech=payload, complete=True)
                        with Path(video).open('rb') as stream:
                            unchanged = hashlib.file_digest(stream, 'sha256').hexdigest() == report['source_sha256']
                        if not unchanged:
                            raise ValueError('Nguồn video đã đổi trong lúc đối chiếu âm thanh.')
                        atomic_json(cache_path, pending_cache[cache_path])
                elif previous_window != (start, duration):
                    report['speech_cache_hits'] += 1
            except TaskCancelled:
                raise
            except TaskInterrupted:
                raise
            except PipelineException as exc:
                decision.update(reason=str(exc), audio=audio.name, audio_sha256=audio_hash)
                if exc.code == SystemErrorCodes.STT_MODEL_UNAVAILABLE:
                    report.update(status='unavailable', reason=str(exc))
                    if not _retry_small:
                        break
                    report['status'] = 'running'
                publish()
                continue
            previous_window, previous_speech = (start, duration), speech
            decision.update(audio=audio.name, audio_sha256=audio_hash,
                            audio_offset=start, speech=[segment.model_dump() for segment in speech.segments])
            cached = views_by_id.get(cue.index)
            if cached and cached[:2] == (cue.start, cue.end):
                decision['observations'] = cached[2]
                decision['visual_evidence'] = 'source_recheck' if cue.index in source_views else 'prior_audio_recheck'
                report['visual_cache_hits'] += 1
            if not decision['observations'] and cap is None:
                cap = cv2.VideoCapture(str(video))
                reader = FrameReader(cap, check)
                fps = cap.get(cv2.CAP_PROP_FPS)
                if not cap.isOpened() or not math.isfinite(fps) or fps <= 0:
                    raise ValueError('Không xác minh được FPS nguồn.')
            for fraction in (() if decision['observations'] else (.25, .5, .75)):
                check()
                wanted = math.floor((cue.start + cue.duration * fraction) * fps)
                ok, frame = reader.read(wanted)
                actual_index = int(cap.get(cv2.CAP_PROP_POS_FRAMES))-1
                seconds = cap.get(cv2.CAP_PROP_POS_MSEC)/1000
                if not ok or actual_index != wanted or not cue.start <= seconds < cue.end:
                    break
                x, y, width, height = source_pixel_rect(roi, frame.shape[1], frame.shape[0])
                crop = frame[y:y+height, x:x+width]
                image = directory / f'cue_{cue.index}_{wanted}.png'
                if not cv2.imwrite(str(image), crop):
                    raise OSError('Không lưu được ảnh đối chiếu âm thanh.')
                decision['observations'].append(dict(frame_index=wanted, seconds=seconds,
                    width=width, height=height, image=image.name,
                    sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
                    variants=[engine.read_boxes(crop, preprocess=value) for value in (False, True)]))
            if len(decision['observations']) == 3:
                pending_ids.discard(cue.index)
            agreed = agreed_caption(cue, speech.segments, decision['observations'], start, labels)
            alternate = prior_views.get(cue.index) if cue.index in source_views else None
            if not agreed and alternate and alternate[:2] == (cue.start, cue.end):
                fallback = agreed_caption(cue, speech.segments, alternate[2], start, labels)
                if fallback and not (set(main_lines(decision['observations'])) - {fallback[0]}):
                    agreed = fallback
                    decision['observations'] = alternate[2]
                    decision['visual_evidence'] = 'prior_audio_recheck'
            position_in_source = cue_positions[cue.index]
            neighbors = cues[max(0,position_in_source-1):position_in_source]+cues[position_in_source+1:position_in_source+2]
            arbitration = adjudicate_source(cue, decision, neighbors, labels) if not agreed else None
            if arbitration and arbitration[1] is not None:
                agreed = arbitration[1:]
            noise = None if arbitration or agreed or has_temporal_disagreement(cue) else watermark_only(
                cue,decision,labels,cues[max(0,position_in_source-1):position_in_source]+
                cues[position_in_source+1:position_in_source+2])
            if agreed and has_temporal_disagreement(cue):
                decision['reason'] = 'Temporal OCR conflict requires source boundary review'
                decision['reason_code'] = 'temporal_vote_unresolved'
                decision['boundary_review_required'] = True
            elif arbitration and arbitration[1] is None:
                excluded.add(cue.index)
                report['excluded'] += 1
                decision.update(action='discarded_non_caption', reason_code=arbitration[0],
                                is_ignored=True, exclusion_reason=arbitration[0], needs_source_review=False)
            elif noise:
                excluded.add(cue.index)
                report['excluded'] += 1
                reason,watermarks,neighbor = noise
                decision.update(action='discarded_non_caption',reason_code=reason,recurring_labels=watermarks,
                                related_cue=neighbor)
            elif not agreed and not has_temporal_disagreement(cue) and layout_noise(
                    cue, speech.segments, start, decision['observations'], labels):
                excluded.add(cue.index)
                report['excluded'] += 1
                decision.update(action='discarded_non_caption', reason_code='no_speech_layout_noise',
                                recurring_labels=sorted({row['text'] for ob in decision['observations']
                                                        for rows in ob['variants'] for row in rows}))
            elif agreed:
                text, confidence = agreed
                sides = sorted({row['text'] for observation in decision['observations']
                                for boxes in observation['variants'] for row in boxes
                                if text_key(row['text']) != text_key(text)})
                replacements[cue.index] = cue.model_copy(deep=True, update=dict(
                    text=text, corrected_text=unicodedata.normalize('NFKC',text).translate(VARIANTS) if labels else cue.corrected_text, confidence=confidence, ocr_alternatives=[],
                    scene_texts=sides, text_classification='mixed' if sides else 'subtitle'))
                if arbitration:
                    replacements[cue.index] = replacements[cue.index].model_copy(
                        update=dict(corrected_text=text, is_approved=True))
                decision.update(action='recovered', text=text, scene_texts=sides,
                                reason_code=arbitration[0] if arbitration else 'ocr_asr_verified')
                report['resolved'] += 1
            else:
                options, _ = source_candidates(cue, decision['observations'], labels)
                decision['candidate_lines'] = list(options)
                decision['speech_in_cue'] = ''.join(text for _,_,text in timed_words(decision,cue))
                if not options:
                    decision['reason'] = 'no_stable_main_line'
                elif not decision['speech_in_cue']:
                    decision['reason'] = 'no_timed_speech'
                else:
                    decision['reason'] = 'text_or_timing_disagreement'
                decision['reason_code'] = ('temporal_vote_unresolved' if has_temporal_disagreement(cue)
                                           else 'ocr_asr_conflict')
            if position % REPORT_BATCH_SIZE == 0 and report['visual_cache_hits'] != position + 1:
                publish()
        else:
            report['status'] = 'completed' if not pending_ids else 'pending'
        extend_verified_noise(cues, excluded, report, labels)
        merge_confirmed_fragments(cues, replacements, excluded, report, labels)
        report['visual_phase_seconds'] = round(time.perf_counter()-visual_started, 3)
    except TaskCancelled:
        report['status'] = 'cancelled'
        raise
    except (TaskInterrupted, OSError):
        report['status'] = 'failed'
        raise
    except Exception as exc:
        # An unavailable recognizer is not evidence that text is non-dialogue.
        report.update(status='unavailable', reason=str(exc))
    finally:
        if cap is not None:
            cap.release()
        (directory / 'source.pcm.wav').unlink(missing_ok=True)
        report['elapsed_seconds'] = round(time.perf_counter() - preflight_started, 3)
        publish()
    check()
    with Path(video).open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != report['source_sha256']:
            for path in pending_cache:
                path.unlink(missing_ok=True)
            report.update(status='failed', reason='Source changed during verification')
            publish()
            raise ValueError('Nguồn video đã đổi trong lúc đối chiếu âm thanh.')
    if (not _retry_small and report['status'] == 'unavailable'
            and (provider.device == 'cuda' or model_directory is not None and any(marker in report.get('reason', '').lower() for marker in
                    ('mkl_malloc', 'failed to allocate', 'out of memory', 'memoryerror')))):
        first_attempt = {key: report.get(key) for key in ('speech_model', 'speech_device', 'speech_compute', 'reason', 'elapsed_seconds')}
        del provider
        result, fallback = recheck(video, roi, cues, output_dir, engine, should_continue,
                                   progress_callback, cache_dir=cache_dir, _retry_small=True,
                                   source_evidence=source_evidence, prior_audio=prior_audio)
        fallback['speech_fallback_from'] = first_attempt
        atomic_json(directory / 'report.json', fallback)
        return result, fallback
    if (report['status'] == 'completed' and pending_cache
            and model_identity(provider.model_size) == report['speech_identity']):
        for path, entry in pending_cache.items():
            try:
                atomic_json(path, entry)
            except OSError:
                # This is a derived cache. The evidence report was already published.
                report['speech_cache_write_failed'] = True
                publish()
                break
    return [replacements.get(cue.index, cue) for cue in cues if cue.index not in excluded], report

AUTO_FALLBACK_VERSION = 'source-final-arbitration-v1'


def auto_adjudicate_unresolved_cues(cues, evidence):
    """Return the surviving cues and decisions from completed source evidence."""
    originals = {row['index']: row for row in evidence.get('input_cues', [])}
    reviewed = {row['cue_id']: row for row in evidence.get('decisions', [])}
    decisions, output = [], []
    trusted = [cue for cue in cues if not cue.needs_source_review]
    han = re.compile(r'[\u3400-\u9fff]')
    for cue in cues:
        if not cue.needs_source_review:
            output.append(cue)
            continue
        row = reviewed.get(cue.index, {})
        original = originals.get(cue.index, {})
        views = row.get('observations', [])
        if (original.get('start'), original.get('end'), original.get('text')) != (cue.start, cue.end, cue.text) or (
                'speech' not in row or len(views) != 3 or len({v['frame_index'] for v in views}) != 3
                or any(len(v['variants']) != 2 for v in views)):
            output.append(cue)
            continue

        neighbors = [other for other in trusted
                     if other.start < cue.end + .35 and other.end > cue.start - .35]
        scores, frames = {}, {}
        for view in views:
            for variant in view['variants']:
                for box in variant:
                    text = box['text'].strip(' ()（）')
                    xs, ys = zip(*box['box'])
                    if (len(han.findall(text)) < 2 or not min(xs) < view['width']/2 < max(xs)
                            or max(ys)-min(ys) < .12 * view['height']):
                        continue
                    key = alignment_key(text)
                    if not key or any(key == alignment_key(n.effective_source_text) for n in neighbors):
                        continue
                    frames.setdefault(key, set()).add(view['frame_index'])
                    if key not in scores or box['confidence'] > scores[key][0]:
                        scores[key] = (box['confidence'], text)
        ranked = [(len(frames[k]), confidence, text) for k, (confidence, text) in scores.items()
                  if len(frames[k]) >= 2 or confidence >= 72]
        if ranked:
            _, confidence, text = max(ranked)
            reason = 'auto_resolved_visual_fallback'
        else:
            candidates = []
            offset = row.get('audio_offset', 0)
            for segment in row['speech']:
                words = []
                for word in segment.get('words', []):
                    start, end = word['start'] + offset, word['end'] + offset
                    if (end <= start or not cue.start <= (start+end)/2 < cue.end
                            or min(end, cue.end)-max(start, cue.start) < .6*(end-start)
                            or any(n.start <= (start+end)/2 < n.end for n in neighbors)):
                        continue
                    words.append(word)
                if words:
                    spoken = ''.join(word['word'] for word in words).strip()
                    probability = sum(word['probability'] for word in words)/len(words)
                    if (len(han.findall(spoken)) >= 2 and probability >= .6
                            and not any(alignment_key(spoken) in alignment_key(n.effective_source_text)
                                        for n in neighbors)):
                        candidates.append((probability, spoken))
            if candidates:
                probability, text = max(candidates)
                confidence = probability * 100
                reason = 'auto_resolved_asr_fallback'
            else:
                reason = 'auto_excluded_residual_noise'
                text = None
        decision = dict(row, original=cue.model_dump(mode='json'), reason_code=reason,
                        needs_source_review=False)
        if text:
            decision.update(action='recovered', text=text, resolution_reason=reason)
            output.append(cue.model_copy(update=dict(corrected_text=text, is_approved=True,
                                                    confidence=confidence, ocr_alternatives=[],
                                                    text_classification='subtitle')))
        else:
            decision.update(action='discarded_non_caption', is_ignored=True, exclusion_reason=reason)
        decisions.append(decision)
    return output, decisions
