# Kiểm chứng tiếp theo ngày 26/10/2026

## Kết quả hiện có

681/681 test offline đạt trong 576,86 giây, không lỗi/skip; JUnit tại `storage/maintenance/followup_20261026_tests.xml`. Phân tích cú pháp 209 file Python và kiểm tra 12 file JavaScript đạt. Nhóm hồi quy tính toàn vẹn/OCR/phục hồi có 89 test đạt trước các guard cuối; nhóm nhập video/server có 40 test đạt; lần toàn bộ bao gồm tất cả sửa mã.

| Kiểm chứng thật | Kết quả | Bằng chứng |
| --- | --- | --- |
| Tải mới Bilibili BV1JGJy6rEU5 | 76.773.991 byte; H.264/AAC, 1920×822, 568,51 giây; giải mã toàn bộ đạt; SHA-256 trùng nguồn hiện có | `storage/audits/full_fresh_20261026T135156/report.json` |
| Dùng chữ hiện có, giọng/render mới | 119/119 câu; video cuối qua QA và strict decode; 784,56 giây | `storage/audits/full_reviewed_20261026T135920/report.json` |
| OCR mới, Gemini mới, Edge mới, render/QA | 276 ảnh VSF → 119 cue; đối chiếu/sửa riêng cue 112 trong audit; timeout dịch được giữ lỗi rồi chạy tiếp từ cache; bản cuối đạt QA/strict | `storage/audits/full_fresh_20261026T140512/report.json` |
| Hai người dùng trên browser/HTTP thật | Upload/import, khóa người sửa thứ hai, Range 206/1024 byte, chưa đăng nhập bị chặn, hai Chromium context, session/task qua restart; 0 lỗi JavaScript | `storage/audits/followup_browser_fixed_20261026/report.json` |
| Kiểm tra cấu hình vùng che | Phát hiện lộ chân chữ ở 470,2 giây; tăng box_padding từ 6 lên 28 trong audit, render lại và đối chiếu ảnh đã che kín | `storage/audits/full_fresh_20261026T140512/cover_review.json`, `encoded_cover_fixed.jpg` |

Lượt tải đầu dừng vì harness đặt cấu hình phụ đề trước trạng thái cho phép, dù tải đã đạt. Harness được sửa đúng thứ tự; không coi lỗi harness là lỗi pipeline. Các lượt thất bại và lịch sử `previous_runs` được giữ vì là bằng chứng của công việc hiện hành, không đổi thành nhãn thành công.

## Chỉ số thành phẩm

| Chỉ số | Chữ hiện có + giọng mới | Từ OCR/dịch mới, sau sửa vùng che |
| --- | --- | --- |
| Số cue dịch/giọng | 119/119 | 119/119 |
| Thời lượng nguồn → đầu ra | 568,51 → 568,50 giây | 568,51 → 568,51 giây |
| Lệch timing trung bình/lớn nhất | 0,150 / 1,060 giây | 0,184 / 1,063 giây |
| Loudness / true peak | −14,39 LUFS / −1,78 dBTP | −14,47 LUFS / −1,55 dBTP |
| QA kỹ thuật và giải mã toàn bộ | Đạt | Đạt |

Không hạ ngưỡng: lệch trung bình ≤0,3 giây, lớn nhất ≤1,5 giây, lệch thời lượng ≤0,1 giây, đủ 100% giọng; loudness −14,5 đến −13,5 LUFS và true peak ≤−1,5 dBTP.

`fully_passed` vẫn false vì AI đã rút gọn một số câu cho timing. Cảnh báo duyệt nghĩa/nghe lại được giữ. Đã xem các khung hình đầu/giữa/cuối chọn mẫu, chưa nghe/xem duyệt thủ công từng giây của toàn bộ phim. Timing đo theo cue OCR, chưa phải phép căn từng từ theo lời nói. Âm thanh gốc là bản mix, chưa tách riêng thoại/nền.

Lượt fresh gồm 147,61 giây OCR/lần đầu, 102,25 giây dừng do timeout dịch, 414,69 giây chạy tiếp thành công và 213,97 giây render lại vùng che: tổng thời gian xử lý các lượt 878,52 giây. Số này có các bước kiểm định lặp lại, không gồm thời gian chờ đối chiếu giữa các lượt; không phải benchmark độc quyền tài nguyên hay cam kết thời gian cho mọi video.

## Các sửa đổi có hồi quy

- Dịch từng câu trong server: giữ ID số xuyên API → journal → tiếp tục → orchestrator; test dùng HTTP có đăng nhập và DB thật.
- Legacy ProjectManager giữ Job.status PROCESSING của queue đến khi queue công bố terminal; current_stage tiếp tục diễn tả công đoạn. Sửa false CANCELLED sau upload/import mà audit browser đã tái hiện.
- Thất bại ghi ROI được báo và phục hồi cấu hình trước, không tiếp tục vô hiệu hóa đầu ra.
- Chạy tất cả nhận/trả đúng con trỏ thành phẩm đã công bố; không render lại chỉ do thiếu đường dẫn legacy.
- Thay tệp tải bằng os.replace trực tiếp; DASH phải kiểm định candidate trước khi thay thành phẩm trước.
- Manifest VSF/render sai kiểu trở thành cache miss; scan identity có SHA-256 nguồn, binary và settings native, cùng thiết lập proxy/CUDA/threads.
- VSF 6.10 chỉ được chấp nhận quy ước exit −1 khi đúng hash binary, có completion SRT mới của lượt quét và không xuất hiện lỗi native mới. Ảnh lẻ hoặc marker cũ không đủ. Kết luận ban đầu “mọi exit khác 0 là crash” đã được loại bỏ sau đối chiếu nguồn và chạy thật.
- Proxy/VSF đọc stdout/stderr liên tục với tail giới hạn, có timeout và terminate/wait khi gián đoạn tài nguyên; engine dự phòng được ghi đúng tên.

Chi tiết nguyên nhân và test: `storage/maintenance/BUG_REGRESSIONS.md`; test bổ sung tập trung trong `tests/test_integrity_followup.py` và các test hiện có. Giao diện và quy trình thao tác nằm trong README.

Nguồn kỹ thuật cho quy ước CLI: [VideoSubFinder OnInit](https://github.com/SWHL/VideoSubFinder/blob/main/Interfaces/VideoSubFinderWXW/VideoSubFinderWXW.cpp) chạy search đồng bộ, join thread rồi trả false để thoát CLI; bản bundled có SHA-256 `7d8eb55f08f489aad9067c179806b98ac61a77f57272a9e6a688204fdc95abbe`. Đã chứng minh trên video thật: `projects/audit_fresh/transcript/vsf_process.json` ghi exit 4294967295, completion_sentinel=true, completed=true, native_error rỗng. Không suy diễn OOM từ exit này.

## Tài nguyên và dữ liệu

Máy có 8 luồng CPU, khoảng 8 GiB RAM, GTX 1050 Ti 4 GiB. Lượt reviewed đo tổng working set cây tiến trình cao nhất khoảng 713,6 MiB; lượt fresh trước render sửa vùng che khoảng 691,7 MiB. Các tiến trình khác và kiểm thử có lúc cùng chạy, nên không quy các số này thành tốc độ CPU/GPU riêng của pipeline. NVENC được dùng thật trong render. Chưa có so sánh encoder/CPU mới đủ kiểm soát để đổi mặc định.

Benchmark VieNeu 1/2/4 luồng được chạy và trả `deferred`: dưới 2 GiB RAM khả dụng. Không đổi VIENEU_CPU_THREADS hoặc tự dừng ứng dụng của người dùng. Báo cáo: `storage/audits/followup_vieneu_cpu_20261026/report.json`.

Audit dùng database/project riêng. Kiểm tra các hàng dự án/nguồn/media/transcript/job và SHA-256 nguồn gốc trước/sau đều giữ nguyên. Cue 112 và phần đệm che chỉ được sửa trong audit. Không thay cấu hình dự án thật. Tệp snapshot phục vụ lần nạp backend hiện hành tách riêng trong `storage/audits/followup_release_20261026`; không khôi phục archive/backup dọn dẹp cũ.

## Phạm vi còn lại

Đã thực hiện tải, OCR, dịch, giọng, render, QA trên video 9 phút 29 giây được người dùng chọn. Chưa chứng nhận video 60–180 phút, mọi provider/nguồn, nhiều máy vật lý hoặc bản sao trên ổ vật lý độc lập. Không tuyên bố mọi file đã được rà sâu hay hệ thống không thể còn lỗi. Cần phân biệt các phần này với milestone kỹ thuật đã kiểm chứng ở trên.

Backend đã nạp lại khi không có task/Job đang chạy hoặc lease sửa đang hiệu lực. Đã kiểm tra chính xác PID, command line, workspace và thời điểm tạo tiến trình; launcher/backend cũ 14328/12492 được thay bằng 1240/17292. Snapshot SQLite nhất quán trước reload được giữ cho đúng đợt phát hành này. Sau reload: `integrity_check=ok`, các hàng business giữ nguyên, **14.973 tệp / 918.311.565 byte** được đọc lại SHA-256, không mất/đổi tệp nào. Địa chỉ local/public trả đúng 200/401; phiên admin kiểm định tạm được tạo nội bộ để kiểm tra quyền đọc/logout rồi xóa, không coi đó là kiểm tra mật khẩu đăng nhập. Đăng nhập mật khẩu thật đã kiểm chứng trên server audit Chromium riêng. Kết quả cuối: `VERIFIED_PASS` tại `storage/audits/followup_release_20261026/report.json`.
