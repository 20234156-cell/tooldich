# Rà soát tính toàn vẹn ngày 26/10/2026

## Phạm vi và cách kiểm chứng

Đọc README và lịch sử hồi quy trước khi sửa. Kiểm kê cấu trúc, phân tích cú pháp 207 file Python trong src/config/scripts/tests/main.py và kiểm tra cú pháp toàn bộ 12 file JavaScript của web. Rà sâu các đường tải Range/tuần tự, kiểm định media, khóa cache giọng và checkpoint; đối chiếu thêm các đường upload, đọc/xóa dữ liệu, quyền truy cập, cache dịch và OCR. Quét cú pháp không đồng nghĩa đã đọc và chứng minh mọi nhánh của từng file.

Giữ thay đổi sẵn có trong worktree và dữ liệu dự án. Không thay cấu hình vận hành, không khởi động lại backend, không gọi provider trả phí hoặc xóa dữ liệu đang dùng. Không tạo lại kho lưu trữ của các đầu ra đã được yêu cầu dọn.

## Các lỗi đã tái hiện và sửa

| Lỗi | Hành vi sau sửa | Kiểm chứng |
| --- | --- | --- |
| Range nhận diện nguồn thiếu hostname | URL đầy đủ quyết định danh tính resume; bỏ fragment | Hai host cùng path/size phải tải bytes riêng |
| Range nuốt BLOCKED_URL | Giữ nguyên lỗi và không thử lại vô ích trong worker | Một request, không backoff, giữ mã lỗi |
| Tải tuần tự nối file dở không rõ nguồn/sai Range/dư byte | Chỉ tiếp tục khi marker URL hash khớp, 206 khớp offset/tổng; đổi URL thì tải lại | Bốn trường hợp offline và HTTP loopback thật |
| Khóa TTS nối trường bằng dấu hai chấm bị trùng | JSON có ranh giới trường cho trường có `:`; giữ khóa cũ không nhập nhằng | Hai giọng/nội dung từng trùng hash nay tách cache |
| Manifest giọng sai kiểu gây AttributeError | Sai cấu trúc trở thành cache miss | null/list/string/number/files sai kiểu |
| FFmpeg báo lỗi nhưng exit 0 vẫn được nhận | `-xerror`, kiểm tra stderr ở mức error, `-nostdin` | Test cả hai chế độ và tệp MPEG-TS hỏng thật |

Chi tiết nguyên nhân và test nằm trong `storage/maintenance/BUG_REGRESSIONS.md`. Hướng dẫn nạp lại/cache nằm trong README.

## Bằng chứng media thật

`storage/audits/code_review_20261026T125221/report.json` lưu hash và kết quả của video H.264/AAC tổng hợp dài 5 giây:

- Nguồn lành qua fast và strict.
- Sửa payload các packet MPEG-TS tạo lỗi giải mã: lệnh FFmpeg cũ trả 0 kèm lỗi; strict mới từ chối MEDIA_CORRUPTED.
- Resume qua HTTP loopback thật tạo thành phẩm có SHA-256 khớp nguồn và qua strict.
- HTTP trả sai offset bị từ chối trước khi xuất bản target.

Audit HTTP tiêm transport loopback chỉ trong tiến trình thử, không đổi chính sách SSRF sản xuất. Các kết quả này không chứng nhận provider, độ chính xác dịch/OCR, chất lượng giọng hay tốc độ xử lý phim dài.

## Tương thích và giới hạn

Kiểm thử cuối sau toàn bộ sửa mã: **656 passed trong 481,05 giây (8 phút 01 giây)**, offline và cách ly trên D:. Kết quả JUnit: `storage/maintenance/review_20261026_tests.xml`. Các nhóm tập trung trước đó: 35 test tính toàn vẹn, 34 test giọng/checkpoint và cuối cùng 25 test download đều đạt. Không có thay đổi mã sau lần chạy toàn bộ này.

Manifest Range cũ và khóa giọng cũ có dấu `:` không đủ thông tin để xác minh an toàn nên có thể phải tải/tổng hợp lại. Không xóa cache; khóa cũ không nhập nhằng vẫn dùng được. Khởi động lại backend khi hết tác vụ để áp dụng mã mới.

Đây là đợt sửa tính toàn vẹn với lỗi đã chứng minh, chưa phải chứng nhận toàn bộ dự án không còn lỗi hay mọi file đã được rà sâu. Không có benchmark mới để khẳng định tăng tốc CPU/GPU hoặc nghiệm thu video dài. Bộ kiểm thử toàn dự án được chạy offline, cách ly trên D:; báo cáo máy đọc được tại `storage/maintenance/review_20261026_tests.xml` sau khi hoàn tất.
