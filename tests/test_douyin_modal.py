import pytest
import requests
from unittest.mock import MagicMock, patch

from src.providers.douyin import DouyinProvider
from src.providers.registry import ProviderRegistry
from src.providers.douyin_browser import read_video_page as actual_read_video_page


@pytest.mark.parametrize("path", ["jingxuan", "discover", "jingxuan/"])
@pytest.mark.parametrize("query", ["modal_id=7698300125159935283", "foo=1&modal_id=7698300125159935283&bar=2"])
def test_modal_link_detects_and_resolves_to_video(path, query):
    url = f"https://www.douyin.com/{path}?{query}"
    detection = ProviderRegistry().detect_platform(url)
    assert detection.valid and detection.platform == "douyin"
    provider = DouyinProvider()
    assert provider.extract_video_id(url) == "7698300125159935283"
    assert provider.resolve_url(url) == "https://www.douyin.com/video/7698300125159935283"


@pytest.mark.parametrize("url", [
    "https://douyin.com.attacker.com/jingxuan?modal_id=7698300125159935283",
    "https://attacker.com/?url=douyin.com/jingxuan&modal_id=7698300125159935283",
    "https://www.douyin.com/jingxuan?fake_modal_id=123",
    "https://www.douyin.com/jingxuan?modal_id=123abc",
    "https://www.douyin.com/jingxuan?modal_id=123&modal_id=456",
    "https://www.douyin.com/jingxuan",
])
def test_modal_link_rejects_invalid_host_or_id(url):
    assert not DouyinProvider().validate_url(url)


def test_shortlink_redirect_to_modal_uses_same_canonical_video():
    with patch('src.providers.douyin.safe_resolve_redirects', return_value='https://www.douyin.com/jingxuan?modal_id=7698300125159935283'):
        assert DouyinProvider().resolve_url('https://v.douyin.com/example/') == 'https://www.douyin.com/video/7698300125159935283'


@pytest.mark.parametrize('failure', ['empty', 'timeout', 'wrong_id'])
def test_public_failure_reaches_fallback_and_preserves_media_headers(failure):
    response = MagicMock(status_code=200)
    response.json.side_effect = ValueError('empty response') if failure == 'empty' else None
    response.json.return_value = {'item_list': [{'aweme_id': '999', 'video': {'play_addr': {'url_list': ['https://wrong.mp4']}}}]}
    info = {'id': '7698300125159935283', 'url': 'https://cdn.douyinvod.com/video.mp4',
            'http_headers': {'User-Agent': 'extractor-agent'}, 'duration': 10}
    with patch('src.providers.douyin.requests.get', return_value=response,
               side_effect=requests.Timeout() if failure == 'timeout' else None), \
         patch('yt_dlp.YoutubeDL') as ydl:
        extractor = ydl.return_value.__enter__.return_value
        extractor.extract_info.return_value = info
        media = DouyinProvider().get_media('https://www.douyin.com/jingxuan?modal_id=7698300125159935283')
    extractor.extract_info.assert_called_once_with('https://www.douyin.com/video/7698300125159935283', download=False)
    assert media.success
    assert media.headers['Referer'] == 'https://www.douyin.com/'
    assert media.headers['User-Agent'] == 'extractor-agent'


@pytest.mark.parametrize('info', [None, {'id': '999', 'url': 'https://wrong.mp4'}, {'title': 'no stream'}])
def test_both_extractors_failing_never_publish_placeholder_metadata(info):
    from src.jobs.logger import PipelineException
    response = MagicMock(status_code=200)
    response.json.side_effect = ValueError('empty')
    with patch('src.providers.douyin.requests.get', return_value=response), patch('yt_dlp.YoutubeDL') as ydl:
        ydl.return_value.__enter__.return_value.extract_info.return_value = info
        with pytest.raises(PipelineException) as error:
            DouyinProvider().get_metadata('https://www.douyin.com/video/7698300125159935283')
    assert error.value.code == 'SOURCE_UNAVAILABLE'
    assert 'Expecting value' not in error.value.message


def test_failed_douyin_source_persists_failed_job_and_no_media(db_session):
    from src.jobs.system import ProjectManager
    from src.database.models import Job, Project, Media, Source
    from src.jobs.logger import PipelineException
    manager = ProjectManager()
    project = manager.create_project('https://www.douyin.com/jingxuan?modal_id=7698300125159935283')
    response = MagicMock(status_code=200)
    response.json.side_effect = ValueError('empty')
    with patch('src.providers.douyin.requests.get', return_value=response), patch('yt_dlp.YoutubeDL') as ydl:
        ydl.return_value.__enter__.return_value.extract_info.side_effect = RuntimeError('Fresh cookies needed')
        with pytest.raises(PipelineException):
            manager.download_and_validate_project(project['project_id'])
    db_session.expire_all()
    assert db_session.get(Job, project['job_id']).status == 'FAILED'
    assert db_session.get(Project, project['project_id']).status == 'FAILED'
    assert not db_session.query(Media).filter_by(project_id=project['project_id']).count()
    source = db_session.query(Source).filter_by(project_id=project['project_id']).one()
    assert not source.video_id


def test_cookie_file_and_media_session_are_forwarded(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, 'DOUYIN_COOKIE_FILE', 'private-cookies.txt')
    response = MagicMock(status_code=403)
    with patch('src.providers.douyin.requests.get', return_value=response), patch('yt_dlp.YoutubeDL') as ydl:
        extractor = ydl.return_value.__enter__.return_value
        extractor.extract_info.return_value = {'id': '123', 'url': 'https://cdn.douyinvod.com/video.mp4'}
        extractor.cookiejar.add_cookie_header.side_effect = lambda req: req.add_unredirected_header('Cookie', 'session=fake-offline')
        media = DouyinProvider().get_media('https://www.douyin.com/video/123')
    assert ydl.call_args.args[0]['cookiefile'] == 'private-cookies.txt'
    assert media.headers['Cookie'] == 'session=fake-offline'


def test_download_cookie_headers_are_not_serialized_to_metadata():
    from src.providers.base import VideoMetadata
    meta = VideoMetadata(platform='douyin', download_headers={'Cookie': 'private-session'})
    assert meta.download_headers['Cookie'] == 'private-session'
    assert 'private-session' not in meta.model_dump_json()


def test_persistent_profile_seed_preserves_refreshed_browser_session(tmp_path):
    from src.providers.douyin_browser import _seed_profile
    context = MagicMock()
    marker = tmp_path/'seed'
    cookies = [{'name': 'sessionid', 'value': 'initial', 'domain': '.douyin.com', 'path': '/'}]
    _seed_profile(context, cookies, marker)
    assert context.add_cookies.call_count == 1
    # A browser-refreshed session must not be overwritten on subsequent reads.
    _seed_profile(context, list(reversed(cookies)), marker)
    assert context.add_cookies.call_count == 1
    assert 'initial' not in marker.read_text()
    cookies[0]['value'] = 'new-user-export'
    _seed_profile(context, cookies, marker)
    assert context.add_cookies.call_count == 2


@pytest.mark.parametrize('navigation_fails', [False, True])
def test_browser_reader_uses_persistent_profile_and_releases_resources(tmp_path, navigation_fails):
    import time
    from config.settings import settings
    from src.providers.douyin_browser import _profile_lock
    cookie = tmp_path/'cookies.txt'
    cookie.write_text('# Netscape HTTP Cookie File\n.douyin.com\tTRUE\t/\tTRUE\t'
                      + str(int(time.time())+3600) + '\tsessionid\toffline\n')
    context, page = MagicMock(), MagicMock()
    context.pages = [page]
    response = MagicMock(url='https://www.douyin.com/aweme/v1/web/aweme/detail/', status=200)
    response.json.return_value = {'aweme_detail': {'aweme_id': '123'}}
    if navigation_fails:
        page.goto.side_effect = RuntimeError('navigation failed')
    else:
        page.goto.side_effect = lambda *args, **kwargs: page.on.call_args.args[1](response)
    page.evaluate.return_value = 'browser-agent'
    with patch('playwright.sync_api.sync_playwright') as playwright:
        engine = playwright.return_value.__enter__.return_value
        engine.chromium.launch_persistent_context.return_value = context
        if navigation_fails:
            with pytest.raises(RuntimeError):
                actual_read_video_page('123', str(cookie), 5)
        else:
            result = actual_read_video_page('123', str(cookie), 5)
            assert result['item']['aweme_id'] == '123'
        engine.chromium.launch_persistent_context.assert_called_once_with(
            str(__import__('pathlib').Path(settings.DOUYIN_BROWSER_PROFILE).resolve()), headless=True)
        engine.chromium.launch.assert_not_called()
    context.close.assert_called_once()
    assert not _profile_lock.locked()


def test_metadata_download_handoff_is_single_use_and_session_bound(tmp_path, monkeypatch):
    from config.settings import settings
    from src.providers.base import VideoMetadata
    cookie = tmp_path/'cookies.txt'
    cookie.write_text('session-one')
    monkeypatch.setattr(settings, 'DOUYIN_COOKIE_FILE', str(cookie))
    provider = DouyinProvider()
    url = 'https://www.douyin.com/video/123'
    meta = VideoMetadata(platform='douyin', video_id='123', video_url='https://cdn.douyinvod.com/video.mp4')
    with patch.object(provider, '_fetch_metadata', return_value=meta) as fetch:
        provider.get_metadata(url)
        provider.get_media(url)
        assert fetch.call_count == 1
        provider.get_media(url)
        assert fetch.call_count == 2
        provider.get_metadata(url)
        cookie.write_text('session-two')
        provider.get_media(url)
        assert fetch.call_count == 4


def test_bilibili_http_412_keeps_structured_error_code():
    from src.providers.bilibili import BilibiliProvider
    from src.providers.base import VideoMetadata
    from src.jobs.logger import PipelineException
    provider = BilibiliProvider()
    with patch.object(provider, 'get_metadata', return_value=VideoMetadata(platform='bilibili', video_id='BV1JGJy6rEU5', extra={'cid': 1})), \
         patch('requests.get', return_value=MagicMock(status_code=412)):
        with pytest.raises(PipelineException) as error:
            provider.get_media('https://www.bilibili.com/video/BV1JGJy6rEU5/')
    assert error.value.code == 'SOURCE_UNAVAILABLE'
    assert '412' in error.value.message


@pytest.mark.parametrize('returned_id', ['7698300125159935283', 'wrong-video'])
def test_authenticated_browser_fallback_preserves_identity_and_headers(monkeypatch, returned_id):
    from config.settings import settings
    from src.jobs.logger import PipelineException
    monkeypatch.setattr(settings, 'DOUYIN_COOKIE_FILE', 'offline-cookies.txt')
    page = {'item': {'aweme_id': returned_id, 'desc': 'Real title',
                     'video': {'duration': 12000, 'play_addr': {'url_list': [
                         'https://cdn.douyinvod.com/video.mp4', 'https://mirror.zjcdn.com/video.mp4']},
                         'bit_rate': [{'play_addr': {'url_list': ['https://cdn.douyinvod.com/other-bitrate.mp4']}}]}},
            'user_agent': 'browser-agent'}
    with patch('src.providers.douyin.requests.get', return_value=MagicMock(status_code=403)), \
         patch('yt_dlp.YoutubeDL') as ydl, \
         patch('src.providers.douyin_browser.read_video_page', return_value=page) as read:
        ydl.return_value.__enter__.return_value.extract_info.side_effect = RuntimeError('API denied')
        if returned_id == 'wrong-video':
            with pytest.raises(PipelineException):
                DouyinProvider().get_media('https://www.douyin.com/jingxuan?modal_id=7698300125159935283')
        else:
            media = DouyinProvider().get_media('https://www.douyin.com/jingxuan?modal_id=7698300125159935283')
            assert media.success and media.headers['User-Agent'] == 'browser-agent'
            assert media.backup_urls == ['https://mirror.zjcdn.com/video.mp4']
            assert media.source_url.endswith('/video/7698300125159935283')
        read.assert_called_once_with('7698300125159935283', 'offline-cookies.txt', settings.REQUEST_TIMEOUT_SECONDS)
