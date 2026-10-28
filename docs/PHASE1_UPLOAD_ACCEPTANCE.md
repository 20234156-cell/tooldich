# Đợt 1 — Kết quả triển khai upload tiếp tục và truy cập HTTPS

Ngày: 25/10/2026 (Asia/Bangkok).

## Đã triển khai

- URL ứng dụng: https://nhunhi.tail343939.ts.net/ — Tailscale Funnel proxy tới 127.0.0.1:8088; không cần mua tên miền. Dùng tài khoản ứng dụng, không dùng tài khoản Tailscale để đăng nhập Studio.
- UploadSession/UploadChunk và Project.source_upload_id có unique index. Migration SQLite bổ sung, không xóa bảng cũ; có test DB cũ và chạy migration hai lần.
- API /api/uploads: init, danh sách theo chủ tài khoản, PATCH chunk với offset/checksum, status đối chiếu ledger, complete 202/status và trả cùng project_id sau hoàn tất, hủy phiên không xóa byte.
- Mỗi phần mặc định tối đa 8 MiB, chỉ xác nhận sau flush/fsync và transaction SQLite. Gửi lại phần đúng không tăng file; khác nội dung hoặc nhảy offset bị từ chối. File thừa sau crash bị cắt về prefix các mảnh liên tục khớp checksum.
- Có giới hạn tổng upload hiện hữu 500 MiB, số phiên hoạt động và dự trù đĩa cho các phiên khác; tối đa hai request nhận body cùng lúc để giới hạn bộ nhớ. Các con số đều là cấu hình hiện tại hoặc giới hạn kỹ thuật, chưa phải kết quả benchmark video dài.
- Kiểm định ffprobe và giải mã mẫu có giới hạn thời gian. Sample decode sử dụng chung heavy slot để không chạy đồng thời với công đoạn nặng hiện có. Công việc kiểm định chạy ngoài request hoàn tất, có thể hỏi trạng thái sau khi đóng trang.
- Tạo Project/Source/Job, liên kết upload và ghi nhận nguồn đã kiểm định trong cùng transaction. File nguồn có danh tính upload bất biến; bước tải/nhập media hiện hữu vẫn hoạt động với nguồn mới.
- Giao diện cho tạm dừng sau mảnh hiện tại, chọn lại tệp để tiếp tục sau reload, xem trạng thái/kiểm tra kết quả và mở dự án. Có SHA-256 fallback cho HTTP LAN không có Web Crypto. Fingerprint đầu/cuối không thay thế hash toàn bộ tệp gốc; API hỗ trợ expected_sha256 toàn file.
- Cleanup cũ loại trừ rõ vùng storage/uploads/sessions. Không có xóa ngầm các byte phiên đã hủy/hết hạn; dọn vòng đời tổng quát thuộc Đợt 4.
- Uvicorn nhận danh sách proxy IP cụ thể; không cho wildcard. Public hostname chính xác được cấu hình, Origin/CSRF giữ kiểm tra, cookie public HTTPS có Secure/HttpOnly/SameSite.

## Bằng chứng kiểm chứng

| Bằng chứng | Kết quả | Giới hạn |
|---|---|---|
| Toàn bộ pytest | 585 passed in 567.64s | Offline/cách ly; chạy tại mốc tích hợp lớn. |
| Nhóm server/upload/backup/launcher | 37 passed in 41.88s | Sau tích hợp proxy; test lỗi Origin tạo request riêng vì Request cache header. |
| Test upload cuối | 11 passed in 16.05s | Chạy lại sau giới hạn hai body stream và thay đổi UI nhỏ. |
| Node SHA-256 fallback | 7 kích thước 0,3,55,56,64,1000,8388608 byte khớp native crypto | Kiểm tra bản fallback không có Web Crypto. |
| storage/audits/resumable_20261025T195350/report.json | Browser tiếp tục phiên 110126544 byte; ngắt phản hồi sau server commit; không trùng dự án; hash khớp; nhập qua pipeline cũ đạt | Loopback; video 2s H264/AAC cộng MP4 free box để thử kích thước truyền, không phải benchmark video dài. |
| storage/audits/funnel_upload_20261025T200147/report.json | Upload 110126544 byte qua HTTPS/public relay trong 113.05s; request body <=8 MiB; duplicate không tăng file; 202 rồi hoàn tất một dự án; hash khớp; chặn anonymous/thiếu CSRF; cookie Secure; logout401 | Server audit/database riêng. Kết nối buộc tới IP công khai lấy từ DNS ngoài, không dùng đường MagicDNS nội bộ. Nguồn vẫn là PC này, chưa phải máy vật lý thứ hai. |
| storage/audits/phase1_release_20261025T200510/deployment.json | Snapshot DB trước nâng cấp; migration và integrity_check đạt; counts 3 projects/3 sources/3 media/5 transcripts/3 jobs giữ nguyên; endpoint upload200; public login/admin200, logout200 và truy cập sau logout401 | Snapshot trước migration là SQLite-only, không phải backup media đầy đủ. Không thay đổi project người dùng. |
| storage/audits/phase1_release_20261025T200510/public_browser.json | Chromium qua public relay thấy hostname/login, sáu mục admin, đúng public share URL và UI upload; không lỗi JS; logout200/401 | Không tạo dự án thật của người dùng trong lần kiểm tra public browser. |

## Đối chiếu các ca AT của Đợt 1

- AT-01a/b/c: test API gửi trùng đúng, trùng sai và nhảy offset đạt. Browser kiểm tra phản hồi bị ngắt sau commit; public relay kiểm tra gửi lại mảnh đầu không tăng dữ liệu.
- AT-02a: hai PATCH đồng thời chỉ ghi một mảnh; ledger một dòng. AT-02b: barrier giữ VERIFYING, lần complete sau nhận202. AT-02c: gọi lại nhận200 cùng một project_id, unique source_upload_id.
- AT-03a: fault injection sửa byte mảnh đã xác nhận và thêm tail chưa xác nhận; status đối chiếu/lùi/truncate đúng. Recovery VERIFYING khi chưa có project trở về UPLOADING được test. Chưa ghi nhận kill tiến trình đúng giữa một syscall ghi, chưa phải thử mất điện vật lý.
- AT-08a: dùng MP4 thật bị cắt cụt; thêm ca metadata đọc được nhưng mdat bị hỏng để sample decoder từ chối. Không thay media validator bằng mock trả đạt; barrier của test complete chỉ giữ nhịp rồi gọi validator thật.
- AT-08b: không coi kiểm tra mẫu là chứng nhận toàn bộ dữ liệu; chưa thêm trạng thái FAILED_CORRUPT_MEDIA xuyên mọi công đoạn ngoài vùng mẫu.

## Phạm vi còn lại

- Chưa triển khai Đợt 2 (state INTERRUPTED đồng bộ, fencing/publish pointer mọi bước, kiểm soát đĩa xuyên OCR/TTS/render), Đợt 3 (backup required-file baseline/kho độc lập), Đợt 4 (DELETING/dry-run/reader protection tổng quát).
- Tiếp tục sau reload cần chọn lại cùng tệp; không tự đọc đường dẫn máy khách. Kiểm tra tên/kích thước/fingerprint mẫu không chứng minh phần giữa file gốc giống hệt nếu client không cung cấp full SHA-256.
- Chưa đo ba video dài đại diện về RAM, đĩa tạm và tỷ lệ xử lý. Không suy ra công suất render từ tốc độ upload 113 giây.
- Link công khai cần máy chủ, backend và Tailscale còn chạy. Cấu hình Funnel chạy nền; launcher hiện khởi động theo đăng nhập Windows, không phải cam kết trước lúc đăng nhập hoặc khi mất điện.
- Người dùng nên xác nhận cuối bằng thiết bị thứ hai dùng 4G/mạng khác. Public relay đã được thử từ PC nguồn; chưa có xác nhận thao tác của máy thứ hai.
