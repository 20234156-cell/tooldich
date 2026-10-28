import pytest
from src.pipeline import render_checkpoint as checkpoint
from src.pipeline.render import RenderConfig
from src.pipeline.roi import ROIBox
from src.ocr.base import Cue


@pytest.mark.parametrize('changed', ['source','voice','settings','cue','encoded','unfinished'])
def test_render_resume_rejects_changed_or_unfinished_work(tmp_path,changed):
    source=tmp_path/'source';source.write_bytes(b'source')
    voice=tmp_path/'voice';voice.write_bytes(b'voice')
    encoded=tmp_path/'encoded';encoded.write_bytes(b'completed')
    path=tmp_path/'checkpoint.json'
    roi=ROIBox(x=0,y=.8,width=1,height=.1)
    cues=[Cue(index=1,start=0,end=1,vi_text='one')]
    cfg=RenderConfig()
    signature=checkpoint.identity(source,voice,roi,cues,cfg)
    if changed!='unfinished':checkpoint.write(path,encoded,signature,{'completed':True})
    if changed=='source':source.write_bytes(b'SOURCE')
    if changed=='voice':voice.write_bytes(b'VOICE')
    if changed=='settings':cfg.font_size+=1
    if changed=='cue':cues[0].vi_text='two'
    if changed=='encoded':encoded.write_bytes(b'corrupted')
    assert checkpoint.read(path,encoded,checkpoint.identity(source,voice,roi,cues,cfg)) is None


def test_retry_clears_previous_job_error_before_running(db_session):
    from src.database.models import Project,Job
    from src.database.connection import SessionLocal
    from src.jobs.queue import JobQueue,QueuedTask
    db_session.add(Project(id='retry_error'))
    db_session.add(Job(id='retry_job',project_id='retry_error',status='FAILED',error_code='old',error_message='old QC error'))
    db_session.commit()
    def run():
        with SessionLocal() as db:
            job=db.get(Job,'retry_job')
            assert job.status=='PROCESSING'
            assert job.error_code=='' and job.error_message==''
        return {}
    queue=JobQueue()
    task=QueuedTask(task_id='retry_task',project_id='retry_error',job_id='retry_job',action='render')
    queue._run(task,run,False)
    assert task.state=='DONE',task.error_message
