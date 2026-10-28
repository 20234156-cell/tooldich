"""Exercise actual UI preset/edit/reload functions with read-only API fixtures."""
import functools
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import json
from pathlib import Path
import threading
from playwright.sync_api import sync_playwright


def main():
    root = Path(__file__).resolve().parents[1]
    output = root / 'storage/audits/reference_subtitle_v2_20261024'
    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(SimpleHTTPRequestHandler, directory=str(root / 'web')))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as runtime:
            browser = runtime.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 1080})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/api/**', lambda route: route.fulfill(status=200, content_type='application/json', body='[]'))
            page.goto(f'http://127.0.0.1:{server.server_port}/', wait_until='networkidle')
            report = page.evaluate("""async () => {
              document.getElementById('sub-reference-preset').click();
              const initial = collectRenderSettings();
              if(initial.cover_effect !== 'blur' || initial.font_scale_x !== 98 || initial.font_shear !== .16 || !initial.cover_full_width || initial.text_width_percent !== 84 || !initial.font_italic || !initial.uppercase)
                throw Error('Preset not collected');
              document.getElementById('sub-blur').value = 45;
              document.getElementById('sub-outline').value = 4;
              document.getElementById('sub-font-color').value = '#ffbb44';
              const edited = collectRenderSettings();
              loadExtraSubtitleSettings(edited);
              const reloaded = collectRenderSettings();
              if(JSON.stringify(edited) !== JSON.stringify(reloaded)) throw Error('Reload mismatch');
              if(edited.font_color !== '&H0044bbff') throw Error('ASS color channel mismatch');
              const url = API.getRenderPreviewUrl('reference-audit', {...edited, t:1});
              if(!url.includes('blur_strength=45') || !url.includes('font_name=Anton')) throw Error('Preview mismatch');
              // Show actual controls without loading or editing any user project.
              document.querySelectorAll('.view').forEach(el => el.style.display='none');
              const studio = document.getElementById('studio-sub');
              const clone = studio.cloneNode(true);
              clone.id = 'audit-visible-sub'; clone.style.display='block';
              const original = studio.querySelectorAll('input,select');
              clone.querySelectorAll('input,select').forEach((el,i) => {el.value=original[i].value;el.checked=original[i].checked;});
              document.body.replaceChildren(clone);
              return {initial, edited, reloaded, preview_url:url};
            }""")
            page.screenshot(path=str(output / 'controls.png'), full_page=True)
            report['javascript_errors'] = errors
            (output / 'browser_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            browser.close()
            assert not errors, errors
            print('Preset, edits, color conversion, reload and preview URL passed in Chromium.')
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
