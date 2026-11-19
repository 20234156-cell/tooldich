/* Offline workspace: keep the editing workflow, remove online distractions. */
(() => {
  if(window.workspacePersonal) {
    document.querySelector('.brand-copy small').textContent='STUDIO CÁ NHÂN';
    document.querySelector('#view-dashboard h1').textContent='Video của tôi';
  }
  if (!window.workspaceOffline) return;
  const $ = id => document.getElementById(id);
  document.querySelector('.brand-copy small').textContent = 'STUDIO OFFLINE';
  document.querySelector('#view-dashboard h1').textContent = 'Video của tôi';
  document.querySelector('#view-dashboard h1 + p').textContent = 'Nhập video trên máy → duyệt phụ đề → dịch → xuất thành phẩm.';
  document.querySelector('#view-new h1').textContent = 'Thêm video trên máy';
  document.querySelector('#view-new h1 + p').textContent = 'Dán đường dẫn đầy đủ đến video có sẵn, ví dụ D:\\Video\\phim.mp4.';
  $('input-url').placeholder = 'D:\\Video\\phim.mp4';
  $('input-batch-urls').placeholder = 'D:\\Video\\phim-1.mp4\nD:\\Video\\phim-2.mp4';
  document.querySelector('label[for="input-url"]').textContent = 'Đường dẫn video trên máy';
  document.querySelector('label[for="input-batch-urls"]').textContent = 'Mỗi dòng một đường dẫn video';
  $('btn-mode-single').textContent = 'Một video';
  $('btn-mode-batch').textContent = 'Nhiều video';
  document.querySelector('#form-new button[type="submit"]').textContent = 'Tạo dự án';
  const select = $('translation-provider');
  select.value = 'local_hymt';
  select.querySelector('[value="gemini"]').disabled = true;
  $('trans-key-status').textContent = 'HY-MT trên máy · đang kiểm tra model';
  document.querySelector('#card-step-5 .step-card-title span:last-child').textContent = 'Dịch tiếng Việt trên máy';
  document.querySelector('#card-step-5 > p').textContent = 'HY-MT dịch từng câu và lưu tiến độ. Kiểm tra lại nghĩa, tên riêng và độ dài trước khi duyệt.';
  document.querySelector('#card-step-7 > p').textContent = 'VieNeu tạo giọng từ model có sẵn và căn theo thời gian từng câu. Sửa ngắn câu quá dài rồi tạo lại.';
})();
