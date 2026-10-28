"""Read-only browser smoke test against a running Studio instance."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8088")
    parser.add_argument("--output", type=Path, default=Path("storage/ui_audit/browser"))
    parser.add_argument("--project-id")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    errors, failures, pagination, subtitle_controls = [], [], {}, {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("response", lambda response: failures.append({"status": response.status, "path": response.url.split("?")[0]}) if response.status >= 400 else None)
        response = page.goto(args.url, wait_until="networkidle", timeout=60000)
        project_count = page.locator('#project-rows tr').count()
        if args.project_id:
            page.locator('#project-rows tr').filter(has_text=args.project_id).get_by_role('button', name='Mở Studio').click()
            page.wait_for_function("document.getElementById('detail-video').readyState >= 1", timeout=60000)
            pagination = page.evaluate("""() => {
              const results = {};
              for (const [view, render] of [['ocr', renderCuesTable], ['trans', renderTransTable], ['voice', renderVoiceTable]]) {
                render();
                const pager = document.getElementById(`cue-pager-${view}`);
                if (!pager || pager.hidden) { results[view] = {needed: false}; continue; }
                const first = pager.querySelector('span').textContent;
                pager.querySelector('button:last-child').click();
                const second = pager.querySelector('span').textContent;
                pager.querySelector('button:first-child').click();
                const restored = pager.querySelector('span').textContent;
                results[view] = {needed: true, first, second, passed: first !== second && restored === first};
              }
              return results;
            }""")
            subtitle_controls = page.evaluate("""async () => {
              const cfg = await API.getSubtitleSettings(currentProjectId);
              const font = document.getElementById('sub-font-size');
              const opacity = document.getElementById('cover-box-opacity');
              return {font_size: Number(font.value), saved_font_size: cfg.font_size,
                      font_max: Number(font.max), opacity: Number(opacity.value),
                      saved_opacity: cfg.box_opacity,
                      passed: Number(font.value) === cfg.font_size && Number(opacity.value) === cfg.box_opacity};
            }""")
        page.screenshot(path=str(args.output / "studio.png"), full_page=True)
        report = {"http_status": response.status, "title": page.title(),
                  "javascript_errors": errors, "http_errors": failures,
                  "project_count": project_count, "pagination": pagination,
                  "subtitle_controls": subtitle_controls}
        browser.close()
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return int(bool(errors or failures or subtitle_controls.get('passed') is False
                    or any(row.get('passed') is False for row in pagination.values())))


if __name__ == "__main__":
    raise SystemExit(main())
