"""Read-only playback and byte-range audit of the actual served MP4."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
from playwright.sync_api import sync_playwright
from config.settings import settings
from src.pipeline.orchestrator import orchestrator


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project_id')
    args = parser.parse_args()
    root = orchestrator.get_project_dir(args.project_id)
    qa = json.loads((root/'output/verify_report.json').read_text(encoding='utf-8'))
    if not qa.get('passed'):
        raise RuntimeError('Full encoded pipeline QA must pass before browser playback')
    directory = root/'audit/browser_encoded_delivery'
    directory.mkdir(parents=True, exist_ok=True)
    headers = {'X-API-Token': settings.API_TOKEN} if settings.API_TOKEN else {}
    url = 'http://127.0.0.1:8088/api/projects/'+args.project_id+'/files/dubbed_video'
    with requests.get(url, headers={**headers, 'Range':'bytes=0-65535'}, timeout=20, stream=True) as response:
        response.raise_for_status()
        assert response.status_code == 206
        data = response.raw.read(65537)
        with (root/'output/video_vi.mp4').open('rb') as stream:
            assert data == stream.read(65536)
        range_header = response.headers.get('Content-Range')
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(extra_http_headers=headers, viewport={'width':1440,'height':1000})
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://127.0.0.1:8088', wait_until='networkidle')
        page.evaluate('''url=>{
          const video = document.createElement('video'); video.id='audit-delivery';
          video.muted=true; video.controls=true; video.src=url;
          video.style='position:fixed;inset:0;width:100%;height:100%;background:black;z-index:999999';
          document.body.append(video);
        }''', url)
        page.wait_for_function("document.getElementById('audit-delivery').readyState>=1", timeout=30000)
        duration = page.evaluate("document.getElementById('audit-delivery').duration")
        samples = []
        for label, position in [('start', 15), ('middle', duration/2), ('end', duration-4)]:
            page.evaluate('''async t=>{const v=document.getElementById('audit-delivery');
              v.currentTime=t; await v.play();}''', position)
            page.wait_for_function("t=>{const v=document.getElementById('audit-delivery');return v.currentTime>t+.2 && v.readyState>=2}", arg=position, timeout=30000)
            result = page.evaluate("()=>{const v=document.getElementById('audit-delivery');v.pause();return {time:v.currentTime,width:v.videoWidth,height:v.videoHeight,error:v.error?.code||null}}")
            assert result['error'] is None and result['width'] == 1920 and result['height'] == 1080
            page.screenshot(path=str(directory/f'{label}.png'))
            samples.append({'label':label, **result})
        browser.close()
    assert not errors
    report = {'passed':True, 'scope':'Real HTTP byte-range equality and Chromium decoded playback samples',
              'http_status':206, 'content_range':range_header, 'duration':duration,
              'samples':samples, 'javascript_errors':errors}
    (directory/'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
