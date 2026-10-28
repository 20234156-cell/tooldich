import contextvars
import inspect
import subprocess
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from src.database.connection import SessionLocal
from src.database.models import Job, Project, JobStatus
from src.jobs.logger import SystemErrorCodes, PipelineException, TaskCancelled

_current_task_var: contextvars.ContextVar[Optional["QueuedTask"]] = contextvars.ContextVar(
    "_current_task_var", default=None
)

# Hook subprocess.Popen to automatically track running processes for the active task
_orig_popen_init = subprocess.Popen.__init__


def _hooked_popen_init(self, *args, **kwargs):
    _orig_popen_init(self, *args, **kwargs)
    task = _current_task_var.get()
    if task is not None:
        with task._proc_lock:
            task.subprocesses.append(self)


subprocess.Popen.__init__ = _hooked_popen_init


@dataclass
class QueuedTask:
    """Một tác vụ pipeline đang chờ hoặc đang chạy trong hàng đợi."""

    task_id: str
    project_id: str
    job_id: Optional[str]
    action: str
    state: str = "QUEUED"  # QUEUED | RUNNING | DONE | FAILED | CANCELLED
    result: Optional[Dict[str, Any]] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    error_details: Dict[str, Any] = field(default_factory=dict)
    cancel_requested: bool = False
    progress: int = 0
    stage: str = "QUEUED"
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    progress_events: List[Dict[str, Any]] = field(default_factory=list)
    future: Optional[Future] = field(default=None, repr=False)
    subprocesses: List[Any] = field(default_factory=list, repr=False)
    _proc_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _event_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    owner: str = 'local'
    options: Dict[str, Any] = field(default_factory=dict)
    journaled: bool = False
    input_snapshot: Dict[str, Any] = field(default_factory=dict)
    fencing_token: int = 0
    interrupted_stage: Optional[str] = None
    interrupted_reason_code: Optional[str] = None
    retained_summary: Dict[str, Any] = field(default_factory=dict)

    def record_progress(self, percent: int, stage: str = "", *, force: bool = False) -> None:
        """Update live progress and retain a compact, timestamped milestone history."""
        bounded = max(0, min(100, int(percent)))
        label = stage or self.stage or self.action
        with self._event_lock:
            previous_progress = self.progress
            previous_stage = self.stage
            self.progress = bounded
            self.stage = label
            last = self.progress_events[-1] if self.progress_events else None
            should_record = (
                force
                or last is None
                or label != previous_stage
                or bounded in (0, 100)
                or bounded >= int(last.get("progress", 0)) + 5
                or bounded < previous_progress
            )
            if should_record:
                event = {
                    "timestamp": time.time(),
                    "progress": bounded,
                    "stage": label,
                    "state": self.state,
                }
                if last and all(last.get(k) == event.get(k) for k in ("progress", "stage", "state")):
                    return
                self.progress_events.append(event)
                # A task should never grow an unbounded in-memory event log.
                if len(self.progress_events) > 100:
                    self.progress_events[:] = self.progress_events[-100:]
        if self.journaled:
            from src.server.tasks import save
            save(self,force=force)

    def kill_subprocesses(self) -> None:
        """Kill all tracked child processes when task is cancelled or terminates."""
        with self._proc_lock:
            procs = list(self.subprocesses)
        for proc in procs:
            try:
                if hasattr(proc, "poll") and proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
        with self._proc_lock:
            self.subprocesses.clear()


class JobQueue:
    """Hàng đợi chạy pipeline nền cho REST API.

    Huỷ tác vụ: nếu chưa bắt đầu chạy thì huỷ ngay; nếu đang chạy thì đánh dấu yêu cầu
    huỷ, kill các subprocess ffmpeg đang chạy và dừng.
    """

    def __init__(self, max_workers: int = 2):
        self._heavy_slot = threading.BoundedSemaphore(1)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="pipeline"
        )
        self._tasks: Dict[str, QueuedTask] = {}
        self._by_project: Dict[str, str] = {}
        self._lock = threading.Lock()

    def submit(
        self,
        project_id: str,
        action: str,
        func: Callable[..., Dict[str, Any]],
        *args,
        inject_cancel: bool = False,
        update_pipeline_job: bool = True,
        **kwargs,
    ) -> QueuedTask:
        """Đưa một công đoạn pipeline vào hàng đợi chạy nền.

        `inject_cancel=True` sẽ truyền thêm tham số `should_continue` cho hàm chạy, để
        chuỗi nhiều công đoạn có thể dừng giữa chừng khi người dùng bấm Huỷ.
        """
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        task = QueuedTask(
            task_id=task_id,
            project_id=project_id,
            job_id=self._current_job_id(project_id) if update_pipeline_job else None,
            action=action,
        )
        task.record_progress(0, "Đang chờ đến lượt", force=True)
        from config.settings import settings
        if settings.SERVER_MODE:
            from src.server.access import actor
            from src.server.tasks import SAFE_OPTIONS,save
            task.owner=actor.get()
            task.options={k:(v.model_dump() if hasattr(v,'model_dump') else v) for k,v in kwargs.items() if k in SAFE_OPTIONS and (isinstance(v,(str,int,float,bool,dict,type(None))) or hasattr(v,'model_dump'))}
            if action=='translate':
                selected=kwargs.get('target_cues')
                task.options['target_cue_ids']=[cue if isinstance(cue,int) else cue.index for cue in selected] if selected is not None else None
            task.journaled=True

        with self._lock:
            # Hai công đoạn chạy song song trên cùng một dự án sẽ ghi đè lẫn nhau
            # (cùng tệp .part, cùng thư mục đích) nên chỉ cho phép một tác vụ mỗi lúc
            active_id = self._by_project.get(project_id)
            active = self._tasks.get(active_id) if active_id else None
            if active and active.state in ("QUEUED", "RUNNING"):
                raise PipelineException(
                    SystemErrorCodes.PROJECT_BUSY,
                    f"Dự án {project_id} đang chạy công đoạn '{active.action}'. "
                    f"Hãy đợi hoàn tất hoặc huỷ tác vụ {active.task_id} trước.",
                )

            self._tasks[task_id] = task
            self._by_project[project_id] = task_id
            if task.journaled:
                try:
                    save(task,force=True)
                except Exception:
                    self._tasks.pop(task_id,None)
                    self._by_project.pop(project_id,None)
                    raise

        task.future = self._executor.submit(
            self._run, task, func, inject_cancel, *args, **kwargs
        )
        return task

    def _current_job_id(self, project_id: str) -> Optional[str]:
        with SessionLocal() as db:
            job = (
                db.query(Job)
                .filter(Job.project_id == project_id)
                .order_by(Job.created_at.desc())
                .first()
            )
            return job.id if job else None

    def _run(self, task: QueuedTask, func: Callable, inject_cancel: bool, *args, **kwargs) -> None:
        from config.settings import settings
        from src.jobs.resources import TaskInterrupted,check_disk,storage_reason
        from src.pipeline.attempts import execution_start
        heavy=settings.SERVER_MODE and task.action.lower() in {'ocr','voiceover','render','run-all','run_all','remix'}
        acquired=False;token=None;actor_token=None;storage_failed=False;reader_lease=None
        monitor_stop=threading.Event();monitor=None
        def continue_work():
            if task.cancel_requested:return False
            if task.interrupted_reason_code:raise TaskInterrupted(task.interrupted_reason_code,'Tác vụ đã được yêu cầu dừng an toàn.')
            check_disk()
            return True
        if inject_cancel:kwargs['should_continue']=continue_work
        try:accepts_progress='progress_callback' in inspect.signature(func).parameters
        except (TypeError,ValueError):accepts_progress=False
        if accepts_progress and 'progress_callback' not in kwargs:
            def update_progress(percent,stage=''):
                check_disk();task.record_progress(percent,stage or task.action)
            kwargs['progress_callback']=update_progress
        try:
            if task.cancel_requested:raise TaskCancelled()
            if task.interrupted_reason_code:raise TaskInterrupted(task.interrupted_reason_code)
            if heavy:
                while not self._heavy_slot.acquire(timeout=.2):
                    if task.cancel_requested:raise TaskCancelled()
                    if task.interrupted_reason_code:raise TaskInterrupted(task.interrupted_reason_code)
                    task.record_progress(0,'Chờ tài nguyên máy chủ')
                acquired=True
            if task.cancel_requested:raise TaskCancelled()
            if task.interrupted_reason_code:raise TaskInterrupted(task.interrupted_reason_code)
            task.state='RUNNING';task.started_at=time.time();token=_current_task_var.set(task)
            if settings.SERVER_MODE:
                from src.server.readers import read_guard
                lease=read_guard(settings.storage_path/task.project_id);lease.__enter__();reader_lease=lease
                from src.server.access import actor
                actor_token=actor.set(task.owner);execution_start(task)
            else:self._mark_job(task.job_id,JobStatus.PROCESSING.value,error_code='',error_message='')
            check_disk(start=True)
            task.record_progress(task.progress,task.action,force=True)
            if heavy:
                def monitor_resources():
                    while not monitor_stop.wait(.5):
                        try:check_disk()
                        except TaskInterrupted as error:
                            task.interrupted_reason_code=error.reason
                            task.kill_subprocesses();return
                monitor=threading.Thread(target=monitor_resources,daemon=True);monitor.start()
            task.result=func(*args,**kwargs)
            if task.cancel_requested:raise TaskCancelled()
            if task.interrupted_reason_code:raise TaskInterrupted(task.interrupted_reason_code)
            # A stale worker may not overwrite the Job's terminal state.
            if settings.SERVER_MODE:
                from src.pipeline.attempts import assert_task_authority
                assert_task_authority()
            task.state='DONE'
            self._mark_job(task.job_id,JobStatus.COMPLETED.value,error_code='',error_message='')
        except BaseException as exc:
            reason=storage_reason(exc)
            if task.interrupted_reason_code and not isinstance(exc,TaskCancelled):exc=TaskInterrupted(task.interrupted_reason_code,str(exc))
            storage_failed=bool(reason)
            task.error_details=getattr(exc,'details',{})
            if isinstance(exc,TaskInterrupted) or reason:
                task.state='INTERRUPTED';task.error_code=getattr(exc,'reason',None) or reason
                task.error_message=str(exc);task.interrupted_stage=task.stage or task.action
                task.interrupted_reason_code=task.error_code
                task.retained_summary={'message':'Giữ đầu ra đã công nhận và cache khớp cấu hình. Kiểm tra checkpoint trước khi tiếp tục.','stage':task.interrupted_stage}
            elif isinstance(exc,TaskCancelled) or task.cancel_requested:
                task.state='CANCELLED';task.error_code='CANCELLED';task.error_message=str(exc)
            else:
                task.state='FAILED';task.error_code=getattr(exc,'code',SystemErrorCodes.UNKNOWN_ERROR);task.error_message=str(exc)
            # Persist once if possible; a failure here must not skip resource release.
            try:
                self._mark_job(task.job_id,task.state,error_code=task.error_code,error_message=task.error_message)
            except Exception as write_error:
                storage_failed=storage_failed or bool(storage_reason(write_error))
                import logging;logging.getLogger(__name__).error('Cannot persist terminal task state: %s',type(write_error).__name__)
        finally:
            monitor_stop.set()
            if monitor:monitor.join(timeout=2)
            task.finished_at=time.time();task.kill_subprocesses()
            if reader_lease:reader_lease.__exit__(None,None,None)
            # Release scarce resources before attempting any final journal write.
            if acquired:self._heavy_slot.release()
            if actor_token is not None:
                from src.server.access import actor
                actor.reset(actor_token)
            if token is not None:_current_task_var.reset(token)
            if not storage_failed:
                try:
                    labels={'DONE':'Hoàn tất','FAILED':'Thất bại','CANCELLED':'Đã hủy','INTERRUPTED':'Bị gián đoạn — kiểm tra để tiếp tục'}
                    task.record_progress(100 if task.state=='DONE' else task.progress,labels.get(task.state,task.stage),force=True)
                except Exception:
                    import logging;logging.getLogger(__name__).error('Final journal unavailable; in-memory terminal state retained')

    def _mark_job(
        self,
        job_id: Optional[str],
        status: str,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        if not job_id:
            return
        with SessionLocal() as db:
            job = db.query(Job).filter(Job.id == job_id).first()
            if job:
                active=_current_task_var.get()
                if active and active.fencing_token and job.current_fencing_token!=active.fencing_token:
                    if job.status=='CANCELLED':raise TaskCancelled()
                    from src.jobs.resources import TaskInterrupted
                    raise TaskInterrupted('STALE_ATTEMPT','Lượt chạy không còn quyền đổi trạng thái Job.')
                if status=='COMPLETED' and job.status=='CANCELLED':raise TaskCancelled()
                job.status = status
                if status=='INTERRUPTED':
                    job.interrupted_reason_code=error_code;job.interrupted_stage=active.interrupted_stage if active else job.current_stage
                    job.retained_summary={'message':'Giữ kết quả hoàn tất và cache phù hợp; không tự chạy lại.'}
                if error_code is not None:
                    job.error_code = error_code
                if error_message is not None:
                    job.error_message = error_message
                project = db.query(Project).filter(Project.id == job.project_id).first()
                if project and status in (JobStatus.FAILED.value, JobStatus.CANCELLED.value, JobStatus.INTERRUPTED.value):
                    project.status = status
                db.commit()

    def get(self, task_id: str) -> Optional[QueuedTask]:
        with self._lock:
            task=self._tasks.get(task_id)
        if task:
            return task
        from src.server.tasks import restore
        return restore(task_id)

    def get_by_project(self, project_id: str) -> Optional[QueuedTask]:
        """Lấy tác vụ gần nhất của một dự án."""
        with self._lock:
            task_id = self._by_project.get(project_id)
        if not task_id:
            from config.settings import settings
            if settings.SERVER_MODE:
                from src.server import store
                matches=[data for _,data in store.rows('task:') if data['project_id']==project_id]
                if matches:
                    task_id=max(matches,key=lambda d:d['created_at'])['task_id']
        return self.get(task_id) if task_id else None

    def get_by_job(self, job_id: str) -> Optional[QueuedTask]:
        """Prefer the active retry; otherwise return the latest job attempt."""
        with self._lock:
            matches = [task for task in reversed(self._tasks.values()) if task.job_id == job_id]
            return next((task for task in matches if task.state in ('QUEUED', 'RUNNING')),
                        matches[0] if matches else None)

    def cancel(self, task_id: str) -> QueuedTask:
        """Yêu cầu huỷ một tác vụ."""
        task = self.get(task_id)
        if not task:
            raise PipelineException(
                SystemErrorCodes.UNKNOWN_ERROR, f"Không tìm thấy tác vụ {task_id}"
            )

        if task.state in ("DONE", "FAILED", "CANCELLED", "INTERRUPTED"):
            return task

        task.cancel_requested = True
        with SessionLocal() as db:
            job=db.get(Job,task.job_id) if task.job_id else None
            if job:
                job.current_fencing_token=(job.current_fencing_token or 0)+1
                job.status='CANCELLED';db.commit()
        task.kill_subprocesses()
        if task.future is not None and task.future.cancel():
            task.state = "CANCELLED"
            task.finished_at = time.time()
            task.record_progress(task.progress, "Đã huỷ", force=True)
            self._mark_job(task.job_id, JobStatus.CANCELLED.value)
        else:
            self._mark_job(task.job_id, JobStatus.CANCELLED.value)
        if task.journaled:
            from src.server.tasks import save
            save(task,force=True)
        return task

    def pause(self,task_id):
        task=self.get(task_id)
        if task is None:raise ValueError('Không tìm thấy tác vụ.')
        if task.state not in {'QUEUED','RUNNING'}:return task
        task.interrupted_reason_code='USER_PAUSE';task.interrupted_stage=task.stage or task.action
        task.kill_subprocesses()
        if task.future and task.future.cancel():
            task.state='INTERRUPTED';task.error_code='USER_PAUSE';task.error_message='Người dùng tạm dừng.';task.finished_at=time.time()
            self._mark_job(task.job_id,'INTERRUPTED',error_code='USER_PAUSE',error_message=task.error_message)
        if task.journaled:
            from src.server.tasks import save
            save(task,force=True)
        return task

    def forget_project(self, project_id: str) -> Optional[QueuedTask]:
        """Huỷ và quên tác vụ của một dự án (dùng khi xoá dự án).

        Huỷ là hợp tác nên công đoạn đang chạy có thể còn chạy nốt; nó sẽ tự hỏng khi
        thư mục dự án biến mất, và job đã bị xoá nên không ghi được gì thêm.
        """
        task = self.get_by_project(project_id)
        if task and task.state in ("QUEUED", "RUNNING"):
            task.cancel_requested = True
            task.kill_subprocesses()
            if task.future is not None:
                task.future.cancel()
            task.state = "CANCELLED"
            self._mark_job(task.job_id, JobStatus.CANCELLED.value)

        with self._lock:
            task_id = self._by_project.pop(project_id, None)
            if task_id:
                self._tasks.pop(task_id, None)
        return task

    def shutdown(self, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait)


queue = JobQueue()
