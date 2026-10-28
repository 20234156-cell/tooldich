import json
from unittest.mock import MagicMock, patch

import pytest

from src.download.manager import DownloadManager
from src.jobs.logger import PipelineException, SystemErrorCodes
from src.ocr.vsf_adapter import VideoSubFinderAdapter
from src.pipeline.roi import ROIBox
from tests.test_small_server import server
from tests.test_resumable_uploads import real_video


@pytest.mark.parametrize('mode', ['range', 'sequential'])
def test_download_policy_error_cannot_enter_unguarded_fallback(tmp_path, mode):
    manager = DownloadManager()
    blocked = PipelineException(SystemErrorCodes.BLOCKED_URL, 'Blocked redirect')
    with patch.object(manager, '_probe_url', return_value=(mode == 'range', 4*1024**2, 'https://test.bilivideo.com/a')), \
         patch('src.download.manager.AdaptiveRangeDownloader.download_range', side_effect=blocked), \
         patch.object(manager, '_download_sequential', side_effect=blocked), \
         patch.object(manager, '_download_ytdlp') as fallback:
        with pytest.raises(PipelineException) as exc:
            manager.download('https://test.bilivideo.com/a', tmp_path/'video.mp4')
    assert exc.value.code == SystemErrorCodes.BLOCKED_URL
    fallback.assert_not_called()


def test_sequential_replace_failure_preserves_previous_delivery(tmp_path):
    target, part, marker = tmp_path/'video.mp4', tmp_path/'video.part', tmp_path/'video.src'
    target.write_bytes(b'previous verified delivery')
    response = MagicMock(status_code=200, headers={'Content-Length': '4'})
    response.iter_content.return_value = [b'next']
    import os
    original_replace = os.replace
    def replace(source, destination):
        if destination == target:
            raise PermissionError('Destination locked')
        return original_replace(source, destination)
    with patch.object(DownloadManager, '_get', return_value=response), patch('src.download.manager.os.replace', side_effect=replace):
        with pytest.raises(PermissionError):
            DownloadManager()._download_sequential('https://test.bilivideo.com/a', {}, target, part, marker, verify_media=False)
    assert target.read_bytes() == b'previous verified delivery'


@pytest.mark.parametrize('record', [None, [], 'invalid', 42])
def test_invalid_scan_manifest_restarts_scan(tmp_path, record):
    source = tmp_path/'source.mp4'; source.write_bytes(b'source')
    (tmp_path/'vsf_scan.json').write_text(json.dumps(record))
    adapter = VideoSubFinderAdapter()
    with patch.object(adapter, 'get_executable', return_value=None), \
         patch.object(adapter.fallback_detector, 'detect_cues', return_value=[]) as detect:
        assert adapter.extract_cues(source, ROIBox(x=.1,y=.8,width=.8,height=.1), tmp_path) == []
    detect.assert_called_once()


def test_failed_vsf_cannot_publish_partial_images(tmp_path):
    import io
    adapter = VideoSubFinderAdapter()
    proc = MagicMock(returncode=1,stdout=io.StringIO(''),stderr=io.StringIO('Decoder failed halfway'))
    proc.poll.return_value = 1
    proc.communicate.return_value = ('', 'Decoder failed halfway')
    def launch(*args, **kwargs):
        images = tmp_path/'RGBImages'; images.mkdir()
        (images/'0_00_00_000__0_00_01_000.png').write_bytes(b'partial scan image')
        return proc
    with patch('src.ocr.vsf_adapter.subprocess.Popen', side_effect=launch), \
         patch('src.ocr.vsf_adapter.cv2.VideoCapture') as capture, \
         patch.object(adapter, 'recognize_vsf_images', return_value=[]) as recognize:
        capture.return_value.isOpened.return_value = False
        with pytest.raises(RuntimeError, match='VSF error'):
            adapter._run_vsf(tmp_path/'vsf.exe', tmp_path/'source.mp4',
                             ROIBox(x=.1,y=.8,width=.8,height=.1), tmp_path, 0, None)
    recognize.assert_not_called()
    assert not (tmp_path/'vsf_scan.json').exists()


@pytest.mark.parametrize('existing', [True, False])
def test_roi_db_failure_does_not_publish_new_region_or_invalidate_files(tmp_path, existing):
    from src.pipeline.orchestrator import PipelineOrchestrator
    from src.pipeline.roi import ROISet
    runner=PipelineOrchestrator();path=tmp_path/'source/roi.json'
    if existing:ROISet(rois=[ROIBox(x=.1,y=.8,width=.8,height=.1)]).save_to_file(path)
    previous=path.read_bytes() if existing else None
    db=MagicMock();db.commit.side_effect=RuntimeError('DB write failed')
    with patch.object(runner,'get_project_dir',return_value=tmp_path), \
         patch('src.pipeline.orchestrator.SessionLocal') as session, \
         patch.object(runner,'_remove_stale_artifacts') as invalidate, \
         patch.object(runner,'save_state') as save:
        session.return_value.__enter__.return_value=db
        with pytest.raises(RuntimeError,match='DB write failed'):
            runner.save_roi_set('p',[ROIBox(x=.2,y=.7,width=.6,height=.1).model_dump()])
    assert (path.read_bytes() if path.exists() else None)==previous
    invalidate.assert_not_called();save.assert_not_called()


def test_run_all_returns_committed_output_without_reencoding(tmp_path):
    from src.pipeline.orchestrator import PipelineOrchestrator
    from src.pipeline.roi import ROISet
    from src.pipeline.state_machine import PipelineState,ProjectState
    from src.ocr.base import Cue
    runner=PipelineOrchestrator()
    output=tmp_path/'attempts/current/output/video_vi.mp4';output.parent.mkdir(parents=True);output.write_bytes(b'committed')
    state=PipelineState(current_state=ProjectState.VERIFIED)
    report=MagicMock(passed=True);report.to_dict.return_value={'passed':True}
    with patch.object(runner,'get_project_dir',return_value=tmp_path), \
         patch.object(runner,'get_source_video_path',return_value=tmp_path/'source.mp4'), \
         patch.object(runner,'get_roi_set',return_value=ROISet(rois=[ROIBox(x=.1,y=.8,width=.8,height=.1)])), \
         patch.object(runner,'get_state',return_value=state), \
         patch.object(runner,'get_cues',return_value=[Cue(index=1,start=0,end=1,text='你好',vi_text='Xin chào')]), \
         patch.object(runner,'get_subtitle_settings',return_value={'audio_mode':'original_only'}), \
         patch.object(runner,'save_subtitle_settings'), \
         patch.object(runner,'run_render') as render, \
         patch.object(runner,'verify',return_value=report), \
         patch('src.pipeline.orchestrator.artifact_path',side_effect=lambda root,name: output if name=='output/video_vi.mp4' else root/name), \
         patch('src.cloud.archive.auto_archive',return_value={'status':'DISABLED'}):
        result=runner.run_all('p')
    render.assert_not_called()
    assert result['output_path']==str(output)


@pytest.mark.parametrize('record',[None,[],42])
def test_render_seed_ignores_malformed_prior_manifest(db_session,monkeypatch,record):
    from config.settings import settings
    from src.database.models import Project
    from src.pipeline.attempts import build_area,seed_render_checkpoint
    monkeypatch.setattr(settings,'SERVER_MODE',True)
    db_session.add(Project(id='seed'));db_session.commit()
    root=settings.storage_path/'seed';root.mkdir()
    with build_area(root,'output',{}) as previous:
        (previous.directory/'output').mkdir()
        (previous.directory/'output/video_vi.encode.json').write_text(json.dumps(record))
    with build_area(root,'output',{}) as current:
        seed_render_checkpoint(current)
        assert not (current.directory/'output/video_vi.tmp.mp4').exists()


def test_proxy_resource_interruption_reaps_encoder(tmp_path):
    import io
    from src.jobs.resources import TaskInterrupted
    process=MagicMock(stdout=io.StringIO(''),stderr=io.StringIO(''))
    process.poll.return_value=None
    capture=MagicMock()
    capture.get.side_effect=lambda prop: {3:1920,5:25,7:250}.get(prop,0)
    with patch('src.ocr.vsf_adapter.cv2.VideoCapture',return_value=capture), \
         patch('src.pipeline.render.get_ffmpeg_bin',return_value='ffmpeg'), \
         patch('src.ocr.vsf_adapter.subprocess.Popen',return_value=process), \
         patch('src.ocr.vsf_adapter.check_disk',side_effect=TaskInterrupted('LOW_DISK_SPACE')):
        with pytest.raises(TaskInterrupted):
            VideoSubFinderAdapter()._create_scan_proxy(tmp_path/'source.mp4',tmp_path,0,None)
    process.terminate.assert_called_once()
    process.wait.assert_called()


def test_selected_translation_api_preserves_integer_scope_and_current_db(server,db_session,monkeypatch):
    from src.database.models import Project,Transcript
    from src.jobs.queue import JobQueue
    import src.jobs.queue as queue_module
    from src.pipeline.state_machine import PipelineState,ProjectState
    from src.ocr.base import Cue
    api,admin=server
    db_session.add(Project(id='scope'))
    db_session.add(Transcript(project_id='scope',segments=[{'index':7,'start':0,'end':2,'text':'你好','vi_text':''},
                                                        {'index':42,'start':3,'end':5,'text':'再见','vi_text':''}]))
    db_session.commit()
    api.orchestrator.save_state('scope',PipelineState(current_state=ProjectState.SOURCE_REVIEWED))
    queue=JobQueue(1)
    monkeypatch.setattr(api,'queue',queue);monkeypatch.setattr(queue_module,'queue',queue)
    seen=[]
    def translate(self,cues,**kwargs):
        seen.extend(c.index for c in cues)
        return [c.model_copy(update={'vi_text':'Xin chào'}) for c in cues]
    monkeypatch.setattr('src.translation.batch_translator.BatchTranslator.translate_cues',translate)
    lease=admin.post('/api/server/lease/scope').json();admin.headers['If-Match']=str(lease['revision'])
    try:
        result=admin.post('/api/projects/scope/translate',json={'cues':[7]})
        assert result.status_code==200,result.text
        task=queue.get(result.json()['task_id']);task.future.result(10)
        assert task.state=='DONE',task.error_message
        assert task.options['target_cue_ids']==[7] and seen==[7]
        assert api.orchestrator.get_cues('scope')[1].vi_text==''
    finally:queue.shutdown()


@pytest.mark.parametrize('case',['complete','missing','stale','native_error','unknown_binary'])
def test_legacy_vsf_exit_requires_current_completion_and_clean_native_log(tmp_path,case):
    import io
    adapter=VideoSubFinderAdapter()
    completion=tmp_path/'vsf_completed.srt'
    if case=='stale':completion.write_text('1\n00:00:00,000 --> 00:00:01,000\nempty\n')
    proc=MagicMock(returncode=4294967295,stdout=io.StringIO(''),stderr=io.StringIO(''))
    proc.poll.return_value=4294967295
    def launch(command,**kwargs):
        assert '-ces' in command
        if case not in {'missing','stale'}:completion.write_text('1\n00:00:00,000 --> 00:00:01,000\nempty\n')
        if case=='native_error':(tmp_path/'error.log').write_text('Got C++ Exception during search')
        return proc
    with patch('src.ocr.vsf_adapter._legacy_wxw',return_value=case!='unknown_binary'), \
         patch('src.ocr.vsf_adapter.subprocess.Popen',side_effect=launch), \
         patch('src.ocr.vsf_adapter.cv2.VideoCapture') as capture, \
         patch.object(adapter,'recognize_vsf_images',return_value=[]) as recognize:
        capture.return_value.isOpened.return_value=False
        if case=='complete':
            assert adapter._run_vsf(tmp_path/'vsf.exe',tmp_path/'video.mp4',ROIBox(x=.1,y=.8,width=.8,height=.1),tmp_path,0,None)==[]
            recognize.assert_called_once()
        else:
            with pytest.raises(RuntimeError,match='VSF error'):
                adapter._run_vsf(tmp_path/'vsf.exe',tmp_path/'video.mp4',ROIBox(x=.1,y=.8,width=.8,height=.1),tmp_path,0,None)
            recognize.assert_not_called()


def test_failed_dash_validation_preserves_previous_delivery(tmp_path):
    from src.providers.base import MediaSource
    target=tmp_path/'video.mp4';target.write_bytes(b'previous verified delivery')
    manager=DownloadManager()
    media=MediaSource(success=True,platform='bilibili',video_url='https://test.bilivideo.com/v',audio_url='https://test.bilivideo.com/a')
    def mux(video,audio,path):path.write_bytes(b'invalid new mux');return path
    with patch('src.download.manager.select_best_mirrors',side_effect=lambda urls,headers:urls), \
         patch.object(manager,'_probe_url',side_effect=lambda url,headers:(True,8,url)), \
         patch('src.download.manager.AdaptiveRangeDownloader.download_range',return_value=True), \
         patch('src.download.manager.mux_video_audio_copy',side_effect=mux), \
         patch('src.download.manager.validate_download',side_effect=PipelineException(SystemErrorCodes.MEDIA_CORRUPTED,'Bad mux')):
        with pytest.raises(PipelineException):manager.download(media,target)
    assert target.read_bytes()==b'previous verified delivery'


def test_scan_identity_rejects_changed_bytes_with_preserved_size_and_mtime(tmp_path):
    import os
    source=tmp_path/'video.mp4';source.write_bytes(b'first')
    roi=ROIBox(x=.1,y=.8,width=.8,height=.1)
    first=VideoSubFinderAdapter.scan_cache_key(source,roi)
    stat=source.stat();source.write_bytes(b'other');os.utime(source,ns=(stat.st_atime_ns,stat.st_mtime_ns))
    assert VideoSubFinderAdapter.scan_cache_key(source,roi)!=first


def test_authenticated_upload_import_remains_authorized_until_queue_completion(server,real_video,monkeypatch):
    from src.jobs.queue import JobQueue
    import src.jobs.queue as queue_module
    from src.database.models import Job
    from src.server import store
    api,admin=server;queue=JobQueue(1)
    monkeypatch.setattr(api,'queue',queue);monkeypatch.setattr(queue_module,'queue',queue)
    try:
        uploaded=admin.post('/api/server/uploads?filename=sample.mp4',content=real_video,headers={'Content-Type':'application/octet-stream'})
        assert uploaded.status_code==200,uploaded.text
        project=uploaded.json()['project_id']
        lease=admin.post('/api/server/lease/'+project).json();admin.headers['If-Match']=str(lease['revision'])
        response=admin.post('/api/projects/'+project+'/download',json={})
        assert response.status_code==200,response.text
        task=queue.get(response.json()['task_id']);task.future.result(20)
        assert task.state=='DONE',task.error_message
        with store.transaction() as db:assert db.get(Job,task.job_id).status=='COMPLETED'
        assert api.orchestrator.get_state(project).current_state.value=='MEDIA_READY'
    finally:queue.shutdown()


def test_scan_identity_includes_native_binary_and_settings(tmp_path,monkeypatch):
    from config.settings import settings
    source=tmp_path/'source.mp4';source.write_bytes(b'video')
    binary=tmp_path/'vsf.exe';binary.write_bytes(b'engine1')
    config=tmp_path/'settings/general.cfg';config.parent.mkdir();config.write_text('setting1')
    monkeypatch.setattr(settings,'VSF_PATH',str(binary))
    roi=ROIBox(x=.1,y=.8,width=.8,height=.1)
    first=VideoSubFinderAdapter.scan_cache_key(source,roi)
    config.write_text('setting2')
    assert VideoSubFinderAdapter.scan_cache_key(source,roi)!=first
    first=VideoSubFinderAdapter.scan_cache_key(source,roi)
    binary.write_bytes(b'engine2')
    assert VideoSubFinderAdapter.scan_cache_key(source,roi)!=first
