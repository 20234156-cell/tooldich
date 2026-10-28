# Kiểm định tool DuanLayLink ngày 26/10/2026

## Quyết định

Không coi tool đã hết bug chỉ vì server chạy hoặc video mang nhãn VERIFIED. Sửa trước các lỗi có thể cho kết quả QA sai hoặc vượt quyền; giữ nguyên dữ liệu người dùng và ngưỡng chất lượng. Bộ kiểm thử offline, kiểm định media thật và kiểm tra provider là ba loại bằng chứng khác nhau.

## Lỗi đã tái hiện và sửa

| Ưu tiên | Lỗi | Quyết định và kiểm chứng |
|---|---|---|
| Cao | Render lại giữ VERIFIED=COMPLETED của bản trước, kể cả encode thất bại | Hủy hiệu lực render/QA ngay trước encode. Hai kiểm thử quan sát trạng thái bên trong encoder, thành công và lỗi. |
| Cao | QA chạy lại lỗi hoặc báo không đạt nhưng dự án còn VERIFIED | Ghi RUNNING và bỏ VERIFIED cũ trước đọc/probe; lỗi ghi FAILED, giữ ngoại lệ; báo không đạt giữ RENDERED. Hai kiểm thử lỗi và báo không đạt. |
| Cao | GET QA cho viewer ghi trạng thái và có thể kích hoạt tải cloud | Chuyển thực thi sang POST; GET405 và Allow:POST. Frontend đổi đồng bộ. Kiểm thử viewer/CSRF/maintenance/slot/admin; QA dùng slot nặng chung. |
| Vừa | Dashboard tính transcript của dự án không tồn tại, báo số thiếu âm và cảnh báo cue ngoài workspace | Join Project trước lấy transcript đầu tiên; giữ transcript cố ý rỗng làm thẩm quyền. Kiểm thử hai orphan và dự án hiện có chưa có transcript. |

Chi tiết nguyên nhân và đường dẫn kiểm thử trong storage/maintenance/BUG_REGRESSIONS.md. Trước sửa có 5 kiểm thử lifecycle/dashboard thất bại; kiểm thử quyền riêng xác nhận viewer GET nhận200 và gọi verifier. Nhóm sửa đầu43 đạt. Nhóm mở rộng90 đạt, một lỗi phản hồi GET404 thay vì405 đã được sửa và kiểm thử lại phần quyền đạt.

## Video và dữ liệu thật, chỉ đọc

- Dự án proj_8a7c48989c2b:119 cue;119 bản dịch;119 tham chiếu tệp giọng tồn tại sau giải đường dẫn tương đối theo project;119 ID timeline khớp DB.
- Nguồn và MP4 xuất:568.512 giây, H.264/AAC,1920x822,48kHz stereo. SHA256 MP4:4c4c0e3d25d3817d0c37b63be505f5f8d0893d5762ee6732cb40b7fee9b60ed2.
- Giải mã toàn bộ MP4 bằng FFmpeg -xerror:exit0, stderr trống,48.52 giây. Ba mẫu đầu/giữa/cuối cũng giải mã hình và tiếng đạt.
- Chạy lại hàm QA chỉ đọc bằng cue DB:passed=true,fully_passed=false,coverage100%,duration_diff0,max_drift0.847s,mean_drift0.125s,errors=[]; không sửa pipeline_state/verify_report của dự án.
- fully_passed=false do bản dịch đã được tự rút gọn. Cảnh báo lưu gồm lượt rút gọn56 câu và một lượt1 câu; không suy ra đó là57 ID khác nhau.119 cue chưa đánh dấu duyệt. Quyết định:giữ là đạt kỹ thuật có cảnh báo, yêu cầu duyệt nghĩa/nghe lại trước phát hành; không tự đánh dấu duyệt.
- SQLite integrity_check=ok nhưng foreign_key_check có hai transcript orphan của proj_cue_ops_test và proj_real_e2e_test. Đây là dữ liệu tồn dư đã có; chưa xác định lịch sử tạo, không tự xóa. Bản sửa thống kê không che giấu kết quả kiểm toàn vẹn này.
- Bản Drive trước đó đã REMOTE_VERIFIED và RESTORE_DRILL_PASS. Đợt này không tải lại hoặc xóa cloud/local. Chuyển toàn bộ dự án sang cloud-only vẫn là chức năng chưa hoàn chỉnh, không phải đã được sửa trong audit này.

## Phạm vi kiểm định và giới hạn

Đã đọc hướng dẫn vận hành/lịch sử bug; rà sâu đường render→QA→cloud, quyền/CSRF/lease/maintenance, dashboard và quan hệ transcript. Rà các đường tải/cache/hủy/tác vụ/sao lưu qua mã và bộ hồi quy hiện có. Phân tích cú pháp212 file Python và12 file JavaScript không lỗi; đây không đồng nghĩa đã chứng minh từng nhánh trong mọi file.

Toàn bộ pytest chạy offline, DB/storage tạm cách ly trên D:. Các lượt toàn bộ bị dừng khi phát hiện và sửa thêm lỗi không được tính là kết quả hoàn tất. Kết quả toàn bộ trên mã cuối: **693 passed trong 933.53 giây (15 phút33 giây)**, verified_suite.xml. Không sửa mã thực thi sau lượt này. Frontend thực tế API.getVerify đã được chạy trong Node với transport giả: xác nhận POST và JSON, frontend_qa.json.

Không gọi lại Gemini/TTS hoặc tải video Bilibili/Douyin mới trong đợt này, nên không chứng nhận provider đang nhận yêu cầu thật hay chất lượng OCR/dịch toàn phim. Không hạ QA, không khôi phục nội dung đã xóa, không sửa transcript/người dùng hoặc xóa dữ liệu dự án.

Bằng chứng:storage/maintenance/tool_audit_20261026 gồm reproduction.log,qa_permission_reproduction.log,focused.xml,focused_final.xml,qa_permissions_fixed.log,syntax.json,live_before.json,live_media.json,encoded_full_decode.json,independent_qa.json và verified_suite.xml/log khi hoàn tất.


## Nạp bản sửa và kiểm tra sau triển khai

Chỉ nạp khi không có task QUEUED/RUNNING. Xác minh đúng PID/cwd/launcher, thay backend13216 bằng11764 thông qua launcher6704 đang quản lý; không khởi động backend song song. Server ready; OpenAPI xác nhận POST QA và GET legacy405, app.js đang phục vụ phương thức POST. Các API projects/cloud/infrastructure vẫn từ chối anonymous401.

Sau nạp:7 bảng projects/sources/media/transcripts/jobs/artifact_pointers/processing_attempts giữ nguyên hash;7 tệp chính pipeline_state,MP4,verify_report,render_config,timeline,voiceover.wav,cues.json giữ nguyên SHA256. Đây không phải đối chiếu hash mọi tệp cache; việc quét hash toàn cây bổ sung đã dừng để giảm tranh chấp I/O khi có ứng dụng khác đang chạy. SQLite integrity_check vẫnok; hai orphan lịch sử vẫn được báo, không bị xóa.

Dashboard tính lại từ mã mới:1 dự án,119 cue,119 chưa duyệt,0 thiếu transcript,0 thiếu bản dịch. Bằng chứng restart.json và release_verification.json. Phiên trình duyệt mở sẵn cần nạp lại để lấy app.js mới.
