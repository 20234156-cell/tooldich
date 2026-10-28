# Đợt 3 — Required files, baseline và phục hồi gói

## Phạm vi thực hiện

- Dùng tiếp src/server/backups.py và transport Drive hiện có. Không tạo kết nối OAuth thứ hai.
- Bổ sung bảng file_baselines và legacy_backup_references; không thay chữ/phụ đề/trạng thái công việc của dự án để làm cho bản sao đạt.
- Snapshot SQLite bằng backup API; inventory đọc snapshot ở mode=ro. REQUIRED gồm Media, nguồn local, phiên UploadSession COMPLETED, manifest của attempt hiện được ArtifactPointer công nhận và các clip có vi_text/voice_audio_path được DB tham chiếu. Pointer đã vô hiệu hóa không rơi về legacy. Transcript rỗng/chưa có output được ghi INTENTIONALLY_EMPTY. Không khám phá yêu cầu bằng cách liệt kê các file còn tồn tại.
- Legacy pipeline đang lưu công đoạn/artifact ở pipeline_state.json. Khi giữ maintenance gate, đóng băng các tham chiếu của stage COMPLETED vào bảng legacy_backup_references trước khi snapshot; sau đó verify chỉ dùng snapshot này. Không dùng JSON cũ để thay transcript DB.
- Manifest attempt gồm cả transcript/cues.json nằm cạnh voiceover, không chỉ các tệp nằm trực tiếp trong directory pointer. Chỉ công nhận khi attempt PUBLISHED, project/kind/directory khớp; không đưa attempt chưa công nhận vào gói chuẩn.
- File mới dùng expected SHA-256/size VERIFIED_AT_CREATION đã được ghi ở upload/publish. File legacy có mốc BASELINE_FROM_MIGRATION khi còn tồn tại và phù hợp expected hash/size hiện hữu. Không ghi đè baseline cũ khi phát hiện byte đổi. Thiếu file không được học thành baseline mới.
- Metadata được sửa hợp lệ (pipeline_state/ROI/config/SRT/ASS) được đóng băng theo snapshot với nhãn SNAPSHOT_METADATA, không được quảng cáo là baseline bất biến từ lúc tạo. Việc này tránh báo hỏng giả sau mỗi thay đổi cấu hình, đồng thời so lại byte khi copy/restore.
- AT-07a trả FAILED_INCOMPLETE; AT-07b trả FAILED_CORRUPT_SOURCE. Báo cáo thất bại được giữ, bản tốt trước không bị thay/xóa. Verify v2 kiểm lại inventory từ DB, files list, REQUIRED hash/size, integrity_check và nội dung các bảng nghiệp vụ.
- Maintenance gate chặn tác vụ/ghi đang hoạt động; cũng chờ background upload VERIFYING và thao tác cloud. Không copy byte của upload chưa hoàn tất hoặc cache/attempt chưa công nhận như tài nguyên bắt buộc.
- Gói ZIP có phiên độc lập, không ghi đè phiên tốt. Truyền Drive dùng lại begin/send/metadata, lưu phiên tiếp tục bằng DPAPI và xác minh size/MD5; restore drill tải lại gói và so SHA-256 toàn gói trước khi giải.
- Restore chỉ tạo thư mục mới ngoài projects đang dùng; chặn path traversal/symlink, kiểm dung lượng, PRAGMA integrity_check, so các bảng sau chuyển đường dẫn và kiểm byte REQUIRED. Thu hồi session/invite/lease của server cũ. Giữ nguyên byte JSON artifact để không phá manifest/hash từ lúc tạo. Có lệnh restore-package không cần database đang chạy để phục hồi ZIP còn giữ trên D:.

## Quyết định của chủ máy: D: trước, cloud sau

Get-Partition xác nhận C: và D: đều DiskNumber=0. Người dùng đã chọn dùng D: trước. Cấu hình đích: D:\DuanLayLinkBackups, SERVER_BACKUP_ALLOW_SAME_DISK=true.

- Gói hợp lệ trên D: mang LOCAL_REPLICA_VERIFIED; diễn tập thành công là LOCAL_RESTORE_DRILL_PASS.
- Không gọi đó là kho độc lập, không mở cờ deletion_allowed. Cần ổ vật lý/kho mạng khác hoặc Google Drive và drill đạt trước khi mở điều kiện cho dọn gốc.
- Kiểm tra kho độc lập dùng số disk vật lý Windows, không chỉ ký tự ổ. Với kiểm thử offline, tình huống kho độc lập được giả lập rõ ràng; chưa có cloud thật/ổ vật lý khác được nghiệm thu.
- Chưa có vòng đời DELETING/reader lock của Đợt 4. Luồng xóa MP4 cũ đã bị chặn nếu thiếu gói hệ thống ở kho độc lập đã drill và được đọc/kiểm lại. Bản MP4 trên Drive riêng lẻ chưa đủ mở quyền xóa gốc.

## Bằng chứng kiểm thử

- Nhóm đầu backup/cloud: 49 passed; nhóm mở rộng server: 67 passed. Nhóm cuối và suite toàn dự án được cập nhật bên dưới từ kết quả chạy thật.
- Các ca hồi quy: thiếu file trước scan, đổi 1 byte trước backup, đổi trong copy, manifest cố bỏ file REQUIRED, baseline legacy không tự học lại dữ liệu hỏng, empty cue, legacy completed stage thiếu delivery, metadata hợp lệ thay đổi, sibling transcript trong attempt, background upload VERIFYING, same-disk replica không mở xóa, chuyển kho lỗi giữ bản cục bộ, external source relocate, restore từ kho, remote hỏng thu hồi điều kiện xóa, Drive transport giả lập/offline.
- Audit thật cách ly: storage/audits/phase3_20261025T234049/report.json, success=true. FFmpeg tạo MP4/WAV thử, local package PASS, sao chép D:, restore và so toàn bộ REQUIRED + DB; sửa byte/mất nguồn trước backup bị bắt; ZIP tốt trước giữ nguyên; Chromium admin hiển thị đúng nhãn local/failed, không JS error. Không gọi provider ngoài, không sửa dự án thật, không tuyên bố chất lượng giọng qua âm sine.

## Dữ liệu thật và trạng thái triển khai

Preflight chỉ đọc phát hiện một dự án có 854 tham chiếu clip giọng nhưng file không còn. VOICE_READY của dự án đó là FAILED. Không tự xóa các tham chiếu hoặc tạo lại giọng để che thiếu dữ liệu. Master chưa được công nhận của lượt thất bại không bị suy ra là REQUIRED chỉ vì đã có một số path clip.

Do đó bản sao toàn bộ dữ liệu thật có thể trả FAILED_INCOMPLETE đúng thiết kế; không được xuất gói đó lên D:/cloud với nhãn đạt. Hai dự án có tệp đầy đủ không làm cho cả hệ thống trở thành PASS. Muốn đóng nghiệm thu backup dữ liệu thật cần phục hồi/tạo lại tệp còn thiếu bằng một quy trình riêng có đối chiếu trước/sau.

Kết quả thực tế:

- Full suite: **618 passed in 491.95s**, returncode 0; storage/audits/phase3_final_verification/{pytest.log,junit.xml,result.json}. Lượt này được thu thập trước hai regression bổ sung cuối và một số chỉnh sửa cuối ở inventory/transport.
- Nhóm cuối theo mã đã sửa: **69 passed in 62.16s** (test runner output), 0 lỗi/failure trong focused.xml cùng thư mục; gồm cả hai regression trên và ca chuyển D: sang Drive vẫn giữ bản cục bộ. Không cộng hai nhóm thành số ca độc lập hoặc nói full suite cuối có 620 ca đã chạy.
- Node syntax/compile Python đạt. Audit local-media/browser cuối thành công, 0 provider ngoài/0 JS error; có ảnh desktop/mobile để đối chiếu, không phải test tải nhiều người.
- Đã reload backend khi rảnh, trước đó snapshot SQLite. Release kiểm lại tất cả journal task/Job có trạng thái hoạt động, không chỉ lịch sử HTTP bị giới hạn 50 mục; đồng thời kiểm lease và PID/cwd/command. HTTPS public login/admin/logout và cookie Secure+HttpOnly đạt; dữ liệu nghiệp vụ nguyên vẹn.
- Live acceptance: storage/audits/phase3_live_20261025T234157/report.json. Backup API HTTP409, FAILED_INCOMPLETE, đúng 854 MISSING_REQUIRED. Có 788 baseline BASELINE_FROM_MIGRATION và 27 legacy references; nguồn của cả 3 dự án có baseline. PRAGMA integrity_check=ok; bảng projects/sources/media/transcripts/jobs nguyên vẹn trước/sau. Không đổi path/text/trạng thái để biến kết quả thành PASS.
- D: đã cấu hình và restore drill với fixture thật đạt; **chưa có gói toàn bộ dữ liệu thật VALIDATED_PASS xuất sang D:** vì còn 854 tham chiếu thiếu. Google Drive chưa kết nối; thử transport là giả lập, chưa là nghiệm thu cloud thật. Đợt 3 chưa đóng nghiệm thu dữ liệu thật/kho độc lập; Đợt 4 chưa mở dọn gốc.

Release cuối: storage/audits/phase3_release_20261025T234350/report.json, success=true; nạp thay đổi chuyển D:→Drive sau khi nhóm cuối đạt. Kiểm tra chỉ đọc bản sao legacy 20261025T180646_165461a6 cũng không thấy các clip thuộc dự án đang thiếu; chưa có nguồn khôi phục các clip đó từ bản sao này.


## Cập nhật ngày 26/10 sau Đợt 4

Nhật ký có hai lệnh xóa dự án HTTP200 trước khi nạp Đợt 4, gồm dự án có 854 tham chiếu. Không thực hiện reset trên target đã không còn. Dữ liệu hiện còn một dự án đã được backup VALIDATED_PASS trên D: và restore drill đạt 129 REQUIRED. Không coi đây là phục hồi ba dự án cũ hoặc tạo lại 854 clip. Xem docs/PHASE4_ACCEPTANCE.md và storage/audits/phase4_live_20261026T004952/report.json. Kho D: vẫn cùng DiskNumber0; chưa mở xóa gốc.
