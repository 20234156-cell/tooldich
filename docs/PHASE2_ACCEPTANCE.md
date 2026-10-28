# Đợt 2 — Phục hồi, công bố kết quả và tài nguyên

Ngày: 25/10/2026 (Asia/Bangkok).

## Cơ chế đã triển khai

- Job/TaskJournal có trạng thái INTERRUPTED và thông tin công đoạn/lý do/tóm tắt. Pipeline giữ current_state ở mốc hợp lệ cuối cùng, stage dở mang INTERRUPTED; không thay current_state bằng một nhãn khiến mất mốc công việc. Khởi động không tự enqueue provider work.
- Phân biệt dự án mới PENDING chưa gửi việc với job đang PROCESSING hoặc journal QUEUED/RUNNING thực sự bị mất tiến trình.
- API recovery/resume so chữ ký nguồn, dữ liệu DB, ROI, tùy chọn, phiên bản và thiết lập engine. Giao diện hiển thị nguyên nhân, chọn thiết lập hiện tại/lượt trước, yêu cầu xác nhận nếu khác. Resume là tác vụ mới, vẫn qua quyền ghi và hàng đợi.
- Cột Job current_attempt_id/active_attempt_id/current_fencing_token/input_config_version bổ sung bằng migration; bảng ProcessingAttempt giữ manifest/hash và trạng thái; ArtifactPointer theo project/kind cho phép giọng và render công bố độc lập trong chuỗi run-all.
- Guard công bố dùng trạng thái Job PROCESSING (tên enum Job hiện hữu), trong khi Task dùng RUNNING. UPDATE có điều kiện token/lượt/fingerprint/trạng thái và kiểm rowcount. So lại DB đầu vào trong transaction đã lấy quyền ghi. Transcript và pointer của bản giọng được commit cùng transaction.
- File mới hoàn tất/fsync trong thư mục attempt riêng, manifest SHA-256/size ghi VERIFIED_AT_CREATION cho file mới. Reader master/timeline/cue audio/QA/download đọc con trỏ hiện hành. Không chuyển nhiều file lần lượt lên một thư mục dùng chung cho bản giọng mới ở SERVER_MODE.
- Hủy tăng token; lượt cũ không được đổi trạng thái Job mới. Khởi động đánh dấu STAGING còn sót là UNCOMMITTED_ATTEMPT. Không tự xóa thư mục chưa công nhận.
- Cache TTS mới có checksum; OCR nhận dạng ảnh VSF giữ cache từng ảnh đúng pixel/version/language. Cache/master hiện hữu không bị đổi thành dữ liệu mới có baseline từ lúc tạo; không gắn nhãn VERIFIED_AT_CREATION cho tệp cũ thiếu bằng chứng từ lúc tạo.
- Kiểm tra đĩa trước và trong công việc, monitor tác vụ nặng, dừng FFmpeg và nhả slot trước các lần ghi nhật ký cuối. Lỗi ENOSPC/SQLITE_FULL có đường thoát khi việc ghi trạng thái cũng thất bại.
- Nhận diện stderr lỗi giải mã trong render/audio extraction, trả CORRUPT_MEDIA_STREAM với công đoạn/stream/exit code/stderr và timestamp chỉ nếu công cụ cung cấp. Không quy mọi lỗi FFmpeg thành lỗi nguồn.

## Kiểm chứng đã có

- Mốc toàn bộ suite trước các bổ sung cuối: 595 passed in 427.25s.
- Nhóm mở rộng recovery/TTS/server/OCR/render: 80 passed in 91.70s.
- Nhóm recovery+server sau sửa PENDING: 30 passed in 32.90s.
- Test dùng barrier: hủy/mất fence trước publish, đổi transcript thành rỗng và không cho kết quả cũ ghi đè. Test năm tệp TTS hoàn tất được giữ và chỉ gọi fake provider cho năm câu còn thiếu.
- Test OCR nhận dạng giữ hai kết quả ảnh trước gián đoạn và chỉ nhận dạng hai ảnh còn thiếu.
- Test media thật: MP4 60s với packet video hỏng ở khoảng 15s, ngoài vùng lấy mẫu; sample decode đạt, render thật phát hiện CORRUPT_MEDIA_STREAM. Vị trí này tránh prefetch của decoder làm ca thử vô tình rơi vào phần được sample đọc. Không sửa giảm ngưỡng validator.
- Test render thật bị chặn ở vòng kiểm tra tài nguyên: xóa tmp encode và giữ nguyên input/giọng/bản xuất trước. Inject ENOSPC/SQLITE_FULL trên storage test; không làm đầy ổ dữ liệu thật.
- Audit storage/audits/phase2_20261025T205428/report.json: WAV tổng hợp cục bộ + H264/AAC thật + FFmpeg render/QA. Child process os._exit(74) trước publish, bản trước giữ checksum; startup nhận INTERRUPTED/uncommitted; mở web không tự chạy, bấm resume qua Chromium hoàn tất, không JS error; card mobile nằm trong viewport. Không gọi dịch vụ provider thật, không khẳng định chất lượng lời đọc bằng WAV thử.

## Lỗi bootstrap pytest được phát hiện và xử lý

Một số module test import app/ProjectManager trong lúc collection, trước autouse fixtures, nên init_db có thể chạy migration bổ sung trên DB thật. Đã chuyển DATABASE_URL/STORAGE_DIR sang thư mục tạm ngay đầu conftest trước import settings/app và dispose/cleanup sau session.

Schema thật đã nhận các cột/bảng bổ sung sớm, trước reload backend; không rollback schema đang được dùng. Đối chiếu các cột chung với snapshot Đợt 1 cho projects/sources/media/transcripts/jobs cho kết quả bằng nhau toàn bộ, không chỉ cùng số dòng. Bằng chứng: storage/audits/phase2_collection_isolation/comparison.json. Bộ suite cuối được chạy lại sau sửa bootstrap. Snapshot trước reload sau này phải được gọi đúng là snapshot trước reload, không khẳng định còn schema trước Đợt 2.

## Các giới hạn cần giữ rõ

- Fencing hiện tại phục vụ một backend/SQLite; chưa phải hệ thống cấp lease worker phân tán.
- Lỗi mất tiến trình được thử thật, không tương đương mất điện vật lý/độ bền bộ đệm phần cứng. Nếu database không ghi được trạng thái, reason cụ thể có thể không còn sau mất tiến trình; startup báo SERVER_RESTART theo bằng chứng có được.
- OCR có cache nhận dạng theo ảnh và scan manifest hiện hữu; không hứa nối tiếp ở một frame chưa có checkpoint hoàn tất hoặc âm thầm công nhận scan chưa xong.
- Kiểm tra chữ ký không thay thế kiểm tra file/cache khi dùng. File thiếu/hỏng có thể phải tạo lại sau xác nhận tiếp tục; không khẳng định mọi cache đều còn đủ.
- Đây chưa là đo tải ba video dài đại diện; clip ngắn/WAV thử kiểm chứng kỹ thuật và trạng thái.
- Chưa triển khai vòng đời xóa có DELETING/reader lock, required-file backup ra kho độc lập (Đợt 3–4). Không tự dọn attempts mới.

## Kết quả suite cuối và triển khai

- Suite toàn dự án cách ly: **602 passed in 430.95s**, returncode 0. Bằng chứng: storage/audits/phase2_final_verification/{pytest.log,junit.xml,result.json}.
- Sau thay đổi cuối giữ phạm vi dịch khi resume: **54 passed in 62.354s**, gồm 18 ca recovery và các ca translation/web. Bằng chứng focused.xml cùng thư mục. Không cộng hai nhóm thành số ca độc lập; toàn suite 602 được thu thập trước regression phạm vi dịch cuối.
- Audit FFmpeg/Chromium/process crash cuối: storage/audits/phase2_20261025T225829/report.json, success=true, provider_calls=0, browser_errors=[].
- Đã reload server thật lúc rảnh (0 tác vụ đang chạy/chờ, 0 lease sửa dự án), sau snapshot SQLite trước reload. Chỉ dừng launcher/backend đã đối chiếu PID, cwd và command; không kill theo tên hoặc cổng.
- Biên bản triển khai: storage/audits/phase2_release_20261025T231041/report.json. Dữ liệu projects/sources/media/transcripts/jobs bằng nhau trước/sau; integrity_check=ok; API recovery và bảng attempt sẵn sàng. Phiên đăng nhập hiện hữu còn hiệu lực sau reload.
- Chromium đăng nhập qua HTTPS public, cookie Secure+HttpOnly, tải recovery.js, mở trang quản lý admin, đăng xuất rồi API projects trả 401. Không lỗi JavaScript. Các phiên kiểm thử được đăng xuất; không tạo/sửa nội dung dự án thật.
- Đây là kiểm chứng public từ trình duyệt trên máy chủ; xác nhận từ một thiết bị vật lý dùng mạng khác vẫn chưa có. Không coi việc đăng nhập này là test tải nhiều video hoặc cam kết uptime.

Resume dịch giữ ID các câu thuộc phạm vi yêu cầu gốc và đọc chữ mới từ DB. Không phát lại snapshot chữ cũ hoặc âm thầm mở rộng sang mọi câu. Journal cũ thiếu phạm vi hoặc chứa câu đã xóa bị từ chối; người dùng chọn lại phạm vi dịch trong Studio.
