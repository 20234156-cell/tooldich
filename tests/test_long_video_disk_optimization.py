"""Offline disk safety and real short-media audio-first regression evidence."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
import pytest
from config.settings import settings
from src.jobs.resources import check_sufficient_disk_space, TaskInterrupted, media_timeout
from src.pipeline import render, audio_qc
from src.pipeline.media_storage import cleanup_intermediate_artifacts, release_media
from src.pipeline.roi import ROIBox


def test_limits_and_deadline():
    assert type(settings).model_fields['MAX_VIDEO_DURATION_SECONDS'].default == 21600
    assert type(settings).model_fields['MAX_DOWNLOAD_SIZE_BYTES'].default == 24*1024**3
    assert media_timeout(1800, 15000) == 37800


def test_preflight_blocks_without_writing(tmp_path, monkeypatch):
    before=set(tmp_path.iterdir())
    monkeypatch.setattr('shutil.disk_usage', lambda _: SimpleNamespace(free=1024))
    with pytest.raises(TaskInterrupted, match='GiB'):
        check_sufficient_disk_space(tmp_path, 2048, 'Render')
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize('field', ['downloaded_bytes','total_bytes'])
def test_ytdlp_hook_rejects_declared_and_received_size(tmp_path, monkeypatch, field):
    import yt_dlp
    from src.download.manager import DownloadManager
    from src.jobs.logger import PipelineException
    class FakeYDL:
        def __init__(self, options): self.hook = options['progress_hooks'][0]
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def download(self, urls):
            self.hook(dict(status='downloading', **{field:settings.MAX_DOWNLOAD_SIZE_BYTES+1}))
    monkeypatch.setattr(yt_dlp, 'YoutubeDL', FakeYDL)
    monkeypatch.setattr('src.download.manager.check_sufficient_disk_space', lambda *a: None)
    with pytest.raises(PipelineException) as error:
        DownloadManager()._download_ytdlp('https://example.test/video', {}, tmp_path/'v.mp4', tmp_path/'v.part', 0)
    assert error.value.code == 'FILE_TOO_LARGE'


def test_cleanup_preserves_metadata_and_source(tmp_path):
    for name in ['source/original.mp4', 'output/video_vi.mp4', 'output/verify_report.json',
                 'output/publishing_pack.json', 'voiceover/timeline.json', 'voiceover/clips/clip_0001.wav',
                 'transcript/audio_recheck/window_1.wav', 'transcript/vi.srt']:
        path=tmp_path/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'fixture')
    cleanup_intermediate_artifacts(tmp_path)
    assert (tmp_path/'source/original.mp4').exists()
    assert not (tmp_path/'voiceover/clips/clip_0001.wav').exists()
    release_media(tmp_path)
    assert not (tmp_path/'output/video_vi.mp4').exists()
    for name in ['output/verify_report.json','output/publishing_pack.json','voiceover/timeline.json','transcript/vi.srt']:
        assert (tmp_path/name).read_bytes() == b'fixture'


@pytest.mark.parametrize('suffix', ['.m4a','.mp4'])
def test_real_master_delivery_stream_layout(tmp_path, suffix, monkeypatch):
    ffmpeg=render.get_ffmpeg_bin(); path=tmp_path/('input'+suffix)
    cmd=[str(ffmpeg), '-v','error','-y','-f','lavfi','-i','sine=frequency=440:duration=2']
    if suffix=='.mp4': cmd += ['-f','lavfi','-i','color=s=128x96:d=2','-c:v','libx264']
    subprocess.run(cmd+['-c:a','aac',str(path)],check=True,timeout=30)
    # Explicitly exercise actual stream probing, beyond the legacy fake-media fixture.
    def streams(p):
        data=subprocess.check_output([str(render.get_ffprobe_bin()),'-v','error','-show_streams','-of','json',str(p)],timeout=30)
        return any(s['codec_type']=='video' for s in json.loads(data)['streams'])
    result=audio_qc.master_delivery(ffmpeg,path,duration_seconds=2)
    assert result['passed'] and audio_qc.delivery_passed(audio_qc.measure_audio(ffmpeg,path))
    assert audio_qc.has_video_stream(path) == (suffix=='.mp4')


def test_real_audio_first_compact_render(tmp_path, monkeypatch):
    ffmpeg=render.get_ffmpeg_bin(); source=tmp_path/'source.mp4'; output=tmp_path/'output/video_vi.mp4'
    subprocess.run([str(ffmpeg),'-v','error','-y','-f','lavfi','-i','color=s=128x96:d=2',
        '-f','lavfi','-i','sine=frequency=440:duration=2','-c:v','libx264','-c:a','aac',str(source)],check=True,timeout=30)
    monkeypatch.setattr(render,'nvenc_available',lambda _:False)
    commands=[]; original=render.subprocess.Popen
    def track(cmd,*a,**k):
        commands.append(cmd); return original(cmd,*a,**k)
    monkeypatch.setattr(render.subprocess,'Popen',track)
    result=render.render_one_pass(source,None,ROIBox(x=0,y=.8,width=1,height=.2),[],output,
        render.RenderConfig(audio_mode='original_only',style='original',brand_position='off',encoder_preset='compact',use_nvenc=False))
    assert result.output_path.is_file()
    picture=next(c for c in commands if '-c:v' in c and c[-1].endswith('.tmp.mp4'))
    assert picture[picture.index('-c:a')+1]=='copy'
    assert picture[picture.index('-crf')+1]=='23'
    assert picture[picture.index('-maxrate')+1]=='4500k'
    assert any('mastered_audio.m4a' in str(arg) for arg in picture)
    assert not list(output.parent.glob('*.audio-qc.mp4'))
    assert not (output.parent/'mastered_audio.m4a').exists()
    report=json.loads((output.parent/'audio_qc.json').read_text())
    assert audio_qc.cached_delivery_measurement(output,report) is not None


def test_release_api_preserves_sqlite_cues(tmp_path, db_session):
    from src.api.app import app
    from src.database.models import Project, Transcript
    from fastapi.testclient import TestClient
    db_session.add(Project(id='disk_release'))
    db_session.add(Transcript(project_id='disk_release',language='zh',segments=[{'index':1,'text':'原文','vi_text':'Bản dịch'}]))
    db_session.commit()
    root=settings.storage_path/'disk_release'; (root/'source').mkdir(parents=True)
    (root/'source/original.mp4').write_bytes(b'media')
    (root/'vi.srt').write_bytes(b'sub')
    with TestClient(app) as client:
        assert client.post('/api/projects/disk_release/release-media',json={'confirm':False}).status_code==400
        response=client.post('/api/projects/disk_release/release-media',json={'confirm':True})
        assert response.status_code==200,response.text
    db_session.expire_all()
    assert db_session.query(Transcript).filter_by(project_id='disk_release').one().segments[0]['vi_text']=='Bản dịch'
    assert (root/'vi.srt').read_bytes()==b'sub'
    assert not (root/'source/original.mp4').exists()


def test_preview_temp_stays_in_application_storage(tmp_path, monkeypatch):
    import tempfile
    ffmpeg=render.get_ffmpeg_bin(); source=tmp_path/'source.mp4'
    subprocess.run([str(ffmpeg),'-v','error','-y','-f','lavfi','-i','color=s=128x96:d=1',
                    '-c:v','libx264',str(source)],check=True,timeout=30)
    created=[]; factory=tempfile.TemporaryDirectory
    def track(*args,**kwargs):
        created.append(Path(kwargs['dir'])); return factory(*args,**kwargs)
    monkeypatch.setattr(render.tempfile,'TemporaryDirectory',track)
    result=render.render_preview_frame(source,ROIBox(x=0,y=.8,width=1,height=.2),[],
        0,render.RenderConfig(style='original',brand_position='off'))
    assert result and created == [settings.storage_path.parent/'tmp']


def test_dynamic_trim_timeout_uses_requested_duration(tmp_path, monkeypatch):
    from src.media import trimmer
    timeouts=[]
    monkeypatch.setattr(trimmer,'get_ffmpeg_bin',lambda:Path('ffmpeg'))
    def run(cmd,**kwargs):
        timeouts.append(kwargs['timeout'])
        Path(cmd[-1]).write_bytes(b'fixture')
        return SimpleNamespace(returncode=0,stdout='',stderr='')
    monkeypatch.setattr(trimmer.subprocess,'run',run)
    (tmp_path/'source.mp4').write_bytes(b'source')
    trimmer.clip_video_segment(tmp_path/'source.mp4',0,15000,tmp_path/'trim.mp4')
    assert timeouts and all(t>36000 for t in timeouts)


def test_dynamic_mux_timeout(tmp_path, monkeypatch):
    from src.download import manager
    seen=[]
    monkeypatch.setattr(manager,'get_ffmpeg_bin',lambda:Path('ffmpeg'))
    def run(cmd,**kwargs):
        seen.append(kwargs['timeout']);Path(cmd[-1]).write_bytes(b'fixture')
        return SimpleNamespace(returncode=0,stdout='',stderr='')
    monkeypatch.setattr(manager.subprocess,'run',run)
    manager.mux_video_audio_copy(tmp_path/'v',tmp_path/'a',tmp_path/'out.mp4',duration_seconds=15000)
    assert seen==[37800]


def test_full_decode_timeout_scales(tmp_path, monkeypatch):
    from src.download import validation
    source=tmp_path/'source.mp4';source.write_bytes(b'fixture')
    probe=dict(streams=[dict(codec_type='video',codec_name='h264',width=320,height=180,avg_frame_rate='25/1'),
                        dict(codec_type='audio',codec_name='aac')],format=dict(duration='15000',size='7'))
    seen=[]
    def run(cmd,**kwargs):
        if '-show_streams' in cmd:
            return SimpleNamespace(returncode=0,stdout=json.dumps(probe),stderr='')
        seen.append(kwargs['timeout'])
        return SimpleNamespace(returncode=0,stdout='',stderr='')
    monkeypatch.setattr(validation.subprocess,'run',run)
    monkeypatch.setattr(settings,'MAX_VIDEO_DURATION_SECONDS',21600)
    result=validation.validate_download(source,mode='strict')
    assert result.valid
    assert 37800 in seen


def test_compact_workflow_and_nvenc_settings():
    from src.pipeline.workflow_options import resolve_workflow
    from src.api.schemas import RunAllRequest
    assert resolve_workflow('screen_dub','compact').encoder_preset=='compact'
    assert render.RenderConfig(encoder_preset='compact').encoder_preset=='compact'
    assert RunAllRequest(processing_profile='compact').processing_profile=='compact'
