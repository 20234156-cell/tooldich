# Đợt 4 — Đặt lại tham chiếu lỗi và dọn theo phiên xem trước

## Quy tắc triển khai

- Chọn Hướng A theo ủy quyền trong đặc tả: admin đối chiếu rồi xác nhận đặt lại CHỈ voice_audio_path/voice_duration của cue có tệp không tồn tại. Giữ toàn bộ vi_text, start/end và các trường khác. Chỉ áp dụng VOICE_READY=FAILED, không có attempt PUBLISHED/pointer giọng hoặc master legacy; chặn dự án có job/lease/reader. Kiểm tra mọi bản sao cục bộ đã biết trước khi đặt lại. Không gọi TTS hay dịch vụ ngoài để chữa dữ liệu trong lúc backup.
- Phiên đối chiếu hết hạn sau 10 phút; so fingerprint lại dưới BEGIN IMMEDIATE. Biên bản JSON immutable có danh sách cue_id/path trước và toàn bộ before/after dự kiến, fsync trước commit. Dòng stage_reset_audits và thay đổi Transcript commit cùng giao dịch. File chỉ là PREPARED_INTENT nếu chưa có dòng audit trong DB. Biên bản là REQUIRED với hash từ lúc tạo trong các bản sao sau đó.
- CleanupBatch/CleanupCandidate nằm trên server, có admin sở hữu và TTL 10 phút. API chỉ nhận batch_id/candidate_ids, extra fields bị từ chối. Không nhận file_path từ client. Preview không tự chọn; không tự xóa candidate mới. Recovery ở đầu preview chỉ hoàn tất ý định DELETING đã commit từ trước.
- Nhóm chưa công bố: upload ABORTED/EXPIRED/FAILED quá 24h, attempt UNCOMMITTED_ATTEMPT/FAILED quá 24h không được tham chiếu, tệp .part/.tmp/.part.bak quá 24h thuộc dự án không chạy. DELETE_FAILED có thể xem trước lại để retry tường minh. Không quét xóa tùy ý toàn bộ thư mục audit/model/nguồn.
- Nhóm thành phẩm: luôn khóa nếu chưa có backup kho độc lập đã drill và recheck; thêm điều kiện dự án CLOSED/ARCHIVED, đúng output pointer và không có task/reader. Với hạ tầng D: cùng DiskNumber hiện tại, không thể chọn xóa thành phẩm. Chưa nghiệm thu xóa thành phẩm thật trên kho độc lập hoặc vòng đời catalog REMOTE_ONLY sau khi giải phóng chúng; không coi backup D: là đã mở chức năng đó.
- Lúc thực hiện: kiểm batch/owner/tất cả ID trước khi đụng file; BEGIN IMMEDIATE, kiểm lại trạng thái gốc/mtime_ns/size/inode/tree fingerprint, realpath, tham chiếu DB, task và readers. Conditional UPDATE gốc có rowcount=1 mới commit DELETING. Một mục bị khóa ở preview phải xem trước lại, không được âm thầm trở thành eligible khi gọi execute sau đó.
- Sau commit DELETING mới unlink/rmtree. Kiểm lại tombstone/tham chiếu/stat trước thao tác vật lý. Không đi qua symlink/junction ngoài vùng quản lý. Trượt kiểm tra: SKIPPED_STATE_CHANGED/SKIPPED_ACTIVE_READER hoặc DELETE_FAILED nếu ý định đã commit. Lỗi OS ghi errno/strerror, thời gian và người thực hiện. Sau mất tiến trình, file không còn -> DELETED; file còn -> recheck/retry hoặc DELETE_FAILED. Partial directory khác fingerprint được giữ DELETE_FAILED để đối chiếu/xem trước lại thay vì xóa mù.
- Registry dùng refcount trong một backend. ASGI middleware giữ lease đến cuối phản hồi/cancel/disconnect, gồm Range 206; tắt pathsend hand-off để không nhả lease trước lúc truyền byte xong. Queue giữ lease dự án suốt worker/subprocess. Reader mới bị chặn khi DELETING, đúng file đã DELETED trả 404. Upload load, local source validation và attempt publish kiểm tombstone để chặn dùng lại.
- Tắt sweeper tự xóa .part/orphan và chặn xóa project/cloud MP4 trực tiếp trong SERVER_MODE. CLI không bật SERVER_MODE giữ hành vi cũ. Không tự đóng/mở/xóa dự án người dùng qua cleanup.

## Vùng làm việc sao lưu

Phát hiện C: còn khoảng 4,8 GiB trong lúc triển khai, D: khoảng 61 GiB. SERVER_BACKUP_ROOT cho phép snapshot/gói staging trên D:\DuanLayLinkBackups\snapshots, replica ZIP ở D:\DuanLayLinkBackups. Restore drill từ giao diện ở D:\DuanLayLinkBackups\restore_drills. Catalog vẫn tìm bản legacy trong storage/backups trên C:, không di chuyển/xóa chúng ngầm. Cả hai phân vùng vẫn là DiskNumber=0: chỉ LOCAL_REPLICA_VERIFIED/LOCAL_RESTORE_DRILL_PASS, không độc lập vật lý.

Restore vô hiệu hóa CleanupBatch cũ và reset preview tokens cùng session/invite/lease. Giữ lịch sử cleanup và audit reset; đối chiếu bảng có tính đến thay đổi vận hành có chủ ý này. Verify/restore vẫn đọc được manifest v2 cũ chưa có các bảng vận hành Đợt 4.

## Bằng chứng

- Test tập trung có barrier cho AT-06a/b/c; giữ reader rồi execute; commit DELETING rồi chặn reader/upload mới; đổi stat; phục hồi sau mark/unlink; PermissionError errno; candidate giả/path extra; batch hết hạn/khác admin; disconnect; sweep bypass; reset giữ chữ/timing và chặn dữ liệu đã đổi/published; audit có mặt trong backup; custom root và v2 compatibility.
- Audit cách ly storage/audits/phase4_20261026T003608/report.json đạt: MP4 FFmpeg thật, reset có biên bản, gói D: và restore evidence, child os._exit(74) sau commit DELETING, recovery xóa nốt; Windows mở tệp thật gây DELETE_FAILED, admin preview lại sau nhả lock xóa được; HTTP Range 206, chọn checkbox/xác nhận qua Chromium chỉ xóa nháp, giữ nguồn; 0 lỗi JS/0 provider ngoài. Ảnh desktop/mobile trong cùng thư mục. Đây không phải thử mất điện vật lý hoặc benchmark tải nhiều người.
- Registry/refcount và việc loại trừ tiến trình chỉ bảo đảm cho backend/worker/subprocess đi qua quy trình này; Windows OS lock được thử thật. Không tuyên bố kiểm soát mọi chương trình ngoài ứng dụng, nhiều backend hay writer sửa DB trực tiếp.

## Kết quả cuối và dữ liệu thật

- Full suite lần đầu trên C:: 633 passed, 1 lỗi LibsndfileError/System error khi ghi WAV của test 200 cue; storage/audits/phase4_final_verification/{pytest.log,junit.xml,result.json}. Không sửa TTS, giảm 200 cue hoặc hạ ngưỡng QA để bỏ lỗi. C: lúc kiểm tra còn khoảng 4,37 GiB; chưa chứng minh được nguyên nhân duy nhất của lỗi I/O đó. Lệnh dọn thư mục tạm C: bị auto-review chặn (blocked by policy); không tìm cách xóa vòng qua lệnh bị chặn.
- Chạy lại toàn bộ trên vùng tạm D: mới: **639 passed in 599.62s**, returncode 0. Bằng chứng: storage/audits/phase4_final_verification/final_d/{pytest.log,junit.xml,result.json}. Cùng bài 200 cue đạt trong lượt này; không khẳng định lỗi I/O ban đầu đã được tái hiện/chẩn đoán đầy đủ.
- Nhóm backup/cloud/server/recovery mở rộng: 88 passed in 98.30s. Sau bổ sung cuối chặn attempt directory ra ngoài project/attempts và bảo vệ audit references: **20 passed in 25.29s**, final_guard.xml cùng thư mục. Không cộng các nhóm thành số ca độc lập; full suite được thu thập trước regression guard cuối. Node syntax/Python compile đạt.
- Audit fixture mới nhất: storage/audits/phase4_20261026T003608/report.json, success=true, 0 provider ngoài/0 JS error. Dùng cả snapshot staging trên D:, HTTP Range thật, child process exit74, Windows file lock thật, UI chọn/xóa nháp, nút xóa không bật lại khi lựa chọn rỗng, đọc biên bản admin và restore audit evidence.

### Thay đổi dữ liệu từ phiên quản trị khác trong lúc triển khai

Nhật ký server ghi hai lệnh DELETE /api/projects/... thành công HTTP200 lúc **00:38:20 và 00:38:22 ngày 26/10/2026 (UTC+7)**, cho proj_2ee212e3304d và proj_e53eaadf2159. Lệnh thứ hai là dự án có 854 tham chiếu lỗi. Những lệnh này xảy ra trước lần nạp đầu Đợt 4 lúc 00:40:56. Không suy đoán ai/nguyên nhân thao tác ngoài bằng chứng này.

Snapshot/checksum trước và sau lần nạp đầu đều có projects=1, sources=1, media=1, transcripts=3, jobs=1; dữ liệu nghiệp vụ bằng nhau. Vì target không còn, **không chạy reset 854 tham chiếu và không tự khôi phục các dự án đã xóa**. Công cụ reset đã nghiệm thu trên fixture, không gắn nhãn APPLIED cho dữ liệu thật. Các bản sao legacy/snapshot cũ vẫn được giữ. Biên bản: storage/audits/phase4_release_20261026T004056/report.json và các event trong báo cáo live.

### Backup/restore trên dữ liệu thật còn lại

- Gói **20261026T004621_89f1cb93**, 1390 tệp, VALIDATED_PASS, LOCAL_REPLICA_VERIFIED. Snapshot: D:\DuanLayLinkBackups\snapshots\20261026T004621_89f1cb93. ZIP: D:\DuanLayLinkBackups\20261026T004621_89f1cb93.zip.
- Restore drill thật: D:\DuanLayLinkBackups\restore_drills\20261026T004621_89f1cb93_f6328c83. LOCAL_RESTORE_DRILL_PASS, **129 REQUIRED** khớp size/SHA, kiểm DB đạt. Là bộ dữ liệu hiện có một dự án, không phải khôi phục bộ ba dự án cũ.
- Lượt audit live đầu tạo/drill thành công nhưng harness lỗi import lúc kết thúc; không đổi báo cáo đó thành success. Lượt sau dùng lại đúng gói/drill đã tạo, verify gói lại và đọc/so lại 129 REQUIRED trong restore, kiểm integrity DB và đối chiếu business snapshot. Không tạo thêm bản sao/diễn tập trùng chỉ để lấy báo cáo xanh.
- Báo cáo cuối: storage/audits/phase4_live_20261026T004952/report.json, success=true. projects/sources/media/transcripts/jobs nguyên vẹn, SQLite ok, preview có một thành phẩm và bị khóa; **không xóa rác thật, không sửa transcript thật, không gọi provider ngoài**. Bằng chứng thao tác backup/drill lần đầu: phase4_live_20261026T004620/report.json.
- Lần nạp đầu API/DB đạt nhưng public TLS không đạt vì Tailscale STOPPED. Đã kết nối lại cấu hình hiện hữu bằng tailscale up; Funnel vẫn proxy 127.0.0.1:8088, không tạo tài khoản/link mới. Live audit và release cuối đều kiểm public HTTPS đăng nhập admin, trang Dọn dữ liệu, cookie Secure+HttpOnly, logout rồi API projects401; 0 JS error.
- Release cuối sau guard test đạt: storage/audits/phase4_release_20261026T005345/report.json, success=true; idle0 tasks/leases, đúng PID/cwd/command, schema và API cleanup sẵn sàng, business rows bằng nhau, không tác vụ bị ngắt.

Chốt phạm vi: kỹ thuật dọn nháp/hủy và công cụ reset đã được kiểm thử/triển khai. Backup/restore dữ liệu hiện còn trên D: đạt. Kho vật lý độc lập/Drive, thiết bị mạng ngoài thứ hai và xóa thật thành phẩm trên kho độc lập chưa nghiệm thu; quyền xóa gốc vẫn khóa. Không cam kết chống mất điện/hỏng toàn bộ ổ Disk0 hoặc tải nhiều video 24/7.
