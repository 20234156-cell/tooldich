# Thiết kế máy chủ nhỏ cho 1–2 máy khách

Ngày khảo sát: 24/10/2026. Trạng thái: đã triển khai chế độ server tùy chọn trong code và kiểm thử HTTP/hai phiên Chromium độc lập. Chưa chứng nhận kết nối hai máy vật lý qua LAN; chưa thay instance đang chạy. Hướng dẫn vận hành nằm trong README.md.

Cập nhật 25/10/2026: sau 571 test đạt và người dùng cho phép, đã khởi động lại instance thật bằng bản vá launcher/backup/đăng xuất. Mở BAT lần hai được xác minh trả thành công và mở UI hiện có; backup thật 27.261 tệp đã verified, đăng xuất thật200 và truy cập sau logout401. Những chỗ ghi chưa thay instance bên dưới là lịch sử khảo sát 24/10. LAN từ máy vật lý khác vẫn chưa nghiệm thu; chưa đổi profile/firewall.

## Kết quả triển khai bước đầu

Đã có tài khoản admin/operator/viewer, cookie phiên và CSRF, thu hồi phiên, nhật ký thao tác, quyền sửa theo phiên/tab và revision, giới hạn một công đoạn local nặng, nhật ký tác vụ SQLite và phục hồi trạng thái khi restart. Upload video từ máy khách được kiểm tra bằng FFprobe; lựa chọn giọng được lưu riêng theo tài khoản/dự án. Quản lý tập trung nằm ở nút **Quản lý máy chủ**.

Launcher một instance không giết tiến trình chiếm cổng; có supervisor, log, giữ máy thức và script đăng ký tự chạy khi đăng nhập Windows. Sao lưu SQLite bằng backup API cùng tệp ứng dụng, kiểm tra checksum và integrity; phục hồi offline sang thư mục mới. Sao lưu cùng ổ không bảo vệ khỏi hỏng ổ. Upload hiện chưa nối tiếp sau đứt mạng; tác vụ bị ngắt khi server restart cần người vận hành chạy tiếp, không tự phát lại lời gọi dịch vụ.

Bằng chứng audit thực: `storage/audits/small_server_20261024_final/report.json`. Đã nhập video H.264/AAC thật, phát Range có xác thực, chặn phiên chưa đăng nhập và người sửa thứ hai, mở cùng dự án trong hai browser context không lỗi JavaScript, giữ phiên và tác vụ đã xong qua restart của server thử riêng. Không thay dữ liệu dự án thật. Toàn bộ suite offline: 560 passed trong 291,85 giây. Các bảng khảo sát bên dưới ghi lại lý do thiết kế ban đầu, không phải danh sách tính năng còn thiếu.

SQLite WAL được giữ trên máy chủ theo [tài liệu SQLite](https://www.sqlite.org/wal.html): các tiến trình truy cập database phải cùng host; không chia sẻ tệp database qua SMB/NAS cho máy khách.

## Quyết định kiến trúc

Giữ Windows và ứng dụng hiện tại trên máy chủ. Một tiến trình FastAPI/Uvicorn phục vụ cả giao diện và API cùng địa chỉ; các máy khách dùng trình duyệt. Database SQLite WAL, video, model và cache nằm trên ổ cục bộ máy chủ. Một bộ điều phối tác vụ nặng, công suất ban đầu một công đoạn OCR/TTS/render tại một thời điểm. Người dùng vẫn được xem và chỉnh các dự án khác khi tác vụ nặng chạy.

Không cần chuyển framework, chuyển hệ điều hành, thêm Redis/Celery, PostgreSQL, Docker hay chia worker sang máy khác ở bước đầu. Những thành phần này chỉ được cân nhắc khi số đo hoặc nhu cầu vượt mô hình một máy chủ. Thiết kế task có dữ liệu độc lập với hàm Python để sau này tách worker mà giữ API/UI.

## Khả năng tương thích đã kiểm tra trong code

| Thành phần | Hiện trạng | Hành động tối thiểu |
|---|---|---|
| Giao diện/API | Phần lớn dùng URL tương đối, cùng origin | Giữ nguyên; sửa thông báo lỗi ghi cứng localhost |
| Video/ảnh/âm thanh | Được cấp qua API HTTP thay vì chia sẻ thư mục | Dùng lại; kiểm tra đăng nhập cả video Range, ảnh và audio |
| Bind mạng | main.py/start_server.bat nhận host/port | Thêm chế độ server cấu hình rõ ràng; chưa chỉ mở0.0.0.0 |
| TrustedHost | Mặc định chỉ127.0.0.1/localhost/testserver | Cấu hình hostname/IP LAN thực tế; không mặc định wildcard |
| Đăng nhập | API_TOKEN nhận header/query; JS không tự gửi token và thẻ video/img không thêm header riêng | Bổ sung phiên cookie cho trình duyệt, bảo vệ thao tác ghi; giữ token cho API client cũ |
| Kiểm tra bind | Guard hiện dò chuỗi argv0.0.0.0 | Kiểm tra cấu hình bind thực tế cho mọi địa chỉ ngoài loopback, bao gồm IPv6 |
| Database | SQLite WAL, session trên server | Giữ tại server; không cho client mở app.db qua SMB/NAS |
| Tác vụ | ThreadPoolExecutor2, task/by_project trong RAM | Bổ sung nhật ký task trong SQLite và bộ giới hạn tài nguyên; giữ executor hiện tại |
| Một dự án đang chạy | Queue ngăn submit task thứ hai cùng project | Giữ; bổ sung chống xung đột chỉnh sửa thủ công giữa hai trình duyệt |
| Câu phụ đề | DB là nguồn chuẩn; JSON/SRT là artifact | Giữ nguyên hợp đồng, không đưa JSON thành nguồn trạng thái thay thế |
| Pipeline state | Tệp JSON nguyên tử, đồng bộ Project.status vào DB | Chưa chuyển toàn bộ state sang DB; cần khóa chuyển trạng thái và phục hồi nhất quán |
| Khởi động lại | Startup đánh dấu job chưa xong FAILED/INTERRUPTED | Phục hồi cả task/stage; chưa tự chạy lại thao tác không xác định |
| Giọng đã chọn | localStorage theo trình duyệt | Khi có người dùng, lưu lựa chọn theo user/project ở server; không dùng một mặc định toàn cục ghi đè người khác |
| Nhập video local | Đường dẫn được giải nghĩa trên server, chưa có endpoint upload | Bước đầu dùng URL; chỉ admin nhập file server. Upload từ máy khách là phần bổ sung riêng |
| Drive OAuth | Có cơ chế browser/callback riêng | Kiểm tra callback từ máy khách trước khi công bố hỗ trợ; không suy luận từ localhost thành tương thích LAN |
| Khởi động bằng BAT | Có hỏi giải phóng cổng rồi taskkill/F | Chế độ server phải báo cổng bận và dừng; không tự giết tiến trình đang chiếm cổng |

## Các hợp đồng cần chốt trước khi triển khai

### 1. Đăng nhập và quyền

Bước đầu: một workspace dùng chung cho tối đa hai người tin cậy, có tài khoản riêng để nhận diện người sửa; chưa có tenant riêng hay phân chia kho video. Admin quản lý khóa dịch, cookie nhà cung cấp, xóa dữ liệu và cấu hình máy chủ. Người vận hành xử lý/chỉnh sửa video trong workspace.

Phiên đăng nhập được server kiểm tra, cookie HttpOnly/SameSite, có hạn dùng và thu hồi khi logout; Secure khi triển khai HTTPS. Yêu cầu ghi cần CSRF/origin validation. Cookie cùng origin cho phép các yêu cầu fetch, img, audio, video Range cùng hoạt động. Không đưa token bí mật vào URL video, lịch sử trình duyệt hay link chia sẻ. HTTPS/truy cập từ xa cần thiết kế trước khi mở ra ngoài LAN; giai đoạn đầu không forward cổng ra Internet.

Không bỏ kiểm tra API_TOKEN, allowlist nguồn hoặc quyền đọc file local để làm cho LAN hoạt động. Chỉ đổi bind sau khi toàn bộ đường gọi từ trình duyệt được kiểm thử với đăng nhập.

### 2. Tác vụ bền vững, giữ executor hiện tại

Thêm bản ghi task: task_id, project_id, action, options hợp lệ dạng JSON, input identity, trạng thái, người gửi, timestamps/heartbeat và cancel_requested. Không lưu pickle, tên hàm tùy ý hay khóa API/cookie trong options. Action được ánh xạ bằng danh sách cố định tới các hàm pipeline hiện có.

Luồng: ghi QUEUED thành công -> executor nhận task -> đánh dấu RUNNING -> chạy -> ghi trạng thái cuối. Chặn submit trùng/cùng dự án trong một giao dịch/khóa ngắn. UI đọc task từ DB để reload hoặc chuyển máy không mất thông tin. Chưa cần một process worker riêng, vì như vậy sẽ cần sửa thêm truyền progress/cancel/subprocess ownership.

Sau restart, RUNNING còn treo thành INTERRUPTED; hiện rõ bước cần tiếp tục. Xác minh checkpoint theo nguồn/settings/version và file thật trước khi bỏ qua công việc. Không tự coi tệp preallocate, ảnh quét dở hay audio chưa ghép là hoàn tất. Ban đầu người dùng bấm tiếp tục; tự tiếp tục chỉ triển khai khi từng action đã có hợp đồng an toàn cho chạy lại.

### 3. Chống hai người ghi đè

Dùng revision cho dữ liệu có thể sửa của từng dự án và khóa ngắn quanh kiểm tra revision + ghi + invalidation. Client gửi revision đã đọc; khác revision trả409 kèm hướng dẫn tải bản mới, giữ nội dung đang sửa cho người dùng đối chiếu. Có thể đặt revision ở bảng phụ mới để hạn chế sửa bảng Project cũ, nhưng phải có migration/schema version rõ ràng.

Áp dụng cùng hợp đồng cho sửa/xóa/tách/gộp câu, ROI, bản dịch và thiết lập tác động pipeline. Queue vẫn chặn sửa khi task cùng dự án đang chạy. Không giữ transaction SQL trong suốt FFmpeg/OCR/TTS; không khóa toàn bộ workspace khi một người mở dự án.

### 4. Tài nguyên máy8GB

Giữ một API process vì queue, khóa và model provider hiện ở RAM một process. Nhiều API worker làm mỗi process có queue/lock riêng, có thể chạy trùng cùng dự án và tăng số bản model trong RAM.

Một slot dùng chung cho công đoạn local nặng là cấu hình ban đầu thận trọng, cần đo để điều chỉnh. Chờ slot phải hủy được và có tiến độ rõ ràng. Không áp một khóa dài lên API đọc/video player. Download/dịch mạng có thể song song trong giới hạn hiện có nếu RAM/đĩa còn đủ; không mặc định cho hai OCR hoặc VieNeu cùng chạy trên máy8GB.

Không thay model/độ phân giải OCR/timing/QA để đạt benchmark. Lỗi Edge hoặc quota Gemini là giới hạn dịch vụ độc lập; tăng máy khách không tự khắc phục.

### 5. Tệp video và vận hành

Bước đầu dùng link video trên máy khách, giữ media/cache server-side. Đường dẫn C:\\... nhập từ client không được hiểu là file client. Nếu cần upload: upload có giới hạn dung lượng, ghi theo luồng vào file tạm, xác minh bằng ffprobe/checksum rồi công bố nguyên tử; xử lý mất mạng/retry và phần upload dở. MAX_UPLOAD_SIZE_BYTES hiện chỉ là cấu hình, chưa phải upload endpoint.

Dùng một bản cài đặt ổn định ngoài worktree phát triển. Chuyển vị trí phải kiểm tra các đường dẫn tuyệt đối trong DB/manifest/model/giọng; không chép thư mục rồi giả định mọi cache còn đúng. Migration và sao lưu cần thử khôi phục trước khi đổi máy/đổi thư mục.

Process supervisor trên Windows chỉ chạy một instance, khởi động lại khi lỗi và không cài thư viện trong mỗi lần chạy. Không dùng autoreload ở server. Máy chủ không sleep khi đang phục vụ; tắt máy/mất điện vẫn làm ngừng xử lý. Sao lưu SQLite qua cơ chế backup nhất quán và dữ liệu project tương ứng, không copy riêng app.db đang WAL rồi coi là bản sao hoàn chỉnh.

## Lộ trình ít sửa lại

1. Chốt cấu hình server, tách launcher vận hành an toàn, session đăng nhập, quyền quản trị và hostname. Dùng URL làm nguồn. Kiểm thử API/media đã xác thực bằng hai phiên trình duyệt riêng. Chỉ lúc đạt mới mở LAN.
2. Nhật ký task bền vững + giới hạn công đoạn nặng + revision chống ghi đè. Phục hồi sau restart và đối chiếu cache trên video ngắn thật. Bước1 và2 là điều kiện để dùng thường xuyên bằng hai máy.
3. Nếu cần: upload từ client, lưu preference theo user/project, kiểm chứng OAuth từ xa, supervisor/sao lưu và chạy thử dài hạn trên hai máy thật.
4. Chỉ khi số đo cho thấy cần: thêm RAM/GPU hoặc máy worker; sau đó cân nhắc DB client/server và hàng đợi phân tán. Giữ task action/options và media identity độc lập với nơi chạy để tránh viết lại UI.

## Tiêu chí nghiệm thu

- Hai phiên độc lập mở/duyệt hai dự án, stream video và nghe audio được khi có task nặng.
- Một phiên logout/hết hạn làm media/API từ chối truy cập phù hợp; không mất nội dung sửa khi phiên hết hạn.
- Hai phiên sửa cùng câu: phát hiện409, không âm thầm ghi đè. Dự án đang chạy không nhận mutation không hợp lệ.
- Hai người gửi tác vụ: không trùng cùng project; công đoạn nặng không vượt slot; hủy lúc chờ/running đúng task.
- Restart lúc QUEUED/RUNNING: task và pipeline không treo nhãn RUNNING; chỉ cache/manifest hoàn tất, đúng identity được dùng tiếp.
- So sánh một bộ video ngắn/dài trước/sau: giữ cue IDs/timing/nội dung, có file giọng thật, không trộn voice, final MP4 qua cùng QA. Không chứng nhận từ nhãn/path/mocks.
- Sao lưu/khôi phục thử được DB cùng dữ liệu; khởi động launcher lần hai không giết instance đang chạy.
- Đo RAM, CPU/GPU, thời gian OCR/TTS/render, thời gian đáp ứng và lỗi từ hai máy thật. Chưa có số đo này tại thời điểm thiết kế.

## Tài liệu chính thức đã đối chiếu

- FastAPI deployments / process memory: https://fastapi.tiangolo.com/deployment/concepts/
- SQLite qua mạng: https://www.sqlite.org/useovernet.html
- Starlette sessions/cookie middleware: https://www.starlette.io/middleware/
- Starlette upload streaming/request: https://www.starlette.io/requests/

## Mở rộng sau hai máy khách

| Khi gặp giới hạn | Hướng xử lý | Phần giữ lại |
|---|---|---|
| RAM thiếu/model phải nạp lại, tác vụ nặng chờ lâu | Đo peak RAM/CPU/GPU và thời gian từng công đoạn, nâng RAM/SSD/GPU theo nút thắt thực tế | UI, API, project và hợp đồng QA |
| Cần thêm máy cùng xử lý OCR/TTS/render | Tách worker và hàng đợi bền vững, bổ sung claim/heartbeat/input identity và kho media chung qua dịch vụ | Tài khoản, task ID/action/options và tiến độ trên UI |
| Cần nhiều API process hoặc nhiều người ghi liên tục | Chuyển metadata sang PostgreSQL và khóa/lease dùng chung trước khi nhân API process | API và dữ liệu cue chuẩn |
| Video upload lớn/mạng không ổn định | Upload chia phần có resume và checksum từng phần, quota theo tài khoản | Quy trình kiểm định nguồn và nhập dự án |
| Cần vào ngoài LAN | VPN hoặc HTTPS qua reverse proxy, bật secure cookie, kiểm thử lại origin/host/media | Đăng nhập và phân quyền hiện tại |
| Cần kho dự án riêng theo người/nhóm | Thêm ownership và authorization tới mọi project/media/task/backup endpoint trước khi nhận người ngoài | Pipeline xử lý video |

Không tăng số tác vụ model chỉ vì tăng máy khách. Theo dõi thời gian chờ hàng đợi, thời gian từng bước, RAM/đĩa trống và lỗi nhà cung cấp; dùng số đo này để quyết định đầu tư. Chưa có cam kết tăng tốc xử lý video từ việc thêm trình duyệt.

Trạng thái vận hành 24/10/2026: đã chuẩn bị tài khoản admin trong tệp riêng và đăng ký Scheduled Task tự chạy khi tài khoản Windows hiện tại đăng nhập. Chưa đổi .env/bind/firewall của instance đang chạy; chưa mở LAN. Ethernet đang ở Public và phiên PowerShell hiện tại không có quyền quản trị, nên quy tắc Private LAN cần được chủ máy áp dụng trong PowerShell quản trị sau khi xác định mạng tin cậy. Không đổi phân loại mạng tự động. Tác vụ tự chạy tham chiếu đường dẫn worktree hiện tại; nếu chuyển bản cài phải đăng ký lại. Bộ kiểm thử và audit hiện tại không thay thế nghiệm thu hai máy vật lý.
