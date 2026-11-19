document.getElementById('btn-source-audio-review')?.addEventListener('click', async event => {
  if (!currentProjectId) return;
  const projectId = currentProjectId;
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const task = await API.request('POST', `/api/projects/${projectId}/source-audio-review`, {});
    const completed = await waitForTask(task);
    if (projectId === currentProjectId) {
      await loadCues(true);
      await refreshProjectData();
      showOcrReview('pending');
      const result = completed.result || {};
      window.workspaceToast?.(
        `Đã tự đối chiếu ${result.resolved || 0} câu. ` +
        (result.status === 'unavailable'
          ? 'Bộ nhận dạng lời nói chưa sẵn sàng; các câu còn lại vẫn được giữ để kiểm tra.'
          : 'Các câu chưa đủ bằng chứng nằm trong Cần kiểm tra.'));
    }
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
  }
});

document.getElementById('btn-skip-uncertain')?.addEventListener('click', async event => {
  if (!currentProjectId) return;
  const projectId = currentProjectId;
  const button = event.currentTarget;
  if (!confirm('Bỏ qua các câu chưa chắc đủ điều kiện trong dự án này? Video có thể thiếu lời. Bạn có thể xem và khôi phục trong Lịch sử / hoàn tác.')) return;
  button.disabled = true;
  try {
    const result = await API.request('POST', `/api/projects/${projectId}/cues/skip-uncertain`, {});
    if (projectId === currentProjectId) {
      await loadCues(true);
      await refreshProjectData();
      showOcrReview('pending');
      window.workspaceToast?.(`Đã bỏ qua ${result.skipped} câu. Xem Lịch sử / hoàn tác để khôi phục.`);
      if (result.auto_resume_error) window.workspaceToast?.(result.auto_resume_error, true);
      if (result.resumed_task) await monitorAutoContinuation(result.resumed_task, projectId);
    }
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
  }
});
