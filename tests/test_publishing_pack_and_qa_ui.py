"""Offline contracts for channel copy, SSOT identity and the actual QA UI."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from config.settings import settings
from src.database.models import Project, Transcript
from src.ocr.base import Cue
from src.pipeline import publishing_pack as pub


def sample_cues():
    return [Cue(index=1, start=0, end=3, vi_text='Bạn có muốn cùng tôi tìm hiểu câu chuyện này không?', text='中文'),
            Cue(index=2, start=3, end=5, vi_text='Tiếng cười riêng không dùng', caption_role='nonverbal_laughter'),
            Cue(index=3, start=5, end=8, vi_text='Chúng ta hãy bắt đầu hành trình mới ngay hôm nay!', text='开始')]


@pytest.mark.parametrize('title', ['Câu chuyện về một hành trình mới đầy bất ngờ và những người bạn cùng nhau vượt qua khó khăn để tìm lại niềm tin trong cuộc sống', '中文 Câu chuyện tiếng Việt ' * 15, 'x'*120, '', '中文', 'a\u0301 '*70])
def test_enforce_youtube_title_limit(title):
    result = pub.enforce_youtube_title_limit(title, 200)
    assert 1 <= len(result) <= 95
    assert '中' not in result and '文' not in result
    assert pub.enforce_youtube_title_limit(result) == result


def test_word_boundary():
    result = pub.enforce_youtube_title_limit('hành trình tuyệt vời ' * 12)
    assert result.endswith('…')
    assert result[:-1] in ('hành trình tuyệt vời ' * 12)


@pytest.mark.parametrize('title', ['高武末世废土+机器人主角+异能者+异兽', '當老師進了黑社會【AI】', '以前小孩vs现在小孩'])
def test_offline_pack_replaces_empty_chinese_title(title):
    pack = pub.build_offline_publishing_pack(title, sample_cues())
    assert pack['youtube_titles'][1] == 'Phụ đề Việt: Video thuyết minh Việt'
    assert pack['facebook_post'].startswith('🎬 Video thuyết minh Việt')


def test_long_identical_source_keeps_three_distinct_titles(tmp_path):
    title = 'Hành trình mới cùng những người bạn ' * 8
    cues = [Cue(index=1, start=0, end=4, vi_text=title)]
    pack = pub.generate_publishing_pack('p', tmp_path, title, cues)
    assert pub.valid_pack(pack, pack['content_hash'])
    assert len({t['text'] for t in pack['youtube_titles']}) == 3


def test_publishing_pack_offline_and_identity_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'OFFLINE_MODE', True)
    monkeypatch.setattr(pub, '_gemini_copy', lambda *a: pytest.fail('Network invoked'))
    cues = sample_cues()
    first = pub.generate_publishing_pack('p1', tmp_path, 'Hành trình', cues)
    assert first['provider'] == 'offline_rule' and not first['cache_hit']
    assert (tmp_path/'output/publishing_pack.json').is_file()
    assert 'Tiếng cười riêng' not in json.dumps(first, ensure_ascii=False)
    assert len(first['youtube_titles']) == 3
    assert all(len(t['text']) <= 95 and t['max_chars'] == 95 for t in first['youtube_titles'])
    assert '11:30 & 19:30' in first['youtube_description']
    assert '🎬' in first['facebook_post'] and '#NpzzSub' in first['facebook_post']
    assert pub.generate_publishing_pack('p1', tmp_path, 'Hành trình', cues)['cache_hit']
    cues[0].vi_text = 'Lời thoại đã thay đổi trong câu chuyện này.'
    assert pub.load_publishing_pack('p1', tmp_path, 'Hành trình', cues) is None
    changed = pub.generate_publishing_pack('p1', tmp_path, 'Hành trình', cues)
    assert not changed['cache_hit'] and changed['content_hash'] != first['content_hash']
    assert not pub.generate_publishing_pack('p1', tmp_path, 'Hành trình', cues, force=True)['cache_hit']


def test_empty_and_corrupt_cache(tmp_path):
    with pytest.raises(ValueError, match='chưa có phụ đề'):
        pub.generate_publishing_pack('p', tmp_path, '', [])
    pub.generate_publishing_pack('p', tmp_path, '', sample_cues())
    (tmp_path/'output/publishing_pack.json').write_text('{}', encoding='utf-8')
    assert not pub.generate_publishing_pack('p', tmp_path, '', sample_cues())['cache_hit']


def test_write_failure_is_not_hidden(tmp_path, monkeypatch):
    def broken(*args):
        raise PermissionError('locked')
    monkeypatch.setattr(pub, 'atomic_json', broken)
    with pytest.raises(PermissionError):
        pub.generate_publishing_pack('p', tmp_path, '', sample_cues())


@pytest.mark.parametrize('kind', ['valid', 'invalid', 'network', 'no_key'])
def test_ai_adapter_with_explicit_mock(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(pub, '_online_allowed', lambda: True)
    def mocked(*args):
        if kind == 'network':
            raise TimeoutError('offline simulation')
        if kind == 'no_key':
            return None
        if kind == 'invalid':
            return {'youtube_titles': []}
        return pub.build_offline_publishing_pack('Mẫu', sample_cues())
    monkeypatch.setattr(pub, '_gemini_copy', mocked)
    result = pub.generate_publishing_pack('p', tmp_path, 'Mẫu', sample_cues())
    assert result['provider'] == ('gemini' if kind == 'valid' else 'offline_rule')
    assert pub.valid_pack(result, result['content_hash'])


def test_usage_write_failure_propagates(tmp_path, monkeypatch):
    monkeypatch.setattr(pub, '_online_allowed', lambda: True)
    def broken(*args):
        raise pub.UsagePersistenceError('usage DB failed')
    monkeypatch.setattr(pub, '_gemini_copy', broken)
    with pytest.raises(pub.UsagePersistenceError):
        pub.generate_publishing_pack('p', tmp_path, 'Mẫu', sample_cues())


def test_publishing_pack_api_endpoints(db_session):
    from src.api.app import app
    from src.pipeline.orchestrator import orchestrator
    project_id = 'proj_publishing_test'
    db_session.add(Project(id=project_id, title='Hành trình'))
    db_session.add(Transcript(project_id=project_id, segments=[c.model_dump() for c in sample_cues()]))
    db_session.commit()
    with TestClient(app) as client:
        url = f'/api/projects/{project_id}/publishing-pack'
        assert client.get(url).json() == {'exists':False, 'pack':None}
        first = client.post(url, json={}).json()
        assert first['provider'] == 'offline_rule'
        assert client.get(url).json()['pack']['cache_hit']
        assert client.get(f'/api/projects/{project_id}/files/publishing_pack').json()['content_hash'] == first['content_hash']
        cues = orchestrator.get_cues(project_id)
        cues[0].vi_text = 'Nội dung mới đã được sửa lại từ database.'
        orchestrator.save_cues(project_id, cues)
        assert client.get(url).json()['exists'] is False
        assert client.get(f'/api/projects/{project_id}/files/publishing_pack').status_code == 409
        second = client.post(url, json={}).json()
        assert second['content_hash'] != first['content_hash']
        assert client.post(url, json={'force':True}).json()['cache_hit'] is False
        from src.api.app import _publishing_locks
        lock = _publishing_locks[hash(project_id) % len(_publishing_locks)]
        lock.acquire()
        try:
            assert client.post(url, json={}).status_code == 409
        finally:
            lock.release()
        orchestrator.save_cues(project_id, [])
        assert client.post(url, json={}).status_code == 400
        assert client.get('/api/projects/proj_nonexistent/publishing-pack').status_code >= 400


def test_actual_qa_ui_audio_and_safe_messages():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    source = (Path(__file__).resolve().parents[1]/'web/app.js').read_text(encoding='utf-8')
    function = source[source.index('function renderVerifyScorecard('):source.index('function updateDownloadLinks(')]
    script = '''
const box={children:[],style:{},replaceChildren(){this.children=[];},appendChild(row){this.children.push(row);},get childElementCount(){return this.children.length;}};
const DOM=Object.fromEntries(['qaMetricAudio','qaMetricDuration','qaMetricDrift','qaMetricCoverage','qaMetricDegraded'].map(k=>[k,{}]));
DOM.qaMessagesBox=box;
const document={createElement:()=>({})};
''' + function + '''
renderVerifyScorecard({audio_metrics:{integrated_lufs:-14,true_peak_dbtp:-1.5},errors:['<img src=x onerror=alert(1)>'],warnings:['review']});
if(DOM.qaMetricAudio.className!=='qa-pass' || box.children[0].textContent!=='Lỗi: <img src=x onerror=alert(1)>')throw Error('audio/XSS');
renderVerifyScorecard({audio_metrics:{integrated_lufs:-13,true_peak_dbtp:-1}});
if(DOM.qaMetricAudio.className!=='qa-fail' || box.style.display!=='none')throw Error('bad audio');
renderVerifyScorecard({});
if(DOM.qaMetricAudio.className!=='qa-warn')throw Error('missing measurement');
'''
    subprocess.run([node, '-e', script], check=True, capture_output=True, timeout=10)


def test_publishing_ui_ignores_late_project_response():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    source = (Path(__file__).resolve().parents[1]/'web/app.js').read_text(encoding='utf-8')
    functions = source[source.index('let publishingRequestGeneration'):source.index('// 1-Click Run-All')]
    script = '''
function element(){return {children:[],style:{},replaceChildren(){this.children=[];},append(...items){this.children.push(...items);},appendChild(item){this.children.push(item);},removeAttribute(k){delete this[k];},addEventListener(){}};}
const elements={};
const document={getElementById:id=>elements[id] ||= element(),createElement:()=>element()};
const DOM={linkDownloadPublishingPack:element()};
const listeners={};const window={addEventListener:(type,handler)=>{listeners[type]=handler;}};
let currentProjectId='projectA'; let resolveA;
const API={getPublishingPack:()=>new Promise(resolve=>{resolveA=resolve;})};
''' + functions + '''
(async()=>{
 listeners['workspace-project-open']();
 if(!DOM.linkDownloadPublishingPack.hidden)throw Error('open did not clear old pack');
 const pending=loadPublishingPack();
 currentProjectId='projectB';
 resolveA({pack:{provider:'gemini',youtube_titles:[{text:'wrong project',char_count:13}],hashtags:[]}});
 await pending;
 if(!DOM.linkDownloadPublishingPack.hidden || elements['pub-youtube-titles'].children.length)throw Error('stale project');
 renderPublishingPack({provider:'offline_rule',cache_hit:true,youtube_titles:[{text:'<script>literal</script>',char_count:24}],hashtags:['#NpzzSub'],youtube_description:'desc',facebook_post:'post'});
 if(elements['pub-youtube-titles'].children[0].children[0].textContent.indexOf('<script>')<0)throw Error('unsafe rendering');
 if(!DOM.linkDownloadPublishingPack.href.includes('projectB'))throw Error('download identity');
 renderPublishingPack(null);
 if(DOM.linkDownloadPublishingPack.href || !DOM.linkDownloadPublishingPack.hidden)throw Error('stale download');
})().catch(err=>{console.error(err);process.exitCode=1;});
'''
    subprocess.run([node, '-e', script], check=True, capture_output=True, timeout=10)


def long_publishing_inputs():
    """A seven-minute source with real voice tails and one diagnostic row."""
    cues = [Cue(index=i+1, start=i*14.0, end=i*14.0+11.0,
                text=f'原文{i+1}', vi_text=f'Câu chuyện số {i+1}: Chúng ta cùng bắt đầu hành trình mới.')
            for i in range(30)]
    rows = [dict(cue_index=c.index, schedule_status='placed',
                 actual_start=c.start+.25, actual_end=c.end+1.25)
            for c in cues]
    rows[4] = dict(cue_index=5, schedule_status='unplaced',
                   actual_start=cues[4].start, actual_end=999999.0)
    return cues, rows


def test_part_splits_never_cut_inside_spoken_or_subtitle_spans(tmp_path):
    cues, rows = long_publishing_inputs()
    pack = pub.generate_publishing_pack('parts', tmp_path, 'Hành trình', cues,
                                       timeline_rows=rows, target_part_seconds=180)
    parts = pack['part_splits']
    assert len(parts) >= 2
    assert parts[0]['start_seconds'] == 0
    assert parts[-1]['end_seconds'] == pytest.approx(418.25)
    assert sum(p['cue_count'] for p in parts) == len(cues)
    assert [p['part_index'] for p in parts] == list(range(1, len(parts)+1))
    assert all(p['total_parts'] == len(parts) for p in parts)
    protected = []
    for cue, row in zip(cues, rows):
        start, end = cue.start, cue.end
        if row['schedule_status'] != 'unplaced':
            start, end = min(start, row['actual_start']), max(end, row['actual_end'])
        protected.append((start, end))
    for previous, following in zip(parts, parts[1:]):
        cut = previous['end_seconds']
        assert following['start_seconds'] == cut
        assert not any(start < cut < end for start, end in protected)
        left = max(end for start, end in protected if end <= cut)
        right = min(start for start, end in protected if start >= cut)
        assert left <= cut <= right
    for part in parts:
        assert part['duration_seconds'] == pytest.approx(part['end_seconds']-part['start_seconds'], abs=.001)
        assert 1 <= len(part['title']) <= 95
        assert part['char_count'] == len(part['title'])
        assert pub.enforce_youtube_title_limit(part['title'], 95) == part['title']
        assert part['lead_text'] in {c.vi_text for c in cues}
        assert '-c copy' in part['ffmpeg_copy_cmd']
        assert 'output/video_vi.mp4' in part['ffmpeg_copy_cmd']


def test_protected_spans_merge_overlapping_voice_even_when_voice_order_changes():
    cues = [Cue(index=1, start=10, end=12, vi_text='Câu đầu tiên.'),
            Cue(index=2, start=20, end=22, vi_text='Câu thứ hai.'),
            Cue(index=3, start=30, end=32, vi_text='Câu thứ ba.')]
    rows = [dict(cue_index=1, schedule_status='placed', actual_start=9, actual_end=14),
            dict(cue_index=2, schedule_status='placed', actual_start=8, actual_end=23),
            dict(cue_index=3, schedule_status='unplaced', actual_start=0, actual_end=99999)]
    spans = pub.collect_protected_speech_spans(cues, rows)
    assert len(spans) == 2
    assert (spans[0]['start'], spans[0]['end']) == (8, 23)
    assert {c['cue_index'] for c in spans[0]['cues']} == {1, 2}
    assert (spans[1]['start'], spans[1]['end']) == (30, 32)


@pytest.mark.parametrize('actual_start,actual_end', [(float('nan'), 500), (0, float('inf')), (-1, 500), (4, 3), (True, 500), ('0', 500)])
def test_invalid_voice_bounds_do_not_replace_subtitle_bounds(actual_start, actual_end):
    cue = Cue(index=1, start=10, end=12, vi_text='Dòng phụ đề thật.')
    spans = pub.collect_protected_speech_spans([cue], [dict(cue_index=1, schedule_status='placed',
        actual_start=actual_start, actual_end=actual_end)])
    assert [(s['start'], s['end']) for s in spans] == [(10, 12)]


def test_duplicate_published_voice_rows_are_conservatively_protected():
    cue = Cue(index=1, start=10, end=12, vi_text='Dòng phụ đề thật.')
    spans = pub.collect_protected_speech_spans([cue], [
        dict(cue_index=1, schedule_status='placed', actual_start=8, actual_end=13),
        dict(cue_index=1, actual_start=9, actual_end=15),
        dict(cue_index=1, schedule_status='unplaced', actual_start=0, actual_end=99999)])
    assert [(s['start'], s['end']) for s in spans] == [(8, 15)]


@pytest.mark.parametrize('huge', [10**400, 1e308])
def test_overflowing_timeline_numbers_do_not_break_publishing(huge):
    cue = Cue(index=1, start=0, end=12, vi_text='Mốc phụ đề vẫn hợp lệ.')
    spans = pub.collect_protected_speech_spans([cue], [
        dict(cue_index=1, schedule_status='placed', actual_start=0, actual_end=huge)])
    assert spans[0]['end'] == 12


def test_submillisecond_gaps_are_not_rounded_into_spoken_content():
    cues = [Cue(index=1, start=0, end=60.0004, vi_text='Đoạn đầu vẫn đang nói.'),
            Cue(index=2, start=60.0006, end=121, vi_text='Đoạn sau bắt đầu nói.')]
    spans = pub.collect_protected_speech_spans(cues)
    parts = pub.build_safe_part_splits('Khoảng nghỉ rất hẹp', spans, target_part_seconds=60)
    for part in parts[:-1]:
        assert not any(c.start < part['end_seconds'] < c.end for c in cues)


def test_youtube_chapters_start_at_zero_and_monotonic(tmp_path):
    cues, rows = long_publishing_inputs()
    pack = pub.generate_publishing_pack('chapters', tmp_path, 'Hành trình', cues,
                                       timeline_rows=rows, target_part_seconds=180)
    chapters = pack['youtube_chapters']
    assert len(chapters) >= 3
    assert pack['youtube_chapters_text'].startswith('00:00 ')
    assert chapters[0]['timecode'] == '00:00' and chapters[0]['start_seconds'] == 0
    assert all(b['start_seconds']-a['start_seconds'] >= 10 for a, b in zip(chapters, chapters[1:]))
    assert all(1 <= len(c['label']) <= 60 for c in chapters)
    assert pack['youtube_chapters_text'] == '\n'.join(f"{c['timecode']} {c['label']}" for c in chapters)
    source_words = {word.strip(':.,!?') for cue in cues for word in cue.vi_text.split()}
    for chapter in chapters:
        assert any(word in chapter['label'] for word in source_words if len(word) >= 4)


def test_part_splits_keep_continuous_speech_and_merge_short_tail():
    continuous = [Cue(index=i+1, start=i*10, end=(i+1)*10+1, vi_text=f'Lời thoại số {i+1}') for i in range(50)]
    spans = pub.collect_protected_speech_spans(continuous)
    parts = pub.build_safe_part_splits('Liền mạch', spans, target_part_seconds=180)
    assert len(parts) == 1 and parts[0]['cue_count'] == 50
    cues = [Cue(index=i+1, start=i*20, end=i*20+15, vi_text=f'Lời thoại số {i+1}') for i in range(20)]
    parts = pub.build_safe_part_splits('Đoạn kết', pub.collect_protected_speech_spans(cues), target_part_seconds=180)
    assert len(parts) > 1
    assert parts[-1]['duration_seconds'] >= 35


def test_publishing_timing_and_target_are_cache_identity(tmp_path):
    cues, rows = long_publishing_inputs()
    first = pub.generate_publishing_pack('identity', tmp_path, 'Hành trình', cues,
                                       timeline_rows=rows, target_part_seconds=180)
    assert pub.generate_publishing_pack('identity', tmp_path, 'Hành trình', cues,
                                       timeline_rows=rows, target_part_seconds=180)['cache_hit']
    resized = pub.generate_publishing_pack('identity', tmp_path, 'Hành trình', cues,
                                         timeline_rows=rows, target_part_seconds=120)
    assert not resized['cache_hit'] and resized['content_hash'] != first['content_hash']
    rows[1]['actual_end'] += .5
    retimed = pub.generate_publishing_pack('identity', tmp_path, 'Hành trình', cues,
                                         timeline_rows=rows, target_part_seconds=120)
    assert not retimed['cache_hit'] and retimed['content_hash'] != resized['content_hash']
    cues[1].end += .75
    recued = pub.generate_publishing_pack('identity', tmp_path, 'Hành trình', cues,
                                        timeline_rows=rows, target_part_seconds=120)
    assert not recued['cache_hit'] and recued['content_hash'] != retimed['content_hash']
    path = tmp_path/'output/publishing_pack.json'
    saved = json.loads(path.read_text(encoding='utf-8'))
    saved.pop('youtube_chapters')
    path.write_text(json.dumps(saved, ensure_ascii=False), encoding='utf-8')
    assert not pub.generate_publishing_pack('identity', tmp_path, 'Hành trình', cues,
        timeline_rows=rows, target_part_seconds=120)['cache_hit']


def test_publishing_parts_api_uses_timeline_and_validates_target(db_session):
    from src.api.app import app
    from src.pipeline.orchestrator import orchestrator
    project_id = 'proj_parts_api'
    cues, rows = long_publishing_inputs()
    db_session.add(Project(id=project_id, title='Hành trình'))
    db_session.add(Transcript(project_id=project_id, segments=[c.model_dump() for c in cues]))
    db_session.commit()
    timeline = orchestrator.get_project_dir(project_id)/'voiceover/timeline.json'
    timeline.parent.mkdir(parents=True, exist_ok=True)
    timeline.write_text(json.dumps(rows), encoding='utf-8')
    with TestClient(app) as client:
        url = f'/api/projects/{project_id}/publishing-pack'
        for invalid in (59, 901):
            assert client.post(url, json={'target_part_seconds':invalid}).status_code == 422
        result = client.post(url, json={'target_part_seconds':120})
        assert result.status_code == 200
        pack = result.json()
        assert pack['target_part_seconds'] == 120
        assert pack['part_splits'][-1]['end_seconds'] == pytest.approx(418.25)
        assert client.get(url, params={'target_part_seconds':120}).json()['exists']
        assert client.get(url, params={'target_part_seconds':180}).json()['exists'] is False
        assert client.get(f'/api/projects/{project_id}/files/publishing_pack', params={'target_part_seconds':120}).status_code == 200
        assert client.get(f'/api/projects/{project_id}/files/publishing_pack').status_code == 409
        rows[-1]['actual_end'] += 2
        timeline.write_text(json.dumps(rows), encoding='utf-8')
        assert client.get(url, params={'target_part_seconds':120}).json()['exists'] is False
        assert client.get(f'/api/projects/{project_id}/files/publishing_pack', params={'target_part_seconds':120}).status_code == 409
        timeline.write_text('{not-valid-json', encoding='utf-8')
        assert client.get(url, params={'target_part_seconds':120}).json()['exists'] is False
        assert client.post(url, json={'target_part_seconds':120}).status_code == 400


def test_actual_publishing_ui_renders_chapters_parts_and_copy_safely():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    source = (Path(__file__).resolve().parents[1]/'web/app.js').read_text(encoding='utf-8')
    functions = source[source.index('let publishingRequestGeneration'):source.index('// 1-Click Run-All')]
    script = '''
function element(){return {children:[],events:{},value:'',style:{},replaceChildren(){this.children=[];},append(...items){this.children.push(...items);},appendChild(item){this.children.push(item);},removeAttribute(k){delete this[k];},addEventListener(type,handler){this.events[type]=handler;}};}
const elements={};
const document={getElementById:id=>elements[id] ||= element(),createElement:()=>element()};
const DOM={linkDownloadPublishingPack:element()};
const listeners={};const window={addEventListener:(type,handler)=>{listeners[type]=handler;}};
const copied=[];const navigator={clipboard:{writeText:async text=>copied.push(text)}};
const setTimeout=()=>0;
let currentProjectId='projectParts';
const API={};
''' + functions + '''
(async()=>{
 const title='<img src=x onerror=bad()> Câu thoại thật';
 const command='ffmpeg -hide_banner -ss 0.000 -to 178.400 -i output/video_vi.mp4 -c copy output/part_01.mp4';
 const chapters='00:00 Mở đầu: Câu thoại thật\\n02:58 Câu chuyện tiếp theo';
 renderPublishingPack({provider:'offline_rule',target_part_seconds:180,youtube_titles:[],hashtags:[],youtube_chapters_text:chapters,
 youtube_chapters_warning:'Cần duyệt chương',part_splits_warning:'Copy có thể lệch keyframe',
 part_splits:[{part_index:1,total_parts:3,start_timecode:'00:00',end_timecode:'02:58',duration_seconds:178.4,cue_count:38,title,char_count:title.length,ffmpeg_copy_cmd:command}]});
 if(elements['pub-youtube-chapters'].value!==chapters || elements['btn-copy-yt-chapters'].disabled)throw Error('chapters missing');
 const row=elements['pub-part-splits'].children[0];
 if(!row.children[0].textContent.includes('178.4s · 38 câu') || !row.children[1].textContent.includes(title))throw Error('part summary/XSS');
 await row.children[2].events.click();await row.children[3].events.click();await elements['btn-copy-yt-chapters'].events.click();
 if(JSON.stringify(copied)!==JSON.stringify([title,command,chapters]))throw Error('copy mismatch');
 if(!DOM.linkDownloadPublishingPack.href.endsWith('?target_part_seconds=180'))throw Error('download target mismatch');
 renderPublishingPack(null);
 if(elements['pub-youtube-chapters'].value || elements['pub-part-splits'].children.length || !elements['btn-copy-yt-chapters'].disabled)throw Error('stale chapters/parts');
})().catch(err=>{console.error(err);process.exitCode=1;});
'''
    subprocess.run([node, '-e', script], check=True, capture_output=True, timeout=10)


def test_actual_publishing_ui_ignores_late_duration_response_and_sends_selection():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    source = (Path(__file__).resolve().parents[1]/'web/app.js').read_text(encoding='utf-8')
    functions = source[source.index('let publishingRequestGeneration'):source.index('// 1-Click Run-All')]
    script = '''
function element(){return {children:[],events:{},value:'',style:{},replaceChildren(){this.children=[];},append(...items){this.children.push(...items);},appendChild(item){this.children.push(item);},removeAttribute(k){delete this[k];},addEventListener(type,handler){this.events[type]=handler;}};}
const elements={};
const document={getElementById:id=>elements[id] ||= element(),createElement:()=>element()};
const DOM={linkDownloadPublishingPack:element()};
const window={addEventListener(){}};
let currentProjectId='sameProject';
const reads=[],creates=[];
const API={getPublishingPack:(id,seconds)=>new Promise(resolve=>reads.push({id,seconds,resolve})),createPublishingPack:(id,force,seconds)=>new Promise(resolve=>creates.push({id,force,seconds,resolve}))};
const pack=seconds=>({target_part_seconds:seconds,provider:'offline_rule',youtube_titles:[{text:'Target '+seconds,char_count:10}],hashtags:[],youtube_chapters_text:'00:00 Target '+seconds,part_splits:[]});
''' + functions + '''
(async()=>{
 document.getElementById('pub-part-duration').value='120';
 const pending=createPublishingPack(true);
 if(creates[0].id!=='sameProject'||creates[0].seconds!==120||creates[0].force!==true)throw Error('create target omitted');
 document.getElementById('pub-part-duration').value='600';
 elements['pub-part-duration'].events.change();
 creates[0].resolve(pack(120));await pending;
 if(elements['pub-youtube-chapters'].value || elements['pub-part-splits'].children.length)throw Error('late old target rendered');
 if(reads.length!==1||reads[0].seconds!==600)throw Error('new target was not fetched');
 reads[0].resolve({pack:pack(600)});await Promise.resolve();await Promise.resolve();
 if(elements['pub-youtube-chapters'].value!=='00:00 Target 600')throw Error('new target missing');
 const oldRead=loadPublishingPack();
 document.getElementById('pub-part-duration').value='300';
 elements['pub-part-duration'].events.change();
 reads[1].resolve({pack:pack(600)});await oldRead;
 if(elements['pub-youtube-chapters'].value)throw Error('late GET old target rendered');
 reads[2].resolve({pack:pack(300)});await Promise.resolve();await Promise.resolve();
 if(elements['pub-youtube-chapters'].value!=='00:00 Target 300')throw Error('selected target missing');
})().catch(err=>{console.error(err);process.exitCode=1;});
'''
    subprocess.run([node, '-e', script], check=True, capture_output=True, timeout=10)


def test_chapters_do_not_force_integer_timestamps_into_fractional_speech_gaps():
    cues = [Cue(index=1, start=0, end=20.1, vi_text='Nội dung mở đầu đang được trình bày.'),
            Cue(index=2, start=20.9, end=40.1, vi_text='Nội dung tiếp theo đang được trình bày.'),
            Cue(index=3, start=40.9, end=61, vi_text='Nội dung cuối cùng đang được trình bày.')]
    spans = pub.collect_protected_speech_spans(cues)
    parts = pub.build_safe_part_splits('Khoảng nghỉ lẻ giây', spans, target_part_seconds=60)
    result = pub.build_youtube_chapters(spans, parts)
    assert [c['start_seconds'] for c in result['youtube_chapters']] == [0]
    assert result['youtube_chapters_text'].startswith('00:00 ')
    assert result['youtube_chapters_available'] is False
    assert result['youtube_chapters_warning']
    # Rounding the gap midpoint to 20 or 21 seconds would land inside speech.
    assert all(not c.start < chapter['start_seconds'] < c.end
               for c in cues for chapter in result['youtube_chapters'])


def test_chapter_integer_quantization_stays_inside_actual_safe_gaps():
    cues = [Cue(index=1, start=0, end=20.1, vi_text='Nội dung mở đầu đang được trình bày.'),
            Cue(index=2, start=21.1, end=40.1, vi_text='Nội dung tiếp theo đang được trình bày.'),
            Cue(index=3, start=41.1, end=61, vi_text='Nội dung cuối cùng đang được trình bày.')]
    spans = pub.collect_protected_speech_spans(cues)
    result = pub.build_youtube_chapters(spans, pub.build_safe_part_splits('Chương an toàn', spans, target_part_seconds=60))
    assert [c['start_seconds'] for c in result['youtube_chapters']] == [0, 21, 41]
    assert result['youtube_chapters_available'] is True
    assert not result['youtube_chapters_warning']
    assert all(not cue.start < chapter['start_seconds'] < cue.end
               for cue in cues for chapter in result['youtube_chapters'])


def test_youtube_chapters_use_hour_timecodes_for_long_sources():
    cues = [Cue(index=i+1, start=i*140, end=i*140+120,
                vi_text=f'Đây là nội dung thực tế của đoạn số {i+1}.') for i in range(60)]
    spans = pub.collect_protected_speech_spans(cues)
    parts = pub.build_safe_part_splits('Video dài', spans, target_part_seconds=900)
    result = pub.build_youtube_chapters(spans, parts)
    assert result['youtube_chapters'][0]['timecode'] == '00:00'
    assert any(c['start_seconds'] >= 3600 for c in result['youtube_chapters'])
    for chapter in result['youtube_chapters']:
        seconds = chapter['start_seconds']
        assert int(seconds) == seconds
        hours, remaining = divmod(int(seconds), 3600)
        minutes, secs = divmod(remaining, 60)
        expected = f'{hours:02d}:{minutes:02d}:{secs:02d}' if hours else f'{minutes:02d}:{secs:02d}'
        assert chapter['timecode'] == expected
        assert 1 <= len(chapter['label']) <= 60


def test_corrupt_cached_split_bounds_rejected_with_unchanged_content_hash(tmp_path):
    cues, rows = long_publishing_inputs()
    original = pub.generate_publishing_pack('corrupt_parts', tmp_path, 'Hành trình', cues,
                                          timeline_rows=rows, target_part_seconds=180)
    path = tmp_path/'output/publishing_pack.json'
    corrupt = json.loads(path.read_text(encoding='utf-8'))
    corrupt['part_splits'][0]['end_seconds'] = 5.0
    corrupt['part_splits'][0]['duration_seconds'] = 5.0
    corrupt['part_splits'][1]['start_seconds'] = 5.0
    assert corrupt['content_hash'] == original['content_hash']
    assert cues[0].start < corrupt['part_splits'][0]['end_seconds'] < cues[0].end
    path.write_text(json.dumps(corrupt, ensure_ascii=False), encoding='utf-8')
    assert pub.load_publishing_pack('corrupt_parts', tmp_path, 'Hành trình', cues,
                                   timeline_rows=rows, target_part_seconds=180) is None
    repaired = pub.generate_publishing_pack('corrupt_parts', tmp_path, 'Hành trình', cues,
                                          timeline_rows=rows, target_part_seconds=180)
    assert repaired['cache_hit'] is False
    assert repaired['content_hash'] == original['content_hash']
    assert repaired['part_splits'] == original['part_splits']
