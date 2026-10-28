"""Read an authenticated video page using normal browser navigation.

No challenge solving, signature generation or browser credential extraction.
"""
from http.cookiejar import MozillaCookieJar
from pathlib import Path
import hashlib
import json
import threading
import time

from config.settings import settings

_profile_lock = threading.Lock()


def _seed_profile(context, cookies: list, marker: Path) -> None:
    # Ignore cookie-file formatting/expired entries; preserve browser-refreshed state.
    identity = hashlib.sha256(json.dumps(sorted(cookies, key=lambda c: (c['domain'], c['path'], c['name'])),
                                           sort_keys=True).encode()).hexdigest()
    if marker.is_file() and marker.read_text(encoding='ascii') == identity:
        return
    context.add_cookies(cookies)
    marker.write_text(identity, encoding='ascii')


def read_video_page(video_id: str, cookie_file: str, timeout_seconds: int) -> dict:
    from playwright.sync_api import sync_playwright

    jar = MozillaCookieJar(str(Path(cookie_file).resolve()))
    jar.load(ignore_discard=True, ignore_expires=True)
    cookies = []
    for cookie in jar:
        if cookie.is_expired() or not (
            cookie.domain.lstrip('.') == 'douyin.com' or cookie.domain.endswith('.douyin.com')
        ):
            continue
        row = dict(name=cookie.name, value=cookie.value, domain=cookie.domain,
                   path=cookie.path, secure=cookie.secure)
        if cookie.expires:
            row['expires'] = float(cookie.expires)
        cookies.append(row)
    if not cookies:
        raise ValueError('No active Douyin cookies')

    detail = None
    statuses = []
    profile = Path(settings.DOUYIN_BROWSER_PROFILE or settings.BASE_DIR/'storage/private/douyin_browser_profile').resolve()
    profile.mkdir(parents=True, exist_ok=True)
    if not _profile_lock.acquire(timeout=timeout_seconds):
        raise ValueError('Douyin browser session is busy; retry after the current request')
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(str(profile), headless=True)
            try:
                _seed_profile(context, cookies, profile/'.cookie_seed')
                page = context.pages[0] if context.pages else context.new_page()

                def on_response(response):
                    nonlocal detail
                    if '/aweme/v1/web/aweme/detail/' not in response.url:
                        return
                    statuses.append(response.status)
                    if response.status != 200:
                        return
                    try:
                        item = response.json().get('aweme_detail')
                        if isinstance(item, dict) and str(item.get('aweme_id')) == video_id:
                            detail = item
                    except (ValueError, TypeError, AttributeError):
                        pass

                page.on('response', on_response)
                deadline = time.monotonic() + timeout_seconds
                page.goto(f'https://www.douyin.com/video/{video_id}',
                          wait_until='domcontentloaded', timeout=timeout_seconds * 1000)
                while detail is None and time.monotonic() < deadline:
                    page.wait_for_timeout(200)
                if detail is None:
                    raise ValueError('Browser did not receive requested video detail; HTTP statuses: '
                                     + ','.join(map(str, sorted(set(statuses)))))
                return {'item': detail, 'user_agent': page.evaluate('navigator.userAgent')}
            finally:
                context.close()
    finally:
        _profile_lock.release()
