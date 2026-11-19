"""Request-scoped task ownership for persistent, serialized model processes."""
import contextvars


current_task_var = contextvars.ContextVar('current_pipeline_task', default=None)


class ProcessLease:
    def __init__(self, process, task):
        self.process = process
        self.task = task
        if task is not None:
            with task._proc_lock:
                if not any(p is process for p in task.subprocesses):
                    task.subprocesses.append(process)

    def close(self, *, detach=True):
        task, self.task = self.task, None
        if task is not None and detach:
            with task._proc_lock:
                task.subprocesses[:] = [p for p in task.subprocesses if p is not self.process]

    def release_provider(self, owned_process):
        # Failed provider.close() may clear its handle before kill/wait fails.
        # Keep such live or unknown orphans owned by the task for final cleanup.
        detach = owned_process is self.process
        if not detach:
            try:
                detach = self.process.poll() is not None
            except Exception:
                detach = False
        self.close(detach=detach)


def lease_subprocess(process):
    """Keep active requests cancellable; idle workers belong to the provider."""
    return ProcessLease(process, current_task_var.get())
