from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from src.providers.bilibili import BilibiliProvider
from src.jobs.logger import PipelineException


@pytest.mark.parametrize('alternate_quality,multipart',[(64,False),(32,False),(64,True)])
def test_missing_requested_dash_uses_only_same_or_higher_progressive(monkeypatch,alternate_quality,multipart):
    provider=BilibiliProvider()
    monkeypatch.setattr('src.providers.bilibili.settings.BILIBILI_PREFERRED_QUALITY',64)
    monkeypatch.setattr(provider,'get_metadata',lambda u:SimpleNamespace(video_id='BVtest',extra={'cid':1},source_url=u))
    def response(data):return SimpleNamespace(status_code=200,json=lambda:{'code':0,'data':data})
    get=Mock(side_effect=[response({'quality':64,'dash':{'video':[{'id':32}]}}),
        response({'quality':alternate_quality,'durl':[{'url':'https://cdn.bilibili.com/exact.mp4'}]*(2 if multipart else 1)})])
    monkeypatch.setattr('src.providers.bilibili.requests.get',get)
    if alternate_quality==64 and not multipart:
        result=provider.get_media('https://www.bilibili.com/video/BVtest/')
        assert result.success and result.extra['quality']==64 and not result.extra['dash']
    else:
        with pytest.raises(PipelineException):provider.get_media('https://www.bilibili.com/video/BVtest/')
    assert get.call_args_list[1].kwargs['params']['fnval']==0
    assert get.call_args_list[1].kwargs['params']['qn']==64
