# DuanLayLink — Làm phụ đề và lồng tiếng Việt cho video

Tài liệu này dành cho người sử dụng, không cần biết lập trình. Đây là hướng dẫn chính của dự án; bạn không cần đọc mã nguồn hoặc các báo cáo thử nghiệm cũ để vận hành.

## 1. Hệ thống này dùng để làm gì?

DuanLayLink giúp biến video tiếng Trung thành video có phụ đề và giọng đọc tiếng Việt. Bạn có thể nhập đường dẫn Bilibili, Douyin, TikTok hoặc một video có sẵn trên máy. Khả năng tải bằng đường dẫn còn phụ thuộc quyền truy cập và dịch vụ nguồn.

Ứng dụng nhận chữ Trung trên hình hoặc nghe lời thoại, dịch sang tiếng Việt, tạo giọng đọc, che phụ đề gốc và xuất video mới. Bạn có thể giữ tiếng gốc, thêm giọng Việt trên nền âm thanh cũ hoặc chỉ dùng giọng Việt.

Bạn quyết định bản dịch, kiểu phụ đề và giọng đọc. Các bước nhận dạng, dịch và đọc bằng máy vẫn cần kiểm tra; video xuất thành công chưa có nghĩa mọi câu đều đúng nghĩa hoặc nghe tự nhiên.

Với Douyin, có thể dán link video `/video/...`, link rút gọn `v.douyin.com/...` hoặc link đang mở video dạng `/jingxuan?modal_id=...` và `/discover?modal_id=...`. Ứng dụng tự chuyển hai dạng có `modal_id` sang link video chuẩn; khả năng tải vẫn phụ thuộc quyền truy cập nguồn.

Nếu Douyin yêu cầu cookie phiên mới, có thể cấu hình `DOUYIN_COOKIE_FILE` trong `.env` bằng đường dẫn đến tệp cookie dạng Netscape do bạn xuất từ phiên trình duyệt của mình, rồi khởi động lại máy chủ. Khi API trực tiếp từ chối, ứng dụng thử mở trang video bằng Chromium với phiên cookie này và chỉ nhận dữ liệu đúng ID video. Máy mới cần chạy `python -m playwright install chromium` sau khi cài thư viện. Không tự giải CAPTCHA; nếu trang vẫn yêu cầu xác minh, hãy xử lý trong trình duyệt của bạn. Tệp cookie là thông tin riêng, không gửi nội dung vào chat hay đưa lên Git. Khi không có quyền truy cập nguồn, bạn có thể tải video bằng trình duyệt rồi nhập tệp local. Bilibili HTTP 412 là lỗi nguồn từ chối yêu cầu lấy luồng video; kiểm tra phiên đăng nhập/cookie và thử lại sau. Nhận được link hoặc đọc được tiêu đề chưa có nghĩa đã tải video thành công.

Nếu Douyin đọc được tiêu đề nhưng bản cũ báo “không thể kết nối đến luồng tải sau 30 lần thử”, hãy cập nhật và khởi động lại máy chủ: đã bổ sung CDN `zjcdn.com` và giữ nguyên nguyên nhân khi URL bị chặn. Lỗi chặn URL không được giải quyết bằng cách bấm tải lặp lại nhiều lần.

Phiên Chromium dùng cho Douyin được giữ trong `storage/private/douyin_browser_profile`, độc lập với trình duyệt cá nhân của bạn. Ứng dụng chỉ nạp cookie khi bạn cung cấp một bản xuất mới; các lần đọc sau giữ lại trạng thái phiên mà Douyin đã cập nhật. Không xóa thư mục này để dọn rác khi còn đang dùng Douyin. Có thể đổi vị trí bằng `DOUYIN_BROWSER_PROFILE` trong `.env`.

**Ví dụ:** bạn có video 10 phút với chữ Trung ở cạnh dưới. Bạn khoanh vùng chữ, cho hệ thống nhận dạng, sửa câu nhận sai, duyệt bản dịch, chọn mẫu chữ và giọng Việt, rồi xuất MP4. Video nguồn vẫn được giữ để chỉnh lại sau.

## 2. Mở ứng dụng

### Vận hành trên máy RAM 8 GB

Máy i3-10100F / GTX 1050 Ti 4 GB hiện chỉ có một thanh RAM 8 GB. Giữ một công đoạn video nặng tại một thời điểm. Trước OCR/TTS/render, xem **Quản lý máy chủ → Ổ đĩa & tài nguyên**, đóng các ứng dụng không cần sau khi lưu công việc; RAM khả dụng dưới 2 GB vẫn là cảnh báo, không phải trạng thái đủ tải.

Ngày 26/10/2026, nhật ký Windows ghi nhiều sự kiện 2004 thiếu bộ nhớ ảo; Pyrefly của Antigravity giữ khoảng 2 GB bộ nhớ riêng. Cấu hình người dùng Antigravity đã đặt `pyrefly.lspArguments` thành `["lsp", "--indexing-mode", "none", "--threads", "2"]`. Cấu hình này hạn chế tìm tham chiếu toàn dự án; xóa khóa để trở về mặc định khi đã nâng RAM. Không dừng backend hoặc xóa cache dự án để giải phóng RAM.

Bộ nhớ ảo hiện vẫn cấu hình thủ công; lần thử bật Windows tự quản lý bị từ chối quyền quản trị, chưa áp dụng. Khi có quyền admin, có thể chọn **System Properties → Advanced → Performance Settings → Advanced → Virtual memory → Automatically manage**; Windows có thể yêu cầu khởi động lại, chỉ thực hiện sau khi lưu công việc và dừng tác vụ. Lưu Drive giải phóng đĩa khi đã dọn đúng quy trình, không thay thế RAM. Nâng RAM lên 16 GB là mức đầu tư đầu tiên; nếu thường mở IDE/AI cùng server, ưu tiên 32 GB sau khi đối chiếu tương thích bo mạch.

Nếu máy đã được cài và đang dùng dự án này:

1. Mở thư mục dự án, chạy **`start_server.bat`**.
2. Đợi cửa sổ máy chủ khởi động. Trình duyệt sẽ mở; nếu chưa mở, truy cập **http://127.0.0.1:8088/**.
3. Giữ cửa sổ máy chủ mở trong lúc xử lý. Đóng cửa sổ này sẽ ngắt ứng dụng.
4. Chọn dự án cũ để tiếp tục hoặc tạo dự án mới.

`127.0.0.1` là máy của bạn. Giao diện mở trong trình duyệt nhưng các bước xử lý chạy ở ứng dụng trên máy.

Tải lại trang sẽ mở lại dự án/trang đang xem trong chính tab trình duyệt đó. Nếu bạn chủ động về Tổng quan rồi tải lại, ứng dụng giữ trang Tổng quan. Dự án đã xóa sẽ không được mở lại; lỗi kết nối tạm thời hiển thị thông báo để bạn thử tải lại. Vị trí được lưu trong phiên tab, không chứa nội dung phụ đề hay thông tin đăng nhập.

Nếu báo cổng 8088 đang được dùng, kiểm tra có cửa sổ ứng dụng khác đang mở hay không. Có thể dùng cổng khác bằng lệnh `start_server.bat 127.0.0.1 9000`, rồi mở `http://127.0.0.1:9000/`.

Sau khi cập nhật mã nguồn, dừng ứng dụng khi không có tác vụ đang chạy rồi mở lại `start_server.bat`. Nhấn **Ctrl+F5** trong trình duyệt để nạp giao diện mới. Chỉ tải lại trang không nạp lại phần xử lý Python đang chạy.

### Quản trị máy chủ

Mở **Quản lý máy chủ** bằng tài khoản admin. Chọn từng mục ở thanh điều hướng; có thể dùng phím mũi tên để chuyển mục. Nút **Làm mới** lấy lại số liệu và giữ mục đang xem.

- **Tổng quan:** CPU, RAM, tác vụ đang chạy/chờ, lần sao lưu hoàn tất gần nhất và cảnh báo có nút chuyển tới mục xử lý. Số liệu đo tại thời điểm tải, không tự cập nhật liên tục.
- **Ổ đĩa & tài nguyên:** dung lượng trống/tổng của các ổ ứng dụng, dữ liệu và kho sao lưu đã cấu hình; GPU/VRAM nếu đọc được NVIDIA. Không đo được phải hiển thị thiếu số liệu. Khác ký tự ổ chưa có nghĩa khác thiết bị vật lý.
- **Tác vụ:** tìm theo dự án/người gửi/công đoạn và lọc trạng thái trong tối đa 50 tác vụ gần nhất; xem lỗi, mở dự án, tạm dừng an toàn hoặc hủy tác vụ có quyền. Tiến độ lưu gần nhất không khẳng định worker bị đứng. Tiếp tục công đoạn từ trang dự án sau khi kiểm tra điều kiện phục hồi.
- **Tài khoản / Chia sẻ / Phiên & quyền sửa:** cấp quyền, mời người dùng, chỉnh tài khoản và thu hồi phiên. Chi tiết trình duyệt nằm trong phần mở rộng. Thay đổi tài khoản được ghi vào nhật ký cùng người thực hiện, không ghi mật khẩu.
- **Sao lưu / Dọn dữ liệu / Nhật ký:** giữ các bước xem trước và kiểm chứng hiện có. Cảnh báo sao lưu dùng thời điểm bản hoàn tất; mức kiểm chứng và bản sao độc lập xem trong mục Sao lưu. Bản hoàn tất không đồng nghĩa đã phục hồi thử.

Số liệu hạ tầng chi tiết chỉ dành cho admin. RAM trống dưới 2 GB hoặc ổ cấu hình còn dưới mức lớn hơn giữa 10 GiB và ngưỡng bắt đầu tác vụ sẽ có cảnh báo. Máy chủ hiện giới hạn một công đoạn nặng; giao diện không tự thay đổi hạn mức, xóa tệp hay gửi yêu cầu dịch vụ AI.

### Nếu cài trên máy mới

Cần Python 3.12, các thư viện trong `requirements.txt`, FFmpeg và FFprobe. VideoSubFinder hỗ trợ tìm chữ; các chức năng AI cục bộ cần thêm công cụ và model tương ứng. Những gói này có thể chiếm nhiều dung lượng vì là thành phần vận hành.

Trong thư mục dự án, người cài đặt có thể chạy:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
.\start_server.bat
```

Chỉ sao chép `.env.example` khi **chưa có** `.env`, tránh ghi đè cấu hình đang dùng. Nếu FFmpeg/FFprobe không nằm trong PATH, điền đường dẫn vào `FFMPEG_PATH` và `FFPROBE_PATH` trong `.env`. File này chứa cấu hình riêng, không gửi công khai.

Model, giọng đã đăng ký và thông tin đăng nhập không tự xuất hiện trên máy mới chỉ bằng việc chép mã nguồn.

### Dùng máy này làm server cho 1–2 máy khác

**Chia sẻ và đăng nhập:** mở **Quản lý máy chủ → Chia sẻ truy cập**, chọn địa chỉ LAN rồi bấm **Sao chép link đăng nhập**. Người nhận phải dùng cùng mạng và đăng nhập bằng tài khoản đã cấp. Không gửi địa chỉ `127.0.0.1` cho máy khác vì đó là chính máy của người mở link.

Muốn người nhận tự đặt mật khẩu, nhập tên tài khoản mới và chọn quyền **Người dùng** hoặc **Người xem**, rồi bấm **Tạo link mời 24 giờ**. Sao chép link hiện ra và gửi riêng cho người nhận. Link chỉ dùng một lần; tạo lại lời mời cùng tên sẽ hủy link trước đó. Có thể thu hồi lời mời chưa sử dụng ngay trong mục này. Người nhận đặt và xác nhận mật khẩu tối thiểu 12 ký tự; kích hoạt thành công sẽ đăng nhập luôn. Tài khoản đã có vẫn dùng link đăng nhập thường; admin quản lý mật khẩu/quyền trong mục Tài khoản.

Màn hình đăng nhập có nhãn máy chủ và nút hiện/ẩn mật khẩu. Nếu quên mật khẩu, liên hệ admin. Link mời không cấp quyền quản trị; muốn bổ sung admin, quản trị viên hiện tại phải chủ động tạo/cập nhật tài khoản trong mục Tài khoản.

Địa chỉ từ xa cố định được cấu hình bằng `SERVER_PUBLIC_URL=https://studio.ten-mien-cua-ban/` trong `.env`, sau khi đã tạo đường hầm/reverse proxy HTTPS trỏ tới `http://127.0.0.1:8088`. Khởi động lại server để nạp hostname chính xác vào allowlist và ưu tiên địa chỉ này trong phần chia sẻ. Không dùng wildcard `*`. Cookie cấp qua HTTPS tự được đánh dấu Secure; các yêu cầu ghi vẫn kiểm tra Origin và CSRF. Chỉ điền URL không tự tạo đường hầm hoặc DNS.

Máy chủ cần bật khi làm việc. Link LAN phụ thuộc mạng và Windows Firewall; nút sao chép không xác nhận kết nối từ máy khác. Để dùng khác mạng cần tên miền HTTPS, đường hầm/reverse proxy và cấu hình hostname được phép tương ứng; tính năng mời không tự mở cổng Internet.


**Mở BAT lần nữa:** launcher kiểm tra tiến trình đúng thư mục dự án và API phiên đăng nhập. Nếu server đã sẵn sàng, BAT mở giao diện đang chạy rồi thoát thành công; không mở backend thứ hai. Nếu giữ khóa nhưng backend chưa sẵn sàng, thông báo yêu cầu đợi/đọc log. Không xóa `small_server.lock` hay kết thúc Python chỉ dựa vào tên tiến trình. Tệp khóa tồn tại không có nghĩa khóa hệ điều hành vẫn đang bị giữ.

Kiểm tra chỉ đọc, không khởi động/dừng server:

```powershell
python scripts/run_small_server.py --status
```

`ready` nghĩa tiến trình đúng workspace và API đang trả lời; chưa chứng minh mạng máy khách, provider hay toàn bộ video đã đạt chất lượng.

#### Quy trình vận hành và điều kiện qua từng bước

| Giai đoạn | Kiểm tra bắt buộc | Nếu chưa đạt |
|---|---|---|
| Trước khi mở | Đĩa còn đủ cho video, tệp trung gian và sao lưu; không chạy backend cũ song song; mạng đáng tin cậy | Dừng thao tác mới, xử lý dung lượng/cấu hình; không xóa nguồn dự án |
| Khởi động | Mở BAT, chạy `--status`, kiểm tra `ready` và backend PID; đọc log nếu không đạt | Không bấm liên tục hoặc xóa khóa; phân biệt server chưa sẵn sàng với cổng do ứng dụng khác giữ |
| Đăng nhập | Tài khoản riêng từng người; xem API/media phải yêu cầu đăng nhập; operator/viewer đúng quyền | Dừng mở LAN nếu có dữ liệu truy cập được khi chưa đăng nhập |
| Kết nối máy khách | Mở IP LAN do launcher in, đăng nhập, phát video và nghe âm thanh trên máy khách thật | Kiểm tra IP/DHCP, profile Private và firewall; không mở router ra Internet để sửa lỗi LAN |
| Nhập nguồn | Dùng link hoặc upload; kiểm định tệp thật, thời lượng, hình/âm thanh trước OCR | Sửa nguồn/download; đọc được tiêu đề chưa phải tải thành công |
| Xử lý và duyệt | Một phiên giữ quyền sửa; công đoạn nặng chờ slot; kiểm tra cue/crop, dịch, giọng nghe thử, render và QA | Hủy đúng tác vụ, chờ worker dừng rồi chạy lại phần ảnh hưởng; không hạ ngưỡng QA |
| Dừng/bảo trì | Chờ xong hoặc hủy và xác nhận terminal; có bản sao toàn vẹn trước thay đổi dữ liệu | Không ép tắt chỉ vì tiến độ đứng; đọc log/trạng thái công đoạn trước |
| Sao lưu/phục hồi | Xem bản sao đã hoàn tất, checksum/SQLite đạt; phục hồi thử sang thư mục mới | Thư mục không có manifest hoàn tất là bản dở, không dùng để phục hồi |
| Sau sự cố | Kiểm tra server, task INTERRUPTED, cache đúng identity rồi chạy tiếp; kiểm tra lại đầu ra | Không coi nhãn hoàn tất hoặc file tồn tại là video đã đạt |

Sao lưu lỗi được chờ tăng dần 5–60 phút trước khi thử lại, tránh liên tục chặn ứng dụng. Trong lúc sao lưu vẫn cho phép đăng xuất/thu hồi phiên. Không sao chép hồ sơ Chromium đang hoạt động: cache thay đổi/khóa/đường dẫn dài không tạo được snapshot nhất quán. Sau phục hồi có thể cần đăng nhập Douyin lại. Các thư mục bản sao dở từ phiên bản cũ không được tính là backup hợp lệ; không tự xóa khi chưa đối chiếu. Server chạy lâu dài đã tắt giới hạn Scheduled Task 72 giờ và không bị Task Scheduler dừng chỉ vì chuyển sang pin; mất điện/cạn pin vẫn dừng máy. Script nền chờ launcher và trả mã thoát để Task Scheduler theo dõi được.

Việc sao lưu gồm chép và đọc lại toàn bộ tệp để kiểm tra checksum, không chỉ lưu database. Bộ dữ liệu khoảng 4 GB với hơn 27.000 tệp có thể cần nhiều phút trên máy này. Chỉ chạy lúc rảnh; không dừng server chỉ vì chưa có manifest. Chỉ xem là đã đạt khi API trả `verified: true` và bản sao xuất hiện trong danh sách hoàn tất.

Chế độ cũ vẫn chạy bằng `start_server.bat`. Chế độ quản lý nhiều máy dùng **`start_small_server.bat`**; cần dừng instance cũ trước để tránh hai backend dùng chung dữ liệu. Launcher mới không giết tiến trình chiếm cổng. Dữ liệu video/model/giọng vẫn nằm trên server; máy khách dùng trình duyệt cùng mạng riêng. Đây là **một workspace chung**, chưa chia kho video riêng theo khách hàng.

1. Chạy `python scripts/prepare_small_server.py` trên máy chủ. Nếu chưa có admin, công cụ tạo tài khoản và lưu mật khẩu ngẫu nhiên vào tệp riêng trong `storage/private/server_login_*.txt`, giới hạn quyền đọc theo tài khoản hệ điều hành. Không in mật khẩu vào log/chat. Nếu đã có admin thì giữ nguyên, không tự đặt lại mật khẩu.
2. Khi hết tác vụ, dừng máy chủ cũ, mở `start_small_server.bat`. Mở địa chỉ được in trong cửa sổ và đăng nhập bằng tệp riêng trên. Nếu chưa chuẩn bị admin, launcher chỉ mở localhost để tạo admin đầu tiên; sau đó cần mở lại launcher để dùng LAN.
3. Máy khách mở địa chỉ LAN của server, ví dụ `http://192.168.1.20:8088/`. Đặt DHCP reservation trong router để giữ địa chỉ ổn định. Chỉ cho phép qua Windows Firewall trên mạng Private/LocalSubnet; không forward cổng ra Internet. Nếu chưa cấu hình firewall, cài bằng lệnh bên dưới trong PowerShell quản trị.
4. Bấm **Quản lý máy chủ** ở thanh bên để tạo tài khoản cho máy/người thứ hai. Nên dùng hai tài khoản riêng thay vì chia sẻ mật khẩu admin. Tác vụ không phụ thuộc việc tab trình duyệt có mở hay không; nhưng máy chủ tắt/mất điện thì xử lý vẫn dừng.

| Quyền | Công việc |
| --- | --- |
| `admin` | Quản lý tài khoản, phiên truy cập, quyền sửa, khóa API, kết nối Drive, sao lưu, xóa dự án và xử lý video. |
| `operator` | Xem workspace, tải video lên, tạo/chỉnh/xử lý dự án, hủy tác vụ của mình; không quản lý khóa hay tài khoản. |
| `viewer` | Xem video, kết quả và tiến độ; không ghi nội dung dự án. |

Trang quản lý hiển thị phiên/IP truy cập gần đây, người đang giữ quyền sửa, người gửi tác vụ, tiến độ, RAM/đĩa trống và nhật ký thao tác. Admin có thể đăng xuất phiên khác và giải phóng quyền sửa bị treo. Quyền sửa thuộc một tab, tự hết hạn sau 60 giây không gia hạn; các phiên khác vẫn đọc được. Khi bị xung đột phiên bản, ứng dụng từ chối ghi và yêu cầu tải lại, không tự ghi đè bản mới. Chỉ một công đoạn nặng OCR/TTS/render dùng tài nguyên tại một thời điểm; tác vụ chờ có thể hủy.

**Nhập video từ máy khách:** dùng nút **Tải video từ máy này lên server** ở trang Dự án mới, giới hạn mặc định 500 MB, chỉnh bằng `MAX_UPLOAD_SIZE_BYTES`. Giữ trang mở khi đang upload. Server kiểm tra video và checksum trước khi tạo dự án; upload lỗi/dở được dọn. Đây là upload lại từ đầu nếu mất mạng, chưa có giao thức upload chia phần tiếp tục. Đường dẫn `C:\...` trên máy khách không phải đường dẫn trên server; chỉ admin có thể nhập file từ thư mục server được cho phép.

**Sau restart:** task và progress được lưu trong SQLite; tác vụ dở được đánh dấu `INTERRUPTED`, không treo mãi ở RUNNING và không tự giả định hoàn tất. Mở dự án, chạy tiếp công đoạn; chỉ cache/manifest hoàn tất đúng identity được dùng lại. Không tự gửi lại mọi job, vì một số công đoạn có thể gọi dịch vụ tính phí hoặc cần duyệt. Giọng/tốc độ/màu giọng được lưu trên server theo tài khoản/dự án trong chế độ này; đổi máy vẫn giữ lựa chọn sau đăng nhập.

**Sao lưu:** khi rảnh, server thử tạo bản sao đã kiểm tra sau mỗi 24 giờ (`SERVER_AUTO_BACKUP_HOURS=0` để tắt). Admin có thể bấm sao lưu/kiểm tra/xóa bản cũ trong trang quản lý; không được xóa bản sao hợp lệ cuối cùng. Tác vụ hoặc thao tác ghi đang chạy thì hoãn sao lưu. Trong lúc sao lưu, vẫn xem được dữ liệu nhưng không ghi mới. Bản sao gồm SQLite nhất quán, project, giọng, uploads, cache, từ điển và dữ liệu cấu hình/đăng nhập cần giữ; không sao chép lại model hay audit cũ. Bản sao chứa dữ liệu riêng và có thể chứa khóa cấu hình, không chia sẻ thư mục `backups` qua web hay đưa lên Git. Chưa tự đưa backup ra máy/ổ khác: bản sao trên cùng ổ không bảo vệ khi ổ đó hỏng. Kiểm tra đĩa trống và chuyển bản sao cần giữ sang ổ khác theo quy trình của bạn.

Phục hồi offline sang **thư mục chưa tồn tại**, giữ nguyên dữ liệu đang dùng:

```powershell
python scripts/server_backup.py verify "storage/backups/<ten-ban-sao>"
python scripts/server_backup.py restore "storage/backups/<ten-ban-sao>" --destination "D:/DuanLayLink-Restored"
```

Công cụ kiểm tra checksum/SQLite trước khi chép, điều chỉnh đường dẫn dữ liệu và bỏ phiên đăng nhập/quyền sửa cũ. Cấu hình kết nối thư mục phục hồi nằm trong `restore.env`; đối chiếu rồi áp dụng khi server đã dừng. Môi trường/model/công cụ vẫn cần cài; khóa mã hóa DPAPI và đăng nhập nguồn có thể cần làm lại khi đổi tài khoản Windows. Không coi restore là đã nghiệm thu video: vẫn kiểm tra file và QA khi mở dự án.

**Tự khởi động khi đăng nhập Windows:** chạy PowerShell dưới đúng tài khoản Windows dùng model/DPAPI:

```powershell
powershell -NoProfile -File scripts/install_server_startup.ps1
# Chạy PowerShell quản trị nếu cần tạo quy tắc firewall cho Python/cổng 8088:
powershell -NoProfile -File scripts/install_server_startup.ps1 -ConfigureFirewall
# Gỡ tự khởi động, không xóa dự án hoặc dừng tiến trình đang chạy:
powershell -NoProfile -File scripts/install_server_startup.ps1 -Remove
```

Supervisor chạy một instance, khởi động lại sau lỗi, ghi log tại `storage/maintenance/small_server.log`. Khởi động cùng lúc nhiều launcher hoặc cổng bận sẽ báo lỗi thay vì kết thúc server khác. Tác vụ tự chạy **sau khi người dùng đăng nhập Windows**, không phải trước màn hình đăng nhập. Launcher yêu cầu Windows không sleep trong lúc phục vụ và khôi phục yêu cầu đó khi dừng; có thể dùng `--allow-sleep` nếu muốn để Windows tự ngủ. Không tắt được hậu quả mất điện; máy chủ phải luôn bật để các máy khác làm việc. Không bật autoreload ở chế độ server.

Đăng nhập/kết nối OAuth Drive thực hiện trên trình duyệt máy chủ do callback Desktop OAuth dùng loopback. Máy khách đã đăng nhập ứng dụng vẫn có thể gửi thành phẩm lên Drive đã kết nối. Tài khoản/hạ tầng hiện là nhóm tin cậy trong LAN; cần HTTPS/VPN và rà soát triển khai trước khi dùng qua Internet. Không bật CORS tùy ý hay bỏ xác thực để làm cho video phát được.

Nếu quên mật khẩu admin, chủ máy có thể chạy `python scripts/prepare_small_server.py --reset-admin <ten-tai-khoan>` trên máy chủ; mật khẩu mới cũng chỉ nằm trong tệp riêng, các phiên cũ bị thu hồi. Không gửi nội dung tệp mật khẩu vào chat.

## 3. Quy trình từ video nguồn đến thành phẩm

Luồng sử dụng:

**Nhập video → tải/đọc nguồn → chọn vùng chữ → nhận dạng → duyệt nguồn → dịch và duyệt → chọn phụ đề → tạo giọng → xuất video → kiểm tra và tải về.**

Các bước trên giao diện được đánh số từ 1 đến 9. Bước nhận dạng và dịch có phần duyệt riêng.

Một vài từ trên giao diện: **ROI** là khung chọn vùng chữ; **OCR** là đọc chữ trên hình; **STT** là nghe để lấy lời thoại; **TTS** là tạo giọng từ văn bản; **render** là ghép và xuất video; **QA** là kiểm tra đầu ra. **Model** là bộ dữ liệu AI, còn **runtime** là công cụ chạy model đó.

| Bước | Bạn làm gì? | Hệ thống tạo ra gì? |
| --- | --- | --- |
| 1. Tạo dự án | Nhập URL hoặc đường dẫn video trên máy; chọn đoạn cắt nếu cần. | Một dự án riêng để quản lý video và tiến độ. |
| 2. Tải/nhập video | Bấm tải/nhập và đợi kiểm tra nguồn. | Video nguồn dùng cho các bước sau. Tạo dự án chưa đồng nghĩa đã tải xong. |
| 3. Chọn vùng chữ | Kéo khung bao quanh phụ đề Trung trên video. | Vị trí vùng cần đọc và che chữ. Nên xem vài thời điểm khác nhau. |
| 4. Nhận dạng | Chọn cách lấy lời thoại, chạy thử nếu cần, sửa và duyệt các câu nguồn. | Danh sách câu kèm thời gian bắt đầu/kết thúc. |
| 5. Dịch Việt | Chọn dịch vụ, chạy dịch, đọc lại và sửa, sau đó duyệt bản dịch. | Bản dịch gắn đúng với từng câu nguồn. |
| 6. Che chữ và đặt sub | Chọn mẫu chữ, điều chỉnh nền che, xem thử rồi lưu. | Cấu hình phụ đề dùng cho cả xem thử và video xuất. |
| 7. Tạo giọng Việt | Chọn giọng, nghe thử, chọn tốc độ/màu giọng rồi tạo. | Âm thanh từng câu và bản giọng ghép theo thời gian video. |
| 8. Xuất video | Chọn chế độ âm thanh và chất lượng, bấm render. | Video có phụ đề và âm thanh theo cấu hình đã lưu. |
| 9. Kiểm tra và tải | Xem báo cáo kỹ thuật, nghe/xem kết quả, tải video. | Thành phẩm và các file phụ đề/báo cáo được giao diện cung cấp. |

Lần đầu nên thử một đoạn ngắn trước khi chạy video dài. Sau khi đã quen và có cấu hình phù hợp, bạn có thể dùng chức năng chạy các bước còn lại. Chạy tự động không thay thế việc kiểm tra nội dung.

### Chọn cách nhận dạng ở bước 4

- **Video có phụ đề Trung trên hình:** chọn nhận dạng chữ, thường được gọi là Hardsub/OCR. Khung chọn chữ ở bước 3 cần đúng vị trí.
- **Video không có phụ đề:** chọn nghe lời thoại, thường được gọi là STT/Whisper. Không cần vẽ khung chữ để nghe lời thoại.
- **Tự động:** hệ thống kiểm tra một đoạn mẫu rồi chọn cách xử lý. Nếu mẫu rơi vào đoạn mở đầu không có thoại/chữ, bạn nên chọn lại cách phù hợp.

Chữ cách điệu, nền quá sáng, phụ đề chạy vị trí hoặc tiếng nói khó nghe đều có thể làm nhận dạng sai. Hãy sửa câu nguồn trước khi dịch, nhất là tên riêng, số và câu phủ định.

### Đọc và sửa danh sách câu

Nếu đã bóc phụ đề xong rồi bấm chạy lại, hủy lần chạy mới sẽ giữ bộ câu và ảnh đối chiếu đã hoàn tất trước đó. Khi tác vụ đã dừng, bạn có thể tiếp tục kiểm tra/phê duyệt bộ câu cũ; các câu mơ hồ vẫn phải được xử lý trước khi duyệt. Nếu đã đổi nguồn, đoạn cắt hoặc ROI làm kết quả cũ hết hiệu lực, vẫn cần một lượt bóc mới hoàn tất. Hủy lần bóc đầu tiên không tạo ra kết quả để duyệt. Nút hủy gửi yêu cầu dừng; ứng dụng chỉ mở thao tác sau khi worker đã dừng thực sự.

Mỗi hàng là một câu hoặc đoạn thoại ngắn. Trong tài liệu kỹ thuật, hàng này được gọi là **cue**. Bạn có thể xem thời gian, phát đoạn tương ứng và sửa nội dung; giao diện còn có các thao tác tách/gộp hoặc xử lý câu được chọn tùy trạng thái dự án.

Khi sửa bản dịch, kiểm tra câu đó với đoạn video, không chỉ đọc riêng tiếng Việt. Nếu câu quá dài để đọc trong thời gian ngắn, cần rút gọn mà vẫn giữ nghĩa. Hệ thống có hỗ trợ đề xuất sửa timing; đề xuất AI vẫn cần duyệt khi có điểm chưa chắc chắn.

Trong tab **RapidOCR Cues**, bấm **Cần kiểm tra (số câu)** để gom các câu đang chặn phê duyệt từ toàn bộ video, không phải tìm từng trang. Bấm ảnh phụ đề để phóng lớn, sửa chữ nếu cần rồi bấm **Đã kiểm tra chữ**. Lưu thành công thì câu tự rời danh sách cần kiểm tra; nút **Đã xác nhận** cho xem lại những câu đã đối chiếu. Khi còn câu bị đánh dấu, nút **Phê duyệt OCR** tự mở danh sách cần kiểm tra. Các câu tin cậy thấp hoặc có phương án OCR khác vẫn phải được đối chiếu trước khi duyệt.

Nếu một dòng là tên video, bản quyền, watermark, dòng trống hoặc nội dung không cần đưa vào lời thoại, bấm **🗑 Xóa câu** rồi xác nhận để loại khỏi danh sách. Số câu cần kiểm tra sẽ cập nhật sau khi lưu thành công. Thao tác này loại dòng khỏi phụ đề/dịch/giọng đọc; chữ đã in trong hình video được xử lý riêng ở bước che chữ.

## 4. Mẫu chữ và nền che theo ảnh đã chọn

Xem khung hình dùng đúng thời gian đang dừng trên video. Nếu mốc đó nằm giữa hai câu thì có thể không thấy sub Việt hoặc hộp che; thông báo dưới ảnh giải thích và có nút chuyển đến câu có sub gần nhất. Ứng dụng không tự thêm chữ mẫu vào khung trống. Khi chưa dịch, cần tạo bản dịch trước để xem chữ Việt.

Trong **Che chữ Trung & Dán sub Việt**, bấm **Áp dụng mẫu ảnh chuẩn**, bấm **Xem thử**, rồi **Lưu cấu hình**. Nạp mẫu chưa tự ghi đè cấu hình đã lưu của dự án.

Mẫu hiện hành là **v2**: chữ trắng IN HOA, nét dày, hơi nghiêng phải, viền đen; dải nền mờ có phủ xám nhẹ và chạy ngang khung. Font Anton được kèm trong ứng dụng, không cần cài riêng. Mẫu được dựng theo đặc điểm ảnh bạn cung cấp, chưa xác định được đúng font gốc của ảnh.

Bạn vẫn chỉnh được:

- Font, cỡ chữ, màu chữ, màu và độ dày viền.
- Độ rộng nét chữ, độ xiên, giới hạn chiều rộng dòng và ưu tiên một dòng.
- Độ làm mờ nền, màu nền, độ đậm nền và phần đệm quanh vùng che.
- Bật/tắt dải che ngang hết khung.

**Vị trí và chiều cao dải che theo khung ROI** bạn vẽ trên video. Khi tắt chế độ ngang hết khung, chiều ngang cũng theo ROI. Chữ được căn giữa ROI; thanh cách đáy dùng cho bố cục song ngữ.

Nếu dải quá cao, điều chỉnh ROI cho vừa vùng phụ đề; không thu nhỏ đến mức làm lộ chữ gốc. Nếu còn thấy nét chữ Trung, kiểm tra lại ROI rồi tăng blur hoặc độ đậm nền. Xem thử cả cảnh sáng và tối, cả câu ngắn lẫn câu dài.

Ba kiểu xử lý:

| Lựa chọn | Kết quả |
| --- | --- |
| Fast Cover | Dùng nền màu hoặc làm mờ + phủ màu để che chữ gốc, đặt sub Việt lên trên. |
| Delogo | Nội suy vùng chữ từ ảnh xung quanh; có thể để lại vệt méo. Đây không phải AI phục hồi nền. |
| Song ngữ | Giữ chữ Trung, đặt chữ Việt ở vị trí khác. |

Chữ IN HOA chỉ thay đổi phần hiển thị, không sửa nội dung dịch lưu trong dự án hoặc ép giọng đọc đánh vần.

## 5. Dịch và tạo giọng cần những gì?

| Thành phần | Khi nào cần mạng/cài thêm? |
| --- | --- |
| Gemini để dịch | Cần mạng và API key hợp lệ. Quản lý trong mục Gemini API Keys. Có thể phát sinh phí hoặc bị giới hạn theo tài khoản. |
| Edge để đọc Việt | Cần mạng và dịch vụ đang hoạt động. Có các giọng Việt để nghe thử và chọn. |
| Dịch cục bộ HY-MT | Cần runtime/model đã cài và dịch vụ local được mở đúng cấu hình. Không tự coi là sẵn sàng khi chỉ có tên lựa chọn. |
| Giọng cục bộ VieNeu | Cần thư viện tùy chọn, model và bộ giọng đã đăng ký; dùng RAM của máy. |
| OCR và nghe lời thoại | Cần engine/model tương ứng. Lần thiết lập đầu có thể phải tải model. |
| Xuất video | Cần FFmpeg/FFprobe. GPU hỗ trợ có thể tăng tốc encode; nếu không dùng được, hệ thống có thể chuyển CPU và báo cảnh báo. |

Máy đã đăng ký giọng **Ngọc Huyền Review Phim** chỉ hiển thị giọng khi dữ liệu và runtime phù hợp. Không xóa `storage/voices` để dọn rác. Nếu hết RAM, dừng bớt tác vụ nặng rồi thử lại; có model trên ổ không có nghĩa máy luôn đủ RAM để chạy.

Trong phần tạo giọng, các lựa chọn Mặc định, Review Phim, Đêm Khuya, Tin Tức/Drama và Khoa Học Bí Ẩn là cách xử lý màu âm của giọng đã chọn, không phải năm người đọc khác nhau. Nghe thử trước khi tạo cả video.

**Từ điển cách đọc** cho phép nhập kiểu `từ = cách đọc`, mỗi dòng một mục. Từ điển áp dụng cho lời đọc, giữ nội dung phụ đề. Từ điển dùng chung được lưu tại `storage/tts/custom_dict.json` khi bạn đã lưu bằng giao diện.

Mục **Sử dụng API** giúp xem các lượt Gemini dịch đã được ghi nhận, token và lỗi. Các số thống kê này không phải số quota còn lại hoặc hóa đơn chính thức của nhà cung cấp.

## 6. Sửa lại có phải làm từ đầu không?

Thông thường, chỉ cần chạy lại phần bị ảnh hưởng:

| Thay đổi | Phần cần làm lại |
| --- | --- |
| Đổi video nguồn hoặc đoạn cắt | Nhập/tải nguồn và các bước xử lý phía sau. |
| Đổi vùng nhận dạng chữ | Nhận dạng và kiểm tra lại các bước sau. |
| Sửa câu nguồn | Dịch và tạo giọng cho phần bị ảnh hưởng, rồi xuất/kiểm tra lại. |
| Sửa bản dịch | Giọng của câu bị sửa, video tổng và kiểm tra lại. |
| Đổi giọng hoặc tốc độ | Tạo lại phần âm thanh liên quan, xuất/kiểm tra lại. |
| Chỉ đổi font, nền che hoặc âm lượng trộn | Xuất và kiểm tra lại video; không cần tải lại nguồn chỉ vì đổi kiểu chữ. |

Hệ thống giữ các kết quả dùng lại được. Phần lưu để khỏi làm lại được gọi là **cache**. Cache chỉ dùng khi nội dung và cấu hình còn phù hợp; không sửa tên file để ép hệ thống nhận lại dữ liệu cũ.

Nếu đã có video xuất nhưng sửa bản dịch, video cũ không tự trở thành video mới. Cần chạy render và QA lại.

## 7. Biết công việc đang chạy hay đã lỗi

Giao diện có tiến độ, tên công đoạn, thời gian và thông báo lỗi. Một số công đoạn mạng hoặc model có thể chờ lâu; đừng bấm chạy nhiều lần liên tục.

- **Đang chạy/chờ:** tiếp tục theo dõi. Không đóng cửa sổ máy chủ nếu muốn tác vụ tiếp tục.
- **Bước 4 đang chuẩn bị video dò phụ đề:** hệ thống có thể tạo bản 960px của toàn bộ video trước khi dò chữ. Xem số giây đã chuẩn bị trong dòng trạng thái; sau đó mới chuyển sang số khung hình có chữ và số câu OCR. Không khởi động lại máy chủ khi tác vụ vẫn tiến triển.
- **Hủy:** yêu cầu dừng có kiểm soát; kết quả đã hoàn thành có thể được giữ để chạy tiếp.
- **Lỗi:** đọc thông báo, sửa nguyên nhân rồi chạy lại đúng bước. Không xóa dự án chỉ vì một dịch vụ tạm lỗi.
- **Đã kiểm tra kỹ thuật:** các chỉ số đầu ra đã qua kiểm tra ở lần chạy đó. Bạn vẫn cần xem và nghe để duyệt nội dung.

Hệ thống kiểm tra thời lượng, đủ câu dịch/giọng cần thiết, độ lệch lời đọc và luồng âm thanh/hình ảnh. Các ngưỡng timing gồm lệch trung bình không quá 0,3 giây và lệch lớn nhất không quá 1,5 giây; lệch thời lượng nguồn/đầu ra không quá 0,1 giây. Không nên bỏ qua cảnh báo chỉ vì đã có file MP4.

Chưa có cam kết mọi video 60 phút luôn xử lý xong trong 15–20 phút. Thời gian phụ thuộc độ dài, số câu, mạng, model, RAM, GPU và bước kiểm tra âm thanh.

## 8. Lưu thành phẩm lên Google Drive — tùy chọn

Nếu chỉ cần lưu video trên máy, bạn có thể bỏ qua phần này.

Thiết lập một lần bằng tài khoản Google của bạn:

1. Mở [Google Cloud Console](https://console.cloud.google.com/), chọn hoặc tạo một project để dùng cho kho video.
2. Trong thư viện API, bật **Google Drive API**.
3. Cấu hình màn hình đồng ý của ứng dụng: tên ứng dụng, email liên hệ và đối tượng sử dụng. Nếu đang ở chế độ Testing, thêm email sẽ đăng nhập vào danh sách người dùng thử.
4. Tạo thông tin đăng nhập OAuth loại **Desktop app** và tải file JSON. Đây là file cấu hình để ứng dụng mở trang Google cho bạn đăng nhập; không phải mật khẩu Google và cũng không phải Gemini API key.
5. Trong DuanLayLink, mở **Kho cloud → Thiết lập lần đầu**, chọn JSON vừa tải và bấm **Lưu cấu hình**.

Tên mục trong trang Google có thể khác theo giao diện/tài khoản. Người cài đặt có thể đối chiếu [hướng dẫn ứng dụng desktop của Google](https://developers.google.com/identity/protocols/oauth2/native-app). Không gửi mật khẩu hoặc mã xác nhận qua chat để thiết lập.

Sau đó:

1. Bấm **Kết nối Google Drive**, tự đăng nhập trong trang Google và quay lại ứng dụng.
2. Chọn video đã qua QA kỹ thuật, bấm **Tải / tiếp tục lên Drive**.
3. Đợi ứng dụng xác minh file đã tải, không chỉ nhìn số phần trăm đã gửi.
4. Mở bản trên Drive để kiểm tra trước khi quyết định giải phóng MP4 trên máy.

Nếu đóng nhầm tab đăng nhập, bấm **Hủy đăng nhập** rồi kết nối lại. Tải lại trình duyệt không tự hủy phiên đang chờ.

Nút **Giải phóng MP4** chỉ dọn đúng thành phẩm local sau khi kiểm tra bản Drive, không phải xóa toàn bộ dự án. Tính năng tải Drive không sao lưu đầy đủ database, nguồn, phụ đề và cache. Muốn chuyển máy để chỉnh tiếp vẫn cần sao lưu dữ liệu làm việc. Đăng nhập cloud có thể cần làm lại trên máy hoặc tài khoản Windows khác.

## 9. Dữ liệu nằm ở đâu? Cần giữ những gì?

Các vị trí dưới đây là cấu hình mặc định; `.env` có thể đổi nơi lưu.

| Vị trí | Ý nghĩa |
| --- | --- |
| `storage/app.db` | Sổ quản lý dự án, câu chữ và trạng thái. Đây là nguồn dữ liệu chính, không được coi là file rác. |
| `storage/projects/<mã dự án>/` | Video nguồn, ảnh nhận dạng, phụ đề, giọng, cấu hình và thành phẩm của từng video. |
| `storage/projects/<mã dự án>/output/video_vi.mp4` | Vị trí thành phẩm thông thường sau khi xuất thành công. Ưu tiên tải qua giao diện để đúng bản hiện hành. |
| `storage/models/` | Các model tải về để chạy AI cục bộ. Xóa có thể khiến chức năng tương ứng không dùng được. |
| `storage/voices/` | Dữ liệu giọng đã đăng ký. Cần giữ nếu muốn dùng lại giọng đó. |
| `tools/` | Công cụ đi kèm như VideoSubFinder và runtime dịch local. Các file chương trình/DLL là thành phần cần thiết. |
| `assets/fonts/` | Font của mẫu phụ đề cùng giấy phép sử dụng. |
| `.env` và file khóa riêng | Cấu hình, thông tin xác thực. Giữ riêng, không đăng công khai. |
| `storage/audits/` | Kết quả kiểm chứng thật được tạo khi chạy audit. Các lượt thử cũ đã được dọn; thư mục sẽ xuất hiện lại nếu chạy audit mới. |
| `storage/maintenance/` | Nhật ký nguyên nhân lỗi và báo cáo dọn dẹp mới nhất. Không giữ bản nén của các file thử đã bỏ. |

Để sao lưu khi chuyển máy, dừng ứng dụng rồi sao lưu `storage/app.db` cùng `storage/projects`; thêm bộ giọng, model và cấu hình nếu muốn dùng tiếp như máy cũ. Thông tin đăng nhập được bảo vệ theo tài khoản Windows có thể cần cấu hình/đăng nhập lại. Chỉ chép MP4 thành phẩm không đủ để tiếp tục chỉnh sửa dự án.

File JSON/SRT/ASS là dữ liệu trao đổi hoặc kết quả xuất, không thay thế database. Không tự sửa/xóa JSON để cố thay đổi trạng thái dự án.

### Tạo giọng cho video dài và đo tốc độ CPU

Trong tab **Tạo Giọng**, chọn một giọng rồi bấm **Nghe thử giọng đã chọn** để tạo mẫu bằng đúng giọng, tốc độ và màu giọng hiện tại. Trình phát cho nghe lại mẫu; đổi giọng rồi nghe tiếp để so sánh. Mẫu không sửa câu, bản dịch hoặc giọng đã tạo của dự án. Khi có tác vụ đang chạy, cần dừng/chờ xong trước khi tạo mẫu để tránh tranh tài nguyên. Mẫu được xử lý bằng cùng đường tạo âm thanh và cache như giọng đầy đủ.

Nếu dự án đã có một câu ngắn trong cache khớp chính xác giọng/tốc độ/phiên bản, nghe thử sẽ dùng câu đó (1,5–15 giây) để tránh gọi dịch vụ lại. Không dùng âm thanh của giọng khác thay thế. Nếu chưa có cache và Edge trả `NoAudioReceived`, dịch vụ chưa cung cấp được âm thanh; thông báo sẽ hiện nguyên nhân. `Method Not Allowed`/HTTP405 ngay sau cập nhật thường là máy chủ chưa nạp endpoint mới: mở lại máy chủ khi hết tác vụ, rồi Ctrl+F5.

Lựa chọn giọng/tốc độ/màu giọng được nhớ riêng theo dự án trong trình duyệt này. Nút **Dùng làm mặc định cho dự án mới** đặt lựa chọn ban đầu cho dự án chưa có cấu hình riêng; không ghi đè giọng các dự án đã chọn. Nút tạo giọng ghi rõ tên đang chọn để dễ kiểm tra trước khi chạy. Nếu giọng đã lưu chưa sẵn sàng, ứng dụng yêu cầu chọn lại thay vì tự đổi sang giọng khác. Xóa dữ liệu trình duyệt hoặc chuyển trình duyệt/máy sẽ mất các lựa chọn này.

Muốn đổi giọng giữa chừng: chọn giọng mới rồi bấm **Dừng lượt cũ và dùng giọng đã chọn**, xác nhận, chờ worker cũ dừng rồi ứng dụng mới gửi lượt mới. Việc chọn dropdown đơn thuần không đổi tác vụ đang chạy. Cache đúng giọng mới sẽ dùng lại; câu chưa có cache cần tạo lại, không thể chuyển các câu đã đọc bằng giọng cũ thành giọng mới mà không tổng hợp lại. Giọng cũ vẫn còn trong cache để có thể đổi lại. Khi lượt giọng mới hoàn tất, cần render lại để cập nhật video thành phẩm. Tác vụ download/OCR/dịch không bị nút này hủy.

Tiến độ tạo giọng hiển thị bộ đọc thực tế (`edge` hoặc `vieneu`) và loại lỗi gần nhất nếu có. “Câu lỗi 0” không có nghĩa mọi lần gọi đều thành công: hãy xem thêm số lần gọi lỗi/chờ thử lại. Khi Edge đang lỗi, tăng nhiều luồng có thể làm chậm thêm; VieNeu local cần đánh giá CPU/RAM riêng. Trong cùng lượt chạy, các câu trùng đầy đủ nội dung đã chuẩn hóa, giọng, tốc độ và phiên bản dùng chung lần tạo giọng; vẫn giữ file/timing riêng cho từng câu. Các câu khác thiết lập không bị gộp.

Hàng đợi tạo giọng lưu mã cache từng câu, không giữ toàn bộ WAV của video trong RAM. Khi ghép, ứng dụng đọc và kiểm tra lại từng câu; cache mất/hỏng sẽ báo lỗi để chạy lại, không xuất bản giọng thiếu câu. Cache hợp lệ vẫn dùng lại khi tiếp tục hoặc chỉ sửa một câu. Timeline dùng tệp trên đĩa, nên vẫn cần đủ dung lượng trống.

VieNeu giữ một worker; `VIENEU_CPU_THREADS=2` trong `.env` là số luồng tính toán bên trong mô hình, khác số câu chạy đồng thời. Không tăng số worker để tăng tốc trên máy RAM ít. Để so sánh 1/2/4 luồng với câu ngắn, vừa và dài, khi không chạy tác vụ hãy dùng:

```powershell
python scripts/benchmark_vieneu_cpu.py
```

Công cụ lưu báo cáo và WAV nghe thử vào một thư mục mới dưới `storage/audits`, đo riêng thời gian nạp mô hình và các lượt đọc sau đó. Nó tạm hoãn nếu có worker VieNeu khác hoặc RAM trống dưới 2 GiB; đây là điều kiện tối thiểu để đo, không bảo đảm đủ bộ nhớ trong mọi tình huống. Không tự thay `.env`, giọng hay dự án. Sau khi nghe kiểm tra kết quả và xem `suggested_threads` trong báo cáo có trạng thái `measured`, có thể đặt `VIENEU_CPU_THREADS` tương ứng, rồi mở lại máy chủ khi hết tác vụ. Chưa có phép đo hoàn tất thì giữ mặc định 2. Đổi số luồng không xóa cache giọng hiện có.

## 10. Những lỗi thường gặp

| Hiện tượng | Kiểm tra và xử lý |
| --- | --- |
| Không vào được giao diện | Mở `start_server.bat`, xem lỗi trong cửa sổ máy chủ, kiểm tra đúng địa chỉ/cổng. |
| Có nút mới nhưng bấm báo không tìm thấy chức năng | Mở lại máy chủ sau khi cập nhật, rồi Ctrl+F5. |
| Không tải được Bilibili 1080p | Kiểm tra quyền xem/cookie và thông báo của nguồn. Không coi video chất lượng thấp hơn là 1080p. |
| Nhận dạng không có câu hoặc sai nhiều | Kiểm tra đoạn mẫu có chữ/thoại, chọn đúng chế độ và khoanh đúng ROI. |
| Dịch bị chờ quota hoặc lỗi key | Kiểm tra Gemini API Keys và lỗi được báo; đợi hoặc thay key hợp lệ. Thêm key cùng project không bảo đảm có quota độc lập. |
| Giọng không phát ra hoặc dịch vụ đọc lỗi | Nghe thử, kiểm tra mạng/runtime, RAM và thông báo; chạy lại bước tạo giọng. |
| Lời đọc tràn sang câu sau | Xem nhóm câu tràn, rút gọn đúng nghĩa hoặc dùng công cụ sửa timing; nghe lại thay vì tăng tốc vô hạn. |
| Chữ Việt chưa che hết chữ Trung | Sửa ROI, blur/độ đậm nền; kiểm tra bằng khung preview thật. |
| Đã thấy MP4 nhưng chưa tải được bản giao | Kiểm tra render và QA đã hoàn tất; file tạm hoặc kết quả chưa qua kiểm tra chưa phải thành phẩm. |
| Hết dung lượng | Kiểm tra dung lượng video/model; dọn bản tải nén đã giải nén hoặc cache tạm, không xóa database hay nguồn đang cần chỉnh. |

## 11. Nếu cần người khác sửa hoặc bảo trì hệ thống

Thử nghiệm tăng tốc OCR bằng video chỉ chứa vùng phụ đề được tách riêng trong `scripts/benchmark_ocr_roi.py`; chưa dùng làm mặc định. Ví dụ: `python scripts/benchmark_ocr_roi.py <video> <roi.json> --starts 30 900 1800 --seconds 20 --rounds 2 --output <thu-muc-audit-moi>`. Công cụ cần VideoSubFinder và NVENC đang hoạt động, chạy trên mẫu có sẵn, không sửa dự án. Chỉ chạy khi máy rảnh để so sánh thời gian. Cần xem ảnh gốc và đối chiếu chữ/timing; số câu, độ tin cậy OCR hoặc kết quả trùng với cách cũ chưa đủ chứng minh đúng. Thử nghiệm ngày 24/10 phát hiện cách giữ độ phân giải gốc có thể bỏ một chữ dù báo độ tin cậy 99%, nên chưa được đưa vào pipeline.

Không cần đọc phần này để dùng ứng dụng. Nó giúp chỉ đúng nơi khi nhờ người kỹ thuật kiểm tra:

- `web/`: giao diện bạn nhìn thấy trong trình duyệt.
- `src/api/`: nhận yêu cầu từ giao diện.
- `src/pipeline/`: điều phối các bước, xuất video và kiểm tra đầu ra.
- `src/ocr/`, `src/stt/`, `src/translation/`, `src/tts/`: nhận chữ, nghe thoại, dịch và tạo giọng.
- `src/download/`, `src/providers/`, `src/cloud/`: tải nguồn và làm việc với dịch vụ bên ngoài.
- `tests/`: bộ kiểm tra để tránh làm hỏng chức năng khi sửa code.
- `scripts/`: công cụ hỗ trợ, kiểm chứng và thiết lập; không cần chạy lần lượt để dùng ứng dụng.

Muốn kiểm tra phần mềm khi bảo trì: cài `requirements-dev.txt`, chạy `python -m pytest -q`. Bộ test dùng dữ liệu tạm và chặn các lời gọi provider mạng đã quy định; audit video/dịch vụ thật chạy riêng. Test đạt không thay thế việc xem/nghe video thành phẩm.

Các video/âm thanh thử cũ, script gắn cứng với một lần thử, model thử chưa được tích hợp và bản nén tài liệu cũ đã được xóa. Nhật ký nguyên nhân lỗi được giữ tại `storage/maintenance/BUG_REGRESSIONS.md`; đường dẫn artifact lịch sử trong đó có thể không còn tồn tại.

**Các file trong `tests/` dùng lại được**, không phải mỗi file chỉ kiểm tra một lần. Mỗi lần sửa chương trình, chạy lại chúng để phát hiện việc làm hỏng tải video, dữ liệu câu, dịch, tạo giọng hoặc xuất video. Các test này tạo dữ liệu tạm riêng rồi dọn sau khi chạy; không cần giữ các video thử cũ để chạy lại. Những script audit còn giữ hỗ trợ kiểm tra trên dịch vụ/video thật khi cần, khác với dữ liệu đầu ra cũ đã bị xóa.


### Upload lớn có thể tiếp tục

Trong **Dự án mới**, dùng nút **Tải video từ máy này lên server**. Trình duyệt gửi các phần tối đa 8 MiB; tiến độ dựa trên byte server đã xác nhận. Mất kết nối tạm thời sẽ thử lại có giới hạn. Nếu đóng hoặc tải lại trang, đăng nhập cùng tài khoản, mở Dự án mới và chọn **Chọn lại tệp để tiếp tục** ở phiên tải dở; phải chọn lại cùng tệp. Dấu vân tay mẫu kiểm tra tên, kích thước và dữ liệu đầu/cuối; đây không phải hash toàn bộ tệp gốc. API có hỗ trợ expected_sha256 toàn file khi client cung cấp.

Sau khi đủ byte, server đọc metadata và giải mã mẫu đầu/giữa/cuối. Trạng thái **VERIFYING** có thể chờ tài nguyên nếu máy đang chạy công đoạn nặng. Có thể đóng trang, rồi quay lại bấm **Kiểm tra kết quả** hoặc làm mới danh sách. Chỉ khi kiểm định đạt mới tạo dự án; bước tải/nhập media của dự án vẫn sử dụng quy trình hiện có. Kiểm tra mẫu không chứng minh mọi khung hình ngoài mẫu đều lành lặn.

**Tạm dừng tải** dừng sau mảnh hiện tại, giữ phần đã nhận. **Hủy phiên** đóng phiên nhưng không xóa byte ngay; dữ liệu chờ chính sách dọn được nghiệm thu ở Đợt 4. Phiên không hoạt động hết hạn sau UPLOAD_SESSION_HOURS; giới hạn hiện tại tối đa ba phiên hoạt động mỗi tài khoản. Không xóa thủ công storage/uploads/sessions khi còn cần nối lại.

Các ngưỡng upload nằm trong .env.example; MIN_FREE_DISK_START_BYTES/MIN_FREE_DISK_RUNTIME_BYTES ở Đợt 1 áp dụng cho tiếp nhận upload, chưa phải kiểm tra đĩa xuyên suốt OCR/TTS/render của Đợt 2. MAX_SOURCE_VIDEO_DURATION_SECONDS giới hạn toàn video nguồn; MAX_VIDEO_DURATION_SECONDS hiện hữu vẫn áp dụng ở luồng xử lý đoạn đã chọn. Giới hạn dung lượng tổng mặc định hiện hữu vẫn là 500 MiB, không tự tăng lên 2 GiB.

Migration thêm hai bảng phiên/mảnh và cột Project.source_upload_id có index duy nhất; không xóa hoặc thay thế bảng dự án cũ. Sau khi cập nhật backend cần khởi động lại khi máy rảnh để nạp schema/API mới.

Khi chạy trực tiếp/LAN, để TRUSTED_PROXY_IPS=[]. Với proxy cục bộ đã xác định, có thể cấu hình TRUSTED_PROXY_IPS=["127.0.0.1"] rồi khởi động lại. Không dùng ["*"]. SERVER_PUBLIC_URL phải khớp địa chỉ HTTPS được cấu hình thật; điền URL không tự tạo đường hầm.


### Link truy cập từ xa đang cấu hình

Mở **https://nhunhi.tail343939.ts.net/** trên máy khác hoặc điện thoại dùng mạng khác, rồi đăng nhập bằng tài khoản **DuanLayLink** được admin cấp. Người dùng mở link Funnel không cần cài Tailscale. Admin vào **Quản lý máy chủ → Chia sẻ truy cập** để sao chép link hoặc tạo lời mời. Nếu giao diện cũ còn lưu, nhấn Ctrl+F5.

Link cần máy chủ đang bật, backend đang chạy và Tailscale kết nối. Funnel đã được bật chạy nền, trỏ tới http://127.0.0.1:8088; có thể kiểm tra bằng lệnh `tailscale funnel status`. Không mở port modem. Nếu đổi tên thiết bị/tailnet/URL, cập nhật SERVER_PUBLIC_URL trong .env và nạp lại server khi rảnh.

Đã thử truyền hơn 100 MiB qua IP relay Internet công khai với tài khoản/server audit riêng, cùng kiểm tra đăng nhập public trên server thật. Đây chưa thay thế xác nhận từ máy vật lý thứ hai; không phải cam kết uptime hoặc tốc độ video dài.


### Tác vụ bị gián đoạn, tạm dừng và tiếp tục

Trong chế độ máy chủ nhiều người dùng, tác vụ dừng do khởi động lại, thiếu đĩa hoặc lỗi I/O được phân biệt bằng **INTERRUPTED / Bị gián đoạn**. Khởi động server và mở dự án không tự chạy lại OCR/TTS hay gọi dịch vụ giọng đọc. Các dự án mới tạo chưa gửi tác vụ vẫn giữ trạng thái chờ, không bị đánh dấu gián đoạn chỉ vì khởi động lại.

Khi đang chạy, người gửi tác vụ hoặc admin có thể bấm **Tạm dừng an toàn** ở bảng hoạt động. OCR/TTS dừng theo điểm kiểm tra; render dừng tiến trình FFmpeg và bỏ file encode chưa hoàn tất. Lời gọi dịch vụ/inference đang thực hiện có thể cần thời gian trả về trước khi dừng; đây không phải dừng tức thời mọi thao tác. Hủy tác vụ vẫn là thao tác riêng với trạng thái CANCELLED.

Mở dự án bị gián đoạn sẽ có bảng **Tác vụ bị gián đoạn**, lý do và nút **Kiểm tra và tiếp tục**. Chọn dùng thiết lập hiện tại trong Studio hoặc thiết lập của lượt trước. Nếu nguồn, phụ đề, ROI, giọng/thiết lập hay phiên bản xử lý đã thay đổi, hệ thống báo phần khác biệt và hỏi xác nhận trước khi gửi tác vụ mới. Chỉ cache đúng nội dung và còn hợp lệ được tái sử dụng; phần thiếu/hỏng cần tạo lại. Tác vụ cũ chưa có chữ ký đầu vào yêu cầu xác nhận chạy lại.

Dữ liệu phụ đề trong database, kể cả danh sách/câu cố ý để trống, là có thẩm quyền. File JSON cũ không được dùng để lấp lại nội dung đã xóa khi kiểm tra tiếp tục.

Các lượt tạo giọng/render mới trong SERVER_MODE ghi vào **attempts/** riêng. Database chỉ công nhận kết quả khi token quyền ghi, lượt chạy, trạng thái và đầu vào vẫn hợp lệ. Nếu hủy hoặc mất quyền trước lúc công bố, bản trước vẫn được giữ; thư mục chưa công nhận được đánh dấu UNCOMMITTED_ATTEMPT và chưa tự xóa. Tệp cũ dùng đường dẫn truyền thống vẫn được đọc khi chưa có con trỏ mới; không di chuyển hàng loạt dự án cũ. Chế độ CLI/local không bật SERVER_MODE tiếp tục dùng luồng đường dẫn cũ.

MIN_FREE_DISK_START_BYTES được kiểm tra trước tác vụ; MIN_FREE_DISK_RUNTIME_BYTES được kiểm tra tại các vòng xử lý và qua bộ giám sát tác vụ nặng. Nếu thiếu đĩa, giải phóng dung lượng bằng cách có kiểm chứng rồi kiểm tra tiếp tục. Khi SQLite cũng không thể ghi do hết đĩa, trạng thái lỗi có thể chỉ còn trong bộ nhớ/log; khởi động/đối chiếu lại khi ổ đĩa hoạt động để ghi nhận gián đoạn. Không làm đầy ổ chứa dự án để thử lỗi.

Giọng từng câu có cache riêng: đổi một câu có thể dùng lại các câu không đổi; đổi giọng sẽ cần tạo giọng tương ứng. OCR trên ảnh VSF có cache kết quả theo pixel/nội dung ảnh và phiên bản nhận dạng; một lượt quét video chưa hoàn tất không tự trở thành checkpoint hoàn tất. Encode render đã hoàn thành và khớp checkpoint có thể dùng lại để tiếp tục QC; encode còn dở phải chạy lại.

Phần này chưa triển khai tự dọn attempts/cache, sao lưu bắt buộc ra kho ngoài hoặc kiến trúc nhiều worker. Giữ một backend theo launcher hiện tại.

### Sao lưu có đối chiếu tệp bắt buộc

Trong **Quản lý máy chủ → Sao lưu**, tạo bản sao khi máy rảnh. Chỉ nhãn **VALIDATED_PASS / Đạt · REQUIRED đã đối chiếu** mới là gói đã kiểm tra danh mục file bắt buộc từ snapshot DB. FAILED_INCOMPLETE là còn thiếu tệp DB đang tham chiếu; FAILED_CORRUPT_SOURCE là checksum/size khác mốc đã lưu. Không xóa tham chiếu hoặc file để ép bản sao đạt. Các báo cáo lỗi hiển thị số vấn đề và đường dẫn mẫu; manifest.json trong thư mục bản sao giữ danh sách đầy đủ.

Theo lựa chọn hiện tại, gói đạt được sao chép sang **D:\DuanLayLinkBackups**. Cấu hình nằm ở SERVER_BACKUP_DIRECTORY; SERVER_BACKUP_ALLOW_SAME_DISK=true cho phép bản sao cục bộ. C: và D: hiện cùng ổ vật lý: nhãn LOCAL_REPLICA_VERIFIED và LOCAL_RESTORE_DRILL_PASS không chứng minh chịu được hỏng ổ, không cho phép xóa dữ liệu gốc. Tệp thiếu không được tự tạo lại trong lúc backup.

Nút **Phục hồi thử** đọc lại gói đã sao chép, phục hồi vào storage/restore_drills mới rồi kiểm DB/tất cả REQUIRED. Dữ liệu đang dùng không bị ghi đè. Khi muốn chuyển cloud, kết nối Google Drive ở Kho cloud, rồi dùng **Lưu Google Drive** và **Phục hồi thử** cho gói đạt. Chức năng Drive đã có test transport giả lập; chưa có tài khoản Drive thật được nghiệm thu cho gói hệ thống này.

Nếu database/máy chủ cũ không dùng được nhưng còn ZIP trên D:, phục hồi bằng lệnh dưới đây, thay tên gói và chọn một thư mục mới ngoài projects đang dùng:

```powershell
python scripts/server_backup.py restore-package "D:\DuanLayLinkBackups\TEN_GOI.zip" --destination "D:\DuanLayLinkRestoreMoi"
```

Xem restore.env trong thư mục mới trước khi cấu hình máy chủ dùng dữ liệu phục hồi. Phiên đăng nhập/lời mời/quyền sửa cũ bị thu hồi; DPAPI, model và công cụ có thể cần thiết lập lại trên máy khác. Gói sao lưu có dữ liệu riêng và cấu hình tham chiếu: giữ trong kho riêng của admin. Chi tiết kiểm chứng và giới hạn: docs/PHASE3_ACCEPTANCE.md.

### Dọn dữ liệu và đối chiếu giọng lỗi (admin)

Vào **Quản lý máy chủ → Dọn dữ liệu** rồi bấm **Xem trước dữ liệu có thể dọn**. Server lập danh sách giữ hiệu lực 10 phút; chọn từng mục được phép và bấm **Xóa các mục đã chọn**, đọc xác nhận trước khi thực hiện. Chưa bấm xóa thì candidate mới chưa bị xóa. Mục còn tham chiếu, có người đọc, đang xử lý hoặc chưa đủ 24h sẽ bị khóa/bỏ qua. Nếu trạng thái thay đổi sau preview, cần xem trước lại. `DELETE_FAILED` hiển thị lỗi hệ điều hành; xử lý nguyên nhân khóa/quyền rồi xem trước lại để retry. Không có nút xóa toàn bộ nguồn/model/dự án trong luồng này.

Thành phẩm gốc bị khóa khi chỉ có bản sao D: cùng ổ vật lý. Chế độ server cũng chặn đường xóa project/MP4 cũ và sweeper tự dọn để không đi vòng qua preview/reader guard.

Trong mục **Đối chiếu và đặt lại tham chiếu giọng của công đoạn lỗi**, chọn dự án rồi đối chiếu. Chỉ khi đủ điều kiện hệ thống mới hiện nút xác nhận số tham chiếu thiếu. Đặt lại chỉ bỏ `voice_audio_path`/`voice_duration` không có tệp; giữ chữ Việt và thời gian, có biên bản trước/sau được đưa vào backup. Đây không phải tạo lại âm thanh. Muốn dùng giọng cho dự án đã đặt lại, chủ động chạy lại Tạo Giọng sau đó; không tự gọi provider.

`SERVER_BACKUP_ROOT=D:\DuanLayLinkBackups\snapshots` đặt vùng snapshot và gói làm việc trên D: để tránh nhân nhiều video trên C:. ZIP bản sao ở D:\DuanLayLinkBackups; phục hồi thử từ giao diện ở D:\DuanLayLinkBackups\restore_drills. Bản legacy trên C: vẫn được catalog giữ lại. Sau phục hồi, các phiên preview/reset trước đó hết hiệu lực, cần đăng nhập và xem trước mới.

Chi tiết điều kiện, bằng chứng và giới hạn: docs/PHASE4_ACCEPTANCE.md.


### Bảo trì tệp cũ ngày 26/10/2026

Đợt kiểm tra tiếp theo sửa dịch một số câu và tiếp tục dịch trong chế độ server, dùng đúng thành phẩm của lượt đã công bố khi Chạy tất cả, giữ cấu hình/đầu ra cũ nếu ghi ROI hoặc thay file tải thất bại. VideoSubFinder 6.10 có quy ước mã thoát riêng: ứng dụng đối chiếu bản công cụ, dấu hoàn tất của chính lượt quét và log lỗi; không coi vài ảnh đã xuất là quét xong. Cache quét cũ sẽ cần quét lại theo phiên bản mới và checksum video. Bộ dò dự phòng được ghi rõ là opencv-rapidocr. Không tự bỏ qua câu OCR cần duyệt hoặc hạ ngưỡng timing/âm thanh.

Kiểm định lại toàn luồng trong kho riêng, dùng dự án đã có làm nguồn chỉ đọc:

```powershell
python scripts/audit_full_pipeline.py MA_DU_AN --mode fresh --check-source
# Kiểm tra bản chữ đã duyệt với giọng/render mới, có ghi rõ phần được dùng lại:
python scripts/audit_full_pipeline.py MA_DU_AN --mode reviewed
# Tiếp tục đúng kho audit sau khi đối chiếu/sửa câu trong database audit:
python scripts/audit_full_pipeline.py MA_DU_AN --mode fresh --resume-dir storage/audits/TEN_LUOT
```

Lệnh dùng dịch vụ thật, có thể dùng quota API; thực hiện ngoài pytest khi máy rảnh. Audit mới có database/projects riêng dưới storage/audits, không sửa dự án gốc. Kết quả có nguồn/checksum, câu, task, RAM và kiểm định file thật. `passed: false` cùng lỗi kiểm duyệt/provider/QC cần được xử lý; không đổi nhãn để ép đạt. Chạy kiểm định video không có nghĩa đã duyệt toàn bộ nghĩa bản dịch hoặc giọng nghe tự nhiên.

Nếu mẫu xuất vẫn lộ chân chữ gốc, xem khung hình tại đúng cue rồi chỉnh ROI/phần đệm che và render lại; tăng vùng che có thể che thêm hình nên cần xem lại các cảnh khác. Thay phần đệm/font/nền che chỉ cần render/QA, giọng đúng cấu hình vẫn dùng được. Đợt nghiệm thu 26/10 giữ thay đổi vùng che trong kho audit riêng, không tự đổi cấu hình dự án đang dùng. Chi tiết kết quả: `docs/FOLLOWUP_ACCEPTANCE_20261026.md`.

Sau bản sửa kiểm định/cache ngày 26/10, các manifest tải Range cũ sẽ được tải lại nếu không khớp đầy đủ URL nguồn (bao gồm máy chủ). Cache giọng có dấu `:` trong tên giọng/nội dung/thiết lập dùng khóa mới để tránh nhầm âm thanh; cache cũ không nhập nhằng vẫn được dùng. Không cần xóa cache thủ công. Manifest giọng sai cấu trúc không còn làm bước tiếp tục bị văng lỗi. Kiểm định video từ chối lỗi giải mã kể cả khi FFmpeg trả mã thoát 0; nếu bị từ chối, kiểm tra/tải lại nguồn thay vì bỏ qua lỗi. Khởi động lại backend khi hết tác vụ để nạp thay đổi.

Giữ mã test hồi quy trong tests/ và các công cụ audit/benchmark có tham số dùng lại được. Các đầu ra thử nghiệm đã kết thúc, thư mục pytest tạm và bản sao chưa qua kiểm định được đối chiếu riêng với dữ liệu thật trước khi dọn. Không coi model/runtime đang hỗ trợ dịch local là rác chỉ vì dịch vụ chưa chạy.

Đã gỡ 4 helper dùng một lần có đường dẫn mẫu/thông tin đăng nhập cũ gắn cứng: audit_funnel_upload.py, audit_phase4_live.py, apply_failed_voice_reset.py, release_phase2.py. Dùng giao diện quản trị hiện hành cho đối chiếu giọng/sao lưu; các công cụ server_backup/run_small_server và audit cách ly dùng lại vẫn giữ. Đã xóa một video audit có SHA-256 trùng bản phục hồi được giữ. Các đường dẫn helper/đầu ra lịch sử trong tài liệu nghiệm thu không phải hướng dẫn vận hành hiện tại.

Đợt dọn này chưa hoàn tất: công cụ duyệt tự động chặn xóa thư mục đệ quy; xem storage/maintenance/cleanup_report.json để biết số đã dọn, checksum dữ liệu được bảo vệ và phần bị chặn. Không báo rằng toàn bộ thư mục cũ đã được xóa. Kế hoạch chờ ở cleanup_plan.json cần kiểm kê lại trước lần thực hiện tiếp theo.


### Chạy lại QA sau render hoặc cập nhật tool

Render lại làm hết hiệu lực QA của video trước. Sau render, bấm **QA** để kiểm tra bản mới; nếu QA báo lỗi hoặc bị gián đoạn, video chưa được coi là đã đạt. Nút QA dùng `POST /api/projects/{id}/verify`, cần quyền ghi và phiên chỉnh sửa trong chế độ server. GET cũ không chạy QA nữa; chương trình tích hợp cần đổi sang POST. Khởi động lại backend khi hết tác vụ và Ctrl+F5 trình duyệt để nạp bản sửa đồng bộ. Kết quả QA kỹ thuật không thay thế duyệt nội dung từng cue.
