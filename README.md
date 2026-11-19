# DuanLayLink — Studio Video Cá Nhân

Ứng dụng làm phụ đề và lồng tiếng Việt cho video ngắn và dài (Douyin, Bilibili, TikTok, tệp video máy tính): tải video, nhận dạng chữ/lời thoại (RapidOCR / Whisper), dịch thuật đa nguồn (Gemini / HY-MT / Azure), tạo giọng đọc AI (Piper / VieNeu / Edge-TTS), căn timing theo video (`cluster-budget-v1`), che sub cũ, ghép sub Việt và xuất MP4 / SRT theo mục tiêu đã chọn. Khả năng tải phụ thuộc nguồn, quyền truy cập và cookie; chất lượng nhận dạng, dịch và giọng vẫn cần xem/nghe lại.

**API/AI vẫn được giữ.** Cấu hình cá nhân mặc định là `PERSONAL_MODE=true`, `OFFLINE_MODE=false`. Kết nối mạng được dùng cho việc tải video nguồn, gọi API Gemini và giọng online; không cần cấu hình server cho máy khác, cloud hay tài khoản nhiều người. Tuyệt đối không sửa hoặc chia sẻ nội dung file `.env` chứa khóa API.

---

## Mục lục

1. [Không gian làm việc & Thư mục duy nhất](#thư-mục-duy-nhất)
2. [Khởi động & Mở ứng dụng](#mở-ứng-dụng)
3. [Quản lý dự án trên Bảng điều khiển (Dashboard)](#quản-lý-dự-án-trên-bảng-điều-khiển)
4. [Quy trình làm video (9 Bước Studio & Auto 1-Click)](#quy-trình-làm-video)
5. [Bóc tách phụ đề và kiểm tra nguồn](#bóc-tách-phụ-đề-và-kiểm-tra-nguồn)
6. [Điều phối dịch thuật đa nguồn & Quản lý API Key](#điều-phối-dịch-thuật-đa-nguồn--quản-lý-api-key)
7. [Tạo giọng đọc & Căn chỉnh Timing Bước 7 (Cluster Budget)](#tạo-giọng-đọc--căn-chỉnh-timing-bước-7)
8. [Che chữ mờ tự nhiên, Mẫu sub & Render video](#che-chữ-mờ-tự-nhiên-mẫu-sub--render-video)
9. [Khắc phục sự cố, Duyệt thủ công & Tiếp tục Auto](#khắc-phục-sự-cố-duyệt-thủ-công--tiếp-tục-auto)
10. [Hướng dẫn CLI & Bộ công cụ Benchmark / Chẩn đoán](#hướng-dẫn-cli--bộ-công-cụ-benchmark--chẩn-đoán)
11. [Bảng tra cứu cấu hình `.env`](#bảng-tra-cứu-cấu-hình-env)
12. [Nguyên tắc vận hành & An toàn dữ liệu](#nguyên-tắc-vận-hành--an-toàn-dữ-liệu)

---

## Thư mục duy nhất

Bản chính thức duy nhất nằm tại thư mục: **`D:\duanlaylink`**.

- Toàn bộ mã nguồn, cơ sở dữ liệu (`storage/app.db`), các dự án (`storage/projects`) và công cụ hỗ trợ (`tools/`, `storage/models/`) tập trung tại đây.
- Sử dụng shortcut **DuanLayLink** trên Desktop hoặc chạy file `start_server.bat` trong thư mục này.

Các lệnh bên dưới chạy từ thư mục gốc của kho mã.

README mô tả mã nguồn hiện tại, rà soát ngày **15/11/2026**. Các giá trị trong bảng cấu hình là mặc định của `config/settings.py`, không phải bản chụp `.env` trên máy. Biến môi trường, `.env`, launcher và thiết lập đã lưu của từng dự án có thể làm giá trị thực tế khác bảng.

## Mở ứng dụng

Logo NpzzSub từ ảnh thương hiệu dùng chung cho biểu tượng tab, đầu trang Studio và lớp logo xem trước/xuất video. Biểu tượng tab, logo đầu trang, logo xem trước và logo video đều hiển thị hình tròn. Để áp dụng logo mới cho MP4 đã xuất, mở dự án và chạy lại bước Xuất video.

### 1. Khởi động nhanh (Khuyên dùng)

1. Nhấp đúp vào file **`start_server.bat`** (hoặc chạy qua phím tắt ngoài Desktop).
2. Đợi launcher tự động kiểm tra cổng và trình duyệt mở **http://127.0.0.1:9000/**.
3. Chọn dự án cũ để tiếp tục hoặc dán link video (Douyin, Bilibili) / đường dẫn tệp trên máy (ví dụ `D:\Video\phim.mp4`) để tạo dự án mới.
4. Giữ cửa sổ dòng lệnh mở trong suốt quá trình xử lý. Khi không còn tác vụ nào chạy, nhấn **Ctrl+C** để đóng server an toàn.

### 2. Tùy chọn nâng cao khi mở Studio

- **Đổi cổng khởi động:**
  ```cmd
  start_server.bat 9000
  ```
- **Chạy hoàn toàn ngoại tuyến (Offline Mode):**
  Khi bật cờ này, ứng dụng chỉ dùng các model cục bộ trên máy (HY-MT dịch thuật, Piper/VieNeu đọc tiếng, Whisper nhận dạng giọng nói; không gọi API ngoại mạng, không tự tải thêm model):
  ```powershell
  python scripts/run_personal.py --offline
  ```
  Chế độ offline không tải được video từ liên kết mạng; dùng tệp video đã có trên máy. Các model/runtime cần được cài sẵn. Thiếu model thì công đoạn tương ứng có thể báo lỗi hoặc cần thao tác thủ công.
- **Chế độ máy chủ mạng LAN (Small Server):**
  Khi cần mở giao diện cho các thiết bị khác trong cùng mạng nội bộ truy cập:
  ```cmd
  start_small_server.bat
  ```
  Đây là chế độ riêng, mặc định dùng cổng **8088**. Đặt `PERSONAL_MODE=false` và `OFFLINE_MODE=false` trong cấu hình để chế độ server có hiệu lực. Lần đầu mở `http://127.0.0.1:8088/` trên chính máy chủ để tạo quản trị viên; sau đó dừng và chạy lại launcher để mở LAN. Dùng địa chỉ LAN mà launcher in ra. Chỉ mở cổng tường lửa trên mạng riêng tin cậy.

Các ví dụ `python ...` yêu cầu chạy bằng môi trường Python đã cài các dependency của dự án. `start_server.bat` ưu tiên `.venv`, sau đó `venv`, rồi mới dùng `python` trên PATH. Riêng launcher cá nhân luôn đặt `PERSONAL_MODE=true`, `SERVER_MODE=false`, và đặt `OFFLINE_MODE` theo cờ `--offline`.

### 3. Quy trình nạp mã mới sau cập nhật

- Đợi các tác vụ đang chạy kết thúc, nhấn **Ctrl+C** đóng cửa sổ server cũ.
- Chạy lại **`start_server.bat`**. Launcher đối chiếu phiên bản backend từ API sức khỏe với phiên bản mà launcher yêu cầu. Dùng cùng địa chỉ Studio cho các dự án tiếp theo.
- Mở lại trình duyệt và nhấn **Ctrl+F5** (hoặc Ctrl+Shift+R) một lần để nạp mã JavaScript/CSS mới nhất.

---

## Quản lý dự án trên Bảng điều khiển

Tại màn hình **Tổng quan (Dashboard)**:

- **Tìm kiếm và lọc trạng thái:** Tìm nhanh theo tên hoặc mã dự án (hỗ trợ gõ tiếng Việt không dấu), kết hợp bộ lọc 9 trạng thái công đoạn. Danh sách hỗ trợ phân trang 10 / 20 / 50 mục. Ô tìm kiếm ở thanh đầu trang đồng bộ kết quả trực tiếp với bảng.
- **Thẻ việc cần chú ý:** Tự động hiển thị nút bấm dẫn thẳng vào bước đang xử lý hoặc bước tiếp theo của từng dự án: dự án đã qua QA mở **Nghiệm thu**, dự án đã tạo giọng mở **Render**, dự án đang ở bước OCR mở **Phụ đề gốc**. Số lượng câu nghi vấn cần kiểm tra được hiển thị riêng biệt.
- **Đồng hồ thời gian thực tế:** Bảng tự động cập nhật mỗi 5 giây. Bấm mũi tên cạnh tên dự án để xem các thông số đo lường:
  - **Xử lý thực:** Tổng thời gian các tiến trình công việc thực sự chạy.
  - **Chờ / gián đoạn:** Thời gian xếp hàng, chờ người dùng duyệt bài hoặc tạm dừng chủ động.
  - **Chạy lỗi:** Ghi lại thời gian chạy thất bại để bạn nắm rõ điểm nghẽn.
- **Hủy tác vụ:** Trong Studio, bấm **Hủy tác vụ** và đợi trạng thái **Đã hủy**. Video nguồn và dữ liệu dự án vẫn có thể dùng để tiếp tục.
- **Xóa dự án:** Trong Studio hoặc Tổng quan, mở hộp thoại xác nhận và kiểm tra danh sách trước khi bấm **Xóa vĩnh viễn**. Dự án đang xếp hàng, đang chạy hoặc đang dừng phải đợi tác vụ kết thúc. Nếu tệp đang mở, Studio báo lỗi và giữ dự án trong danh sách để thử lại.

---

## Quy trình làm video

### 9 Bước tiêu chuẩn trong Studio

1. **Nguồn:** Nhập liên kết (Douyin, Bilibili) hoặc đường dẫn tệp video máy tính; kiểm tra tiêu đề, thời lượng và thông tin nguồn.
2. **Tải Media:** Tải luồng video/âm thanh mà nguồn cung cấp, ghép và kiểm định media. Bilibili ưu tiên chất lượng 1080p theo cấu hình khi nguồn và quyền tài khoản cho phép; nhánh DASH có thể tải hình/tiếng riêng. Tải theo byte range và nối tiếp chỉ dùng khi máy chủ nguồn hỗ trợ và dữ liệu kiểm tra hợp lệ. MP4 thành phẩm được mã hóa H.264/AAC.
3. **Vùng ROI:** Kéo chuột khoanh vùng phụ đề tiếng Trung trên khung hình xem trước, hoặc chọn **Tự động dò Sub** / **Preset Đáy**. Nếu video không có phụ đề cứng, chọn bỏ qua ROI để chuyển sang nghe giọng nói.
4. **Bóc phụ đề / Lời thoại:** Bóc chữ phụ đề bằng RapidOCR (kèm VideoSubFinder) hoặc nhận dạng lời thoại bằng Whisper nếu video không có sub cứng. Duyệt các câu nghi vấn trước khi dịch.
5. **Dịch tiếng Việt:** Dịch nghĩa bằng Gemini batch kèm cơ chế sliding context, bộ nhớ cache và chia lô song song với HY-MT / Azure.
6. **Che & Sub Việt:** Chọn kiểu mờ tự nhiên hoặc hộp che sub gốc, chọn mẫu chữ, màu sắc, cỡ chữ và vị trí hiển thị phụ đề Việt.
7. **Tạo giọng đọc:** Chọn giọng đọc AI, tốc độ, căn timeline theo video với cơ chế `cluster-budget-v1` và kiểm tra độ trôi âm thanh.
8. **Render:** Kết xuất video thành phẩm 1-pass bằng GPU NVENC (hoặc CPU dự phòng), ghép phụ đề và hòa trộn âm thanh.
9. **Nghiệm thu QA:** Kiểm tra các chỉ số kỹ thuật tự động (độ dài video, độ to âm thanh LUFS, đỉnh âm lượng TruePeak, độ trôi phụ đề) và nghe/xem lại video hoàn chỉnh.

Khi mục tiêu có lồng tiếng, Auto dừng ở **Tạo giọng đọc** nếu còn câu lệch timing. Xem các câu được đánh dấu, sửa lời hoặc thiết lập giọng rồi tiếp tục Auto. Chọn mục tiêu chỉ làm phụ đề nếu bạn muốn xuất bản giữ tiếng gốc.

### Lựa chọn Mục tiêu video (Video Goal)

Trong **Dự án mới**, chọn **Thành phẩm** rồi chọn **Nguồn để dịch**. Dùng **Đọc chữ trên hình** cho video có chữ Trung rõ, hoặc **Nghe lời thoại** cho video không có phụ đề rõ. **Chỉ lồng tiếng Việt · giữ hình gốc** dùng chữ Trung trên hình. Các mục Tùy chỉnh và Hybrid vẫn có trong Studio. Bước 8 có mô tả riêng cho từng chế độ mã hóa.

Ở màn hình tạo dự án mới hoặc trong Studio, hãy chọn mục tiêu phù hợp với nội dung:

- **Chữ trên hình → sub Việt (`screen_subtitles`):** Đọc phụ đề cứng bằng OCR, dịch và ghép sub Việt, dùng âm thanh nguồn và bỏ qua tạo giọng. AAC vẫn qua kiểm định và có thể được hiệu chỉnh.
- **Lời nói → sub Việt (`speech_subtitles`):** Whisper nghe âm thanh gốc, dịch và đặt sub Việt ở dưới; không quét OCR, giữ tiếng gốc, không gọi TTS.
- **Chữ trên hình → giọng + sub (`screen_dub`):** OCR phụ đề cứng, dịch sang tiếng Việt, tạo giọng đọc AI lồng tiếng và trộn cùng âm thanh nền.
- **Chỉ lồng tiếng Việt · giữ chữ Trung (`screen_voice_only`):** Dán link, chọn **Chỉ lồng tiếng Việt · giữ hình gốc** ở màn hình Dự án mới và bấm **Bắt đầu xử lý tự động**. Studio đọc chữ Trung trên hình, dịch để tạo giọng, rồi xuất MP4 giữ chữ gốc và trộn giọng Việt với nền. Chọn vị trí hoặc tắt logo NpzzSub ở phần xem trước nguồn. Bản dịch vẫn có trong Studio để duyệt. Tải MP4 ở tab **Nghiệm thu**.
- **Lời nói → giọng + sub (`speech_dub`):** Whisper nghe lời thoại tiếng Trung, dịch, tạo giọng đọc AI và hòa trộn âm thanh.
- **Hybrid thử nghiệm: đối chiếu lời nhạc / thoại (`hybrid_dub`):** Dùng VAD để hỗ trợ chọn ứng viên rồi đối chiếu ảnh/tiếng bằng hai model. Chỉ chuyển cue OCR sang phụ đề không giọng khi đủ bằng chứng đó là lời hát và không có thoại nói. VAD không tự phân biệt chắc chắn lời hát với lời thoại; cue chưa rõ hoặc lỗi provider vẫn được giữ để xử lý.
- **Tùy chỉnh (`custom`):** Tự do lựa chọn từng công đoạn bóc tách và âm thanh theo ý muốn.

### Cấu hình Tốc độ xử lý / Chất lượng hình

- **Nhanh (`fast`):** NVENC p4 / CQ 22 (CPU dự phòng: veryfast / CRF 21).
- **Cân bằng (`balanced` - Mặc định trên giao diện tạo dự án):** NVENC p5 / CQ 20 (CPU dự phòng: fast / CRF 20).
- **Chất lượng hình cao (`quality`):** NVENC p6 / CQ 18 (CPU dự phòng: medium / CRF 18).

*Lưu ý:* Cả 3 mức profile trên chỉ thay đổi tham số nén video của FFmpeg, giữ nguyên 100% độ khắt khe của các bước quét OCR, duyệt nghĩa, tạo giọng và kiểm định QA kỹ thuật.

API không truyền `processing_profile` có thể giữ preset đã lưu; `RenderConfig` mới mặc định `p4`. Chế độ giữ hình gốc vẫn có thể mã hóa lại hình và hiệu chỉnh AAC; “giữ hình gốc” chỉ việc giữ chữ/nội dung hình nguồn, không cam kết sao chép nguyên luồng video hoặc giữ nguyên từng byte.

---

## Bóc tách phụ đề và kiểm tra nguồn

### 1. Quét chữ và bóc tách hardsub

- Tệp ảnh nguồn thô dùng chung giữa hai nhánh đối chiếu có trần `OCR_SOURCE_SPOOL_MAX_BYTES` mặc định 2 GiB, đồng thời chừa khoảng trống dự phòng ổ đĩa. Nếu tổng ảnh ước tính vượt trần (hoặc ảnh thực lớn hơn dự toán), dùng các lô nhỏ hiện có thay cho giữ toàn bộ ảnh nguồn. Không bỏ cue/ảnh đối chiếu, không giảm số lượt đồng thuận. Đổi lại có thể phải giải mã lại một số khung giữa hai nhánh để tránh đầy ổ.

- Bộ lọc proxy dùng `VSF_PROXY_FILTER_THREADS=1` mặc định để giới hạn tranh CPU trên máy ít lõi; vẫn giữ bộ lọc `scale`, FPS và tham số mã hóa. Có thể cấu hình 1–16 luồng sau khi đo trên máy thực. Proxy hoàn tất có manifest riêng gắn nguồn/phạm vi/cấu hình/binary FFmpeg và SHA-256 đầu ra. Khi hủy hoặc thiếu tài nguyên trong lúc quét, lần sau dùng lại proxy chỉ nếu identity và checksum khớp; tệp dở dang không được dùng lại. Quét chưa hoàn tất vẫn phải quét lại, không coi ảnh đang sinh là checkpoint hoàn thành. Proxy được dọn sau lượt thành công hoặc khi đã có manifest quét hoàn tất hợp lệ.
- Nếu worker RapidOCR GPU lỗi giữa lượt đọc ảnh VSF, giữ các cue/ảnh đã nhận dạng hợp lệ và chuyển ảnh đang lỗi cùng phần còn lại sang CPU; không bắt CPU đọc lại toàn bộ tiền tố. Cache kết quả CPU không được ghi dưới identity GPU. Báo cáo `rapidocr_pass.json` ghi số ảnh thực nhận dạng theo engine và số lần chuyển dự phòng. Thiếu RAM/VRAM/commit của Windows vẫn có thể làm worker lỗi; việc phục hồi không phải cam kết GPU luôn dùng được.

- Studio chọn vùng phụ đề (ROI). Khi bật `VSF_USE_SCAN_PROXY` và nguồn rộng hơn `VSF_PROXY_WIDTH` (mặc định 960 px), FFmpeg giải mã và thu nhỏ bằng bộ lọc `scale` để giữ điểm ảnh dùng cho OCR. FFmpeg ưu tiên `h264_nvenc -preset p1` sau khi thử bộ mã hóa thực tế, rồi dùng `libx264 -preset ultrafast -crf 28` khi NVENC không dùng được. Proxy bỏ tiếng và không đặt tham số giảm FPS. Nếu không tạo được proxy, bộ quét dùng video gốc. MP4 thành phẩm lấy hình từ nguồn.
- Lượt quét đã hoàn tất có thể dùng lại trong cùng dự án khi nguồn, mốc cắt, ROI, phiên bản bộ quét, thiết lập và số ảnh khớp manifest. Tạo dự án mới có thể phải quét lại cùng video. Đối chiếu ảnh nguồn xét toàn bộ cue đủ điều kiện và ghi checkpoint theo lô 12 câu. Nhánh Trung/Anh và nhánh vùng chữ chung dùng chung một lịch đọc theo chỉ số khung hình tăng dần. Tệp tạm chỉ giữ điểm ảnh vùng phụ đề phục vụ cả hai nhánh và được đóng sau đối chiếu. Chỉ dùng lại checkpoint khi nguồn, ROI, bộ nhận dạng, phiên bản và nội dung cue khớp; các kết quả đã hoàn tất trong lượt bị ngắt có thể tiếp tục.
- RapidOCR đọc từng ảnh đã quét và tắt phân loại xoay chữ 180° cho quy trình phụ đề ngang. Ảnh liền kề giống hệt có thể kéo dài cue hiện có trong cửa sổ thời gian cho phép. Cụm ký tự Latinh ngắn, lặp, nằm trong một hộp nhỏ và chỉ đọc được trên ảnh thô có thể được gắn cờ duyệt thay vì đưa thẳng vào dịch. Cache OCR dùng chung theo nội dung ảnh, ngôn ngữ và phiên bản bộ nhận dạng.
- `OCR_DEVICE=auto` chọn runtime CUDA đã kiểm định khi khả dụng. Khi GPU gặp lỗi được nhận diện trong lúc đọc lại ảnh đối chiếu, Studio thử lại ảnh đó bằng CPU.
- Studio hiện tên pha và tiến độ Bước 4/9 cho proxy, VideoSubFinder, RapidOCR, đối chiếu đa khung và từng cửa sổ âm thanh. Hồ sơ `OCR_READY` liên kết các báo cáo có sẵn của lượt chạy. `vsf_proxy` ghi thời gian và đường mã hóa thực tế, `vsf_process` ghi cờ quét và số ảnh, còn `rapidocr_pass` ghi thời gian, số ảnh, lượt lọc trùng và cache.

### 2. Kiểm định nguồn đa góc nhìn (Source Consensus)

Điểm tin cậy OCR cao không đồng nghĩa đó là lời thoại; biển hiệu quảng cáo, tên phim, logo kênh, watermark (`原创@...`) có thể được nhận dạng rất nét. Hệ thống áp dụng các lớp phòng vệ:

- **Đối chiếu 6 góc nhìn (Six-view Consensus):** Mỗi câu nghi vấn được đối chiếu tại 3 khung hình độc lập (25%, 50%, 75% thời lượng cue) nhân với 2 cách xử lý ảnh (ảnh gốc và ảnh tăng tương phản); cả 6 lượt đọc đều phải khớp cùng nội dung với độ tin cậy $\ge 90$ mới tự động xác nhận.
- **Tách chữ Latinh/ASCII trên áo hoặc vật thể:** Khi hình ảnh có lẫn chữ in áo/vật cảnh với phụ đề tiếng Trung, hệ thống chỉ tách chữ Latinh khi có đủ 6 lượt đọc OCR $\ge 90$ xác nhận cùng dòng chính và bố cục chữ phụ phù hợp; khoảng trắng được chuẩn hóa nhưng bảo vệ toàn bộ chữ Hán và chữ số.
- **Nhánh glyph trắng cho phụ đề ngắn:** Đối với các phụ đề ngắn 2–4 chữ CJK có viền hoặc nền phức tạp, bộ lọc ngưỡng màu trắng cố định (threshold 170 + 3x resize) hỗ trợ bóc tách chính xác mà không làm mất dấu phủ định.
- **Chú thích tiếng cười không lời (`caption_role=nonverbal_laughter`):** Những câu phụ đề chỉ gồm các âm cười lặp (ví dụ `嘿嘿嘿`, `哈哈哈`) sau khi qua kiểm định nguồn sẽ được gắn nhãn này. Hệ thống giữ phụ đề trên video, không sinh giọng đọc TTS nhân tạo và không hạ âm lượng gốc tại đoạn đó.

### 3. Đối chiếu chữ với âm thanh gốc tiếng Trung (STT Recheck)

- Khi bật `OCR_AUDIO_RECHECK`, FFmpeg giải mã một tệp PCM tạm với `STT_AUDIO_FILTER`. Studio cắt từng cửa sổ từ PCM và đối chiếu những cue còn nghi vấn. Model lấy từ `OCR_AUDIO_MODEL_DIR` nếu được cấu hình, từ bản large-v3-turbo cục bộ đã kiểm định nếu có, hoặc từ bản small trên máy.
- Mỗi ứng viên được đối chiếu với ba ảnh lấy ở 25%, 50% và 75% thời lượng câu, mỗi ảnh đọc theo hai cách tiền xử lý. Các câu thiếu bằng chứng vẫn cần duyệt. Nếu model nghe không nạp được, báo cáo ghi trạng thái không khả dụng và giữ các câu chưa xác minh.
- Studio giải phóng worker OCR hoặc session ONNX của nhánh CPU trước pha nghe. Khi Whisper cho phép CUDA, Studio kiểm tra VRAM còn trống và kiểu tính toán của runtime (tối thiểu 3.500 MiB cho model lớn hoặc 2.000 MiB cho small). Với CPU, nếu còn dưới 4 GiB RAM vật lý hoặc không đọc được dung lượng, Studio chọn small trước khi nạp model lớn. CPU dùng `int8` với bốn luồng. Một tiến trình giữ model suốt lô cửa sổ, dùng khối encoder tối đa 10 giây, `beam_size=1`, `best_of=1`, `temperature=0.0` và mốc từng từ. Nếu CUDA lỗi hoặc model CPU được cấu hình thiếu bộ nhớ, thử small CPU đã có trên máy. Báo cáo ghi tên model và thiết bị thực tế. Các cửa sổ âm thanh gần nhau được dùng chung khi tổng thời lượng không quá 15 giây; cửa sổ dài được chia có đệm chồng. Các kết quả nghe được lưu theo từng cửa sổ trong khi worker hoạt động, và chỉ tái sử dụng khi nguồn, cửa sổ PCM, model, runtime, mã nhận dạng và thiết lập khớp. Lỗi một cửa sổ của model dự phòng giữ cue đó chờ xử lý và tiếp tục các cue khác. Đối chiếu cắt lời theo mốc từng cue và chọn dòng CJK ổn định, tách khỏi hộp chữ phụ. Lời khớp cần đạt độ tin cậy trung bình của chuỗi và độ giao thời gian. Nếu sáu lượt OCR cùng mặt chữ và khớp chính xác lời có mốc từng từ, chấp nhận OCR từ 78 điểm; so khớp Pinyin vẫn yêu cầu 90 điểm. So chữ Hán và âm tiết Pinyin không dấu theo thứ tự, có thể thiếu tối đa hai âm ở mép câu. Ngưỡng Pinyin là 75%, có chữ trùng làm điểm tựa và giữ nguyên phủ định, số, đơn vị, lượng từ cùng cụm đầu có dấu hiệu tên riêng. Khi bật đối chiếu tiếng, chỉ các cue còn chờ duyệt được bàn giao ba khung, mỗi khung đọc hai cách trên toàn ROI, kể cả khi vùng dòng chính chưa ổn định. Kiểm tra ảnh vẫn cần đủ sáu lượt đồng thuận để xác nhận câu. Bước nghe dùng lại checkpoint cùng nguồn, ROI, bộ nhận dạng và mốc cue, chỉ mở video nếu thiếu bằng chứng. Đoạn ASCII ngắn hoặc nhiễu hộp chữ nhỏ chỉ có trên ảnh thô được loại với mã `non_cjk_scene_texture_noise` sau một lượt nghe thành công và sáu góc nhìn, trừ trường hợp cả sáu lượt đạt 92 điểm và lời nói khớp. Đoạn CJK không có lời độc lập và thiếu dòng chữ chính ổn định có thể được loại với mã `one_off_no_speech_noise`. Mảnh chuyển cảnh còn được kiểm tra theo mốc các câu liền kề và chữ lặp. Chữ số hoặc ASCII ở rìa được tách khi lời nói có mốc khớp phần lõi và ảnh nguồn xác nhận đúng phần lõi. Đầu câu kèm đơn vị được bảo vệ. Với dòng CJK ổn định trong vùng phụ đề, đối chiếu có thể dùng bốn trên sáu lượt, bao gồm cả hai lượt giữa câu và trung bình từ 85 điểm. Khớp từng chữ, Pinyin hoặc chuỗi con liên tiếp hỗ trợ xác nhận khi có bằng chứng hình ảnh và số, phủ định nhất quán. Thán từ một chữ được xác nhận khi lời có mốc khớp, hoặc loại khi thiếu lời độc lập. Cue không đủ bằng chứng xác nhận hoặc loại vẫn xuất hiện ở bước duyệt. Cue loại có bằng chứng ghi mã lý do vào báo cáo QA. Text phục hồi được ghi vào corrected_text để Bước 5 dùng đúng dòng thoại. Báo cáo `audio_recheck/report.json` tách `speech_phase_seconds` và `visual_phase_seconds`, ghi số cửa sổ (`speech_windows`), số tiến trình nghe (`speech_processes`) và số cache có sẵn trước lượt chạy (`speech_preexisting_cache_hits`).

### 4. Bộ nhớ học OCR (OCR Learning)

Thư mục `learning/` lưu trữ các luật suy luận (`rules.md`) và nhật ký sửa lỗi trích xuất từ database (`corrections.jsonl`). Bạn có thể chạy công cụ quản lý:

```powershell
python scripts/ocr_learning.py inventory
python scripts/ocr_learning.py analyze --project MA_DU_AN --evidence-limit 3
python scripts/ocr_learning.py replay
```

---

## Điều phối dịch thuật đa nguồn & Quản lý API Key

### 1. Cơ chế chia lô song song (`parallel-provider-batches-v1`)

Quy trình dịch tự động tối ưu hóa tốc độ và giảm áp lực quota:

- **Model chính:** Gemini (mặc định `gemini-3.5-flash-lite`; dự phòng `gemini-3.1-flash-lite`, `gemini-3.8-flash`, `gemini-3.6-flash`).
- **Model sửa lỗi nghĩa / vượt token:** `gemini-3.8-flash`.
- **Chia lô song song khi có từ 20 câu đại diện khác nhau cần dịch:** Khi bật điều phối đa nguồn và provider đã khả dụng, hệ thống tách một lô thử tối đa 4 câu cho HY-MT cục bộ (`http://127.0.0.1:8091/v1`), một lô thử tối đa 16 câu cho Azure Translator, trong khi Gemini dịch các lô còn lại. Câu trùng được gom trước khi tính ngưỡng này. Launcher cá nhân có thể khởi động HY-MT khi đủ runtime/model cục bộ.
- **Bảo toàn bộ nhớ dịch (`TranslationMemory`):** Khi một nguồn gặp lỗi mạng hoặc hết hạn mức giữa chừng, các câu đã dịch hợp lệ lập tức được ghi vào bộ nhớ đệm. Bản dịch hợp lệ của từng lô, kể cả lô lấy từ cache, được commit ngay vào SQLite và cập nhật checkpoint. Lượt bị ngắt tiếp tục từ các cue còn thiếu; lỗi ghi database được trả ra rõ ràng. Chỉ những câu chưa xong mới được chuyển giao cho nhà cung cấp dự phòng tiếp theo hoặc quay lại Gemini.

- **Cứu câu dịch lỗi khi Auto bật tự chốt:** Sau các lượt sửa giới hạn, thử làm sạch chuỗi Việt còn lẫn chữ Hán, dịch riêng cue và từ vựng dự phòng cho câu ngắn. Mỗi bản được cứu có mã lý do và cảnh báo kiểm nghĩa. Nguồn chỉ còn ký hiệu rác có thể được loại với `untranslatable_ocr_noise` và báo cáo theo SHA-256 nguồn. Ký tự CJK ở câu trả về là lỗi parser `invalid_translation`; bộ kiểm tra nghĩa nhận diện riêng câu hỏi cuối 不/没 và A不A.
- **Theo dõi phản hồi cần sửa:** Auto ghi phản hồi mỗi lượt vào `translation_attempts` trong dự án để phân tích lỗi cấp câu. Nhật ký API chung vẫn chỉ lưu trạng thái, token và thời gian.

### 2. Quản lý Pool API Key & Dự án Google Cloud

Trong cửa sổ **Quản lý API Key**:

- Mỗi model có nhãn trạng thái riêng: phân biệt rõ giữa hết quota ngày (429), giới hạn số lượt gọi/phút (RPM), và lỗi máy chủ tạm thời (503/504), kèm thời gian tự động mở khóa.
- Nút **Project ID** cho phép gán mã Google Cloud Project cho từng key để chia sẻ hạn mức chính xác theo cụm.
- Giới hạn thời gian chia nhỏ lô: Các yêu cầu dịch có thể chia nhỏ (splittable batch) được khống chế trần thời gian 30 giây để đảm bảo nếu cần chia tách thì các batch con vẫn còn đủ thời gian xử lý trong tổng deadline.

---

Ngân sách lời đọc Bước 5 hiệu chỉnh theo profile giọng truyền vào (`VOICE_SPEECH_RATES`: ví dụ `piper:ngochuyen` = `4,45`, `piper:banmai` = `4,15`, `piper:vais1000` = `3,85` nhân hệ số tốc độ, fallback `4,0` kẹp `3,6–5,8` đơn vị/giây) và trừ độ trễ dấu câu / nhóm chữ số trước khi phân bổ nhịp nói. Cue dưới 2 giây nối câu kế tiếp cách dưới 0,15 giây dùng biên an toàn 0,82; các cue khác dùng 0,90. Các mảnh OCR ngắn liền kề `0,3–0,85s` trong cùng một câu (không cắt qua dấu kết câu, người nói hoặc phủ định độc lập) được gộp thông minh bằng `coalesce_short_cues` trước lượt dịch. Cache dịch phân biệt ngân sách và profile giọng nói này.

Bước 7 giữ lại các đề xuất đã kiểm nghĩa thành công nếu nhóm sau lỗi provider; câu chưa xác minh giữ nguyên. Pha phục hồi timing tự động có ngân sách chung `min(600, max(90, thời_lượng_nguồn × 1.5))` giây, bắt đầu khi phát hiện lịch chưa đạt. Hết ngân sách sẽ yêu cầu duyệt tay, không render vượt QA. Không thử tiếp rút gọn cùng đầu vào khi toàn bộ đề xuất bị precheck loại. Các ngưỡng QA và trần tốc độ 1,30x giữ nguyên.

## Tạo giọng đọc & Căn chỉnh Timing Bước 7

### 1. Các bộ đọc AI tích hợp sẵn

Ứng dụng hỗ trợ đa dạng cả giọng đọc offline tốc độ cao và giọng online:

- **Piper TTS (Offline ONNX Runtime; bộ đọc mặc định):**
  - `piper:ngochuyen`: Ngọc Huyền (Nữ Review Phim; giọng mặc định trong cấu hình).
  - `piper:banmai`: Ban Mai (Nữ Miền Bắc thanh thoát).
  - `piper:lacphi`: Lạc Phi (Nam Trầm Kiếm Hiệp cổ trang).
  - `piper:chieuthanh`: Chiêu Thanh (Nam Miền Nam ấm áp).
  - `piper:manhdung`: Mạnh Dũng (Nam Phóng Sự tin tức).
  - `piper:vais1000`: VAIS 1000 (Chuẩn Ngữ Âm).
- **VieNeu TTS (`vieneu:ngoc_huyen_review_phim`):** Chạy GPU NVIDIA CUDA FP32 độc lập (`tools/vieneu-gpu`) hoặc CPU. Mặc định giữ `VIENEU_BATCH_SIZE=1` để đảm bảo ngữ điệu và nhịp ngắt chuẩn xác.
- **Edge-TTS (Online miễn phí):** `vi-VN-NamMinhNeural` (Nam Minh), `vi-VN-HoaiMyNeural` (Hoài My).

### 2. Cắt quãng lặng tự nhiên & Xử lý âm thanh

- `TTS_TRIM_SILENCE=true`: Tự động loại bỏ khoảng lặng thừa ở đầu và đuôi câu do engine TTS sinh ra.
- Timeline dùng ngưỡng RMS để cắt khoảng lặng, rồi chỉ cắt phần đệm PCM bằng 0 còn thừa ở hai mép; `TTS_TRIM_KEEP_MS=60` quy định đệm giữ lại ở bước PCM. Nhánh Edge-TTS còn có bước cắt theo biên độ trước khi tạo timeline. Cần nghe lại đầu/đuôi câu để phát hiện âm bị cụt.

### 3. Thuật toán căn giọng toàn cục Bước 7 (`cluster-budget-v1`)

Hệ thống căn lời Việt theo mốc cue và kiểm tra giới hạn timing; mức tự nhiên vẫn cần nghe lại:

- **Xếp lịch theo mẫu đo thực trước (Schedule First):** Hệ thống tạo file WAV theo giọng và tốc độ đã chọn trước, đo đạc độ dài mẫu thật rồi giải bài toán xếp lịch toàn cục nhằm tối thiểu hóa độ trôi (hard gate: độ trôi trung bình $\le 0,3\text{ s}$, độ trôi tối đa $\le 1,5\text{ s}$, khoảng nghỉ tối thiểu $40\text{ ms}$).
- **Bậc tăng tốc lượng tử hóa:** Chỉ tăng tốc từng phần khi thực sự cần thiết, so sánh trực tiếp các tỷ lệ tốc độ lượng tử hóa (trần tốc độ tối đa không quá $1,30\times$).
- **Phục hồi lịch:** Bộ xếp lịch theo cụm dùng khoảng lặng hoặc cửa sổ cùng người nói khi có bằng chứng phù hợp. Với chế độ Trộn có mức nền từ 0 đến 0,15, bộ xếp lịch WAV có thể thử dịch vị trí câu trong giới hạn QA và thời lượng nguồn. Giới hạn độ lệch dùng mốc gốc của cue.

- **Phục hồi lời quá dài (Timing Recovery):** Thử tối đa 3 chiến lược rút gọn khác nhau (súc tích, đổi cấu trúc, diễn đạt lại). Mọi lượt đều kiểm tra phủ định, số lượng, điều kiện, phạm vi, thuật ngữ, xưng hô và dấu hiệu không chắc chắn, rồi kiểm nghĩa với nguồn và các cue liền kề. Giữ câu hiện tại nếu đề xuất bị loại hoặc chưa xác minh. Khi vẫn không đạt timing, Auto dừng ở bước cần duyệt và giữ checkpoint. Giới hạn drift và tốc độ áp dụng trên WAV đo thực tế.
- **Xếp lịch nhiều nghìn câu:** `minimum_drift_witness` gom tối thiểu 64 câu rồi chỉ tách LP tại biên độc lập được chứng minh bằng cửa sổ đặt giọng thực tế (đã gồm `max_drift` và giới hạn cuối nguồn). Không chỉ dựa vào khoảng cách phụ đề. Trung bình drift vẫn được kiểm tra trên toàn video, không áp riêng từng khối. Mỗi LP con có giới hạn HiGHS 30 giây; solver source-window có 45 giây cho mỗi lượt tối ưu. Hết thời gian mà chưa chứng minh được nghiệm thì báo `solver_unproven`, không coi là đạt QA.
- **Tra khoảng lặng và đo WAV:** Chỉ mục sắp xếp/bisect xử lý cả khoảng lặng chồng hoặc lồng nhau, tránh quét mọi khoảng cho từng câu. Bảng tra ID dùng khi tổng hợp cụm; vòng làm mượt tốc độ kiểm tra tín hiệu hủy từng cặp câu. Số mẫu để xếp lịch lấy từ `sf.info().frames`; kiểm tra âm thanh hữu hạn/không im lặng vẫn đọc theo khối 5 giây. Không bỏ kiểm tra nội dung WAV để đổi lấy tốc độ.
- **Ghép video dài:** Các WAV được lắp vào PCM dạng memmap và ghi theo khối. Hệ thống không tạo dòng lệnh FFmpeg với hàng nghìn đầu vào WAV. Chú thích chỉ có dấu câu không yêu cầu giọng. Thán từ một chữ có WAV giải mã hợp lệ nhưng im lặng được lưu bằng chứng riêng và chuyển thành phụ đề không giọng.

- **Tự động gộp cặp câu OCR liền kề (Adjacent Merge):** Khi có hai câu liền nhau đọc cùng một bản dịch, 2 model Gemini đồng thuận 6 crop ảnh và 1 câu nói, không lệch số/phủ định và vừa vặn timeline WAV, hệ thống sẽ tự động gộp thành một câu hoàn chỉnh.

---

## Che chữ mờ tự nhiên, Mẫu sub & Render video

Thời hạn render, đo loudness, hiệu chỉnh AAC và probe đầu ra co giãn theo công thức `max(timeout_cố_định, int(thời_lượng_nguồn × 2.5) + 300)`. Metadata ban đầu dùng thời hạn nền có đệm khi chưa biết thời lượng. Video 7.200 giây có ít nhất 18.300 giây cho thao tác toàn luồng, video 10.800 giây có ít nhất 27.300 giây.

### 1. Chế độ Mờ tự nhiên theo màu video (`preset:natural_blur`)

Đây là mẫu mặc định trong danh sách mẫu phụ đề Việt.

- Mẫu mờ tự nhiên trong Studio và Auto dùng chung cấu hình từ `RenderConfig` (mặc định `NATURAL_BLUR_PRESET` với mức mờ 24 khi chưa có `subtitle_settings.json` của dự án).
- Phụ đề tiếng Việt chữ Arial trắng in đậm, viền đen sắc nét đặt chồng ngay ngắn trên dải mờ, mang lại cảm giác chuyên nghiệp như video gốc.

### 2. Bộ sưu tập mẫu chữ và kiểu che phong phú

Bạn có thể tùy ý chọn mẫu trong màn hình tạo dự án hoặc tab **Che & Sub Việt**:

- 🟡 **TikTok Vàng Viền Đen (`preset:tiktok_yellow`):** Phông Montserrat Bold nổi bật, cực kỳ hút mắt trên video ngắn.
- ⚪ **Trắng Hiện Đại Viền Đen (`preset:modern_white`):** Phông Montserrat thanh lịch, hiện đại.
- 🎞️ **Review Phim Chuyên Nghiệp (`preset:review_anton`):** Phông Anton dày dặn, phong cách điện ảnh.
- ⚔️ **Phim Cổ Trang / Kiếm Hiệp (`preset:cinematic_svn`):** Phông SVN-Times cổ điển.
- 📷 **Phong Cách Reels / Instagram (`preset:instagram`):** Trẻ trung, năng động.
- 🟡 **Che Tiêu Chuẩn (`cover`):** Hộp màu che kín sub cũ và đè sub mới.
- ✨ **Sub Sạch (`clean`):** Dùng delogo trên vùng chữ gốc rồi chèn phụ đề Việt.
- 🌐 **Phụ Đề Song Ngữ (`bilingual`):** Giữ chữ gốc và chèn phụ đề Việt theo vị trí bạn chọn.

- **Preset nền tảng & Safe-Zone thương mại (`platform_preset`):** `RenderConfig` hỗ trợ `auto`, `ytb_16_9`, `fb_feed` (phụ đề tối đa 1–2 dòng, viền + đổ bóng, lề đáy $\ge 5\%$) và `tiktok_reels_9_16` (đẩy phụ đề và logo khỏi vùng UI đáy $\ge 22\%$, cột nút phải $\ge 15\%$, bề rộng dòng chữ $\le 85\%$ khung hình).
- **Làm sạch đa vùng & LaMa 1-Pass ROI-crop:** `OverlayDetector` / `douyin_logo` dò watermark góc tĩnh/bật tắt và chữ phụ ngoài ROI (`secondary_caption`) kết hợp blur làm mượt viền (`alphamerge`). Khi bật `restore_captions=true`, worker LaMa chỉ xử lý dải ROI cắt gọn (`roi_patch.mp4`) rồi chồng một pass trực tiếp trong lệnh encode chính và giải phóng VRAM trước NVENC.
- **Chuẩn hóa Encode & Keyframe cắt tập:** Video đầu ra chuẩn hóa `-pix_fmt yuv420p`, `-fps_mode cfr`, `-colorspace bt709 -color_primaries bt709 -color_trc bt709`, `-movflags +faststart` và chèn `-force_key_frames` (kèm `-forced-idr 1` khi dùng `h264_nvenc`) khớp các mốc `part_splits` của `publishing_pack` để cắt `-c copy` chuẩn keyframe.
- **Chống Reused Content âm thanh & Platform Compliance Gate:** Âm gốc ở khoảng lặng giữ trần thấp đồng nhất (`bg_gap_ceiling=0.15`, không bật lại `1.0`), hỗ trợ tùy chọn **Vocal-Band Ducking** (`vocal_band_ducking=true`, cắt sâu dải thoại `300–3400 Hz` bằng FFmpeg EQ không tốn VRAM) và hòa âm nhạc nền sạch (`bgm_path`, `bgm_volume`); AI Stem Separation giữ tắt mặc định (`AI_STEM_SEPARATION_DEFAULT=false`). Cổng `src/pipeline/compliance.py` (`COMPLIANCE_GATE_ENABLED=true`) tự động chuẩn hóa từ ngữ nhạy cảm, loại CTA cấm và quét sạch ký tự Hán tự sót trước TTS, render phụ đề và gói đăng kênh.

### 3. Vị trí phụ đề tiếng Việt

Tùy chỉnh linh hoạt: Chồng lên vùng che, Sát phía trên, Sát phía dưới, Sát đáy khung hình, hoặc nhập độ cao tùy chọn (từ 0.0 mép trên đến 1.0 sát đáy).

### 4. Kết xuất Render và kiểm định âm thanh

Studio tạo `mastered_audio.m4a` đã chuẩn hóa loudness (`loudnorm=I=-14:TP=-2:LRA=11` ở cả `AUDIO_PREMASTER`), đo và hiệu chỉnh AAC trước khi encode hình. Lượt render ghép AAC nguyên vẹn bằng `-c:a copy`, tái sử dụng trực tiếp báo cáo `master_delivery` hợp lệ (chỉ đo lại MP4 khi chạy nhánh checkpoint/khôi phục cũ), và tái sử dụng mã SHA-256 giữa `render_checkpoint` và `audio_qc.json`. Các lượt hiệu chỉnh không tạo thêm MP4 lớn thứ hai. Checkpoint encode cũ không dùng lại sau thay đổi audio-first.

Nếu sau các lượt chuẩn hóa, âm lượng đã đạt nhưng true peak của AAC còn vượt −1,5 dBTP, bộ hiệu chỉnh chuyển sang tối đa hai lượt limiter và đo lại AAC thực: encode lại từ đầu vào gốc bằng gain cố định cộng `alimiter` (trần mẫu 0,767361; lượt siết lại dùng 0,691831). Không dùng cách chỉ giảm gain thêm: đo trên dự án chẩn đoán cho thấy AAC vẫn overshoot (−1,28 dBTP sau khi nhắm −1,75), còn hạ trần loudnorm xuống −3,2 làm LUFS rơi khỏi cửa sổ. Ngưỡng −14,5…−13,5 LUFS và ≤−1,5 dBTP không đổi; nhánh thường và nhánh limiter ưu tiên cho video dài dùng cùng cơ chế. Nếu vẫn không đạt, giữ đầu vào và dừng; không xuất nhãn VERIFIED.

Chọn **💾 Tiết kiệm dung lượng** ở Dự án mới hoặc Bước 8 để dùng `compact`: NVENC p5/CQ23 hoặc CPU fast/CRF23, maxrate 4500k, bufsize 9000k. Trần xử lý mặc định 6 giờ, tải 24 GiB; đây không phải cam kết đã nghiệm thu video 6 giờ trên máy 8 GB RAM. Upload server vẫn có giới hạn riêng.

Trước tải và render, Studio kiểm tra chỗ trống trên ổ chứa dữ liệu và chừa 2 GiB dự phòng. Render ước tính theo profile, không bảo đảm bitrate thực tế hay tổng dung lượng mọi công đoạn. DASH còn cần đồng thời các track và MP4 mux; nguồn 24 GiB có thể bị từ chối trên ổ chỉ còn 30 GB. Hãy giải phóng đĩa hoặc chọn đoạn/nguồn nhỏ hơn khi báo thiếu chỗ.

Sau Auto đạt `VERIFIED`, Studio dọn proxy/ảnh VSF tạm, PCM/window nghe và clip giọng trung gian; giữ nguồn, MP4, phụ đề và báo cáo. Muốn sửa giọng/render hoặc nghiệm thu lại sau khi clip bị dọn phải tạo lại giọng. Báo cáo đã lưu mô tả lượt nghiệm thu trước khi dọn, không chứng nhận các tệp đã xóa vẫn còn.

Ở Bước 9, bấm **Đo dung lượng**, rồi **🧹 Giải phóng dung lượng (Giữ Sub & Kịch bản)** và xác nhận để xóa media của riêng dự án. Thao tác giữ bản ghi SQLite, SRT và JSON (gồm kịch bản, gói đăng kênh, báo cáo), không thể hoàn tác tệp đã xóa. Muốn xử lý lại cần tải/tạo lại media. Tệp bị khóa được báo để thử lại; không coi là đã thu hồi thành công.


Render ghép các hiệu ứng hình trong một lượt FFmpeg chính. Bước kiểm định AAC sau mã hóa có thể tạo thêm lượt hiệu chỉnh âm thanh; không bảo đảm toàn bộ xuất video chỉ chạy FFmpeg một lần.

- **Mã hóa phần cứng GPU NVENC:** Hỗ trợ profile p4 / p5 / p6 / compact khi bộ mã hóa chạy được trên GPU/driver hiện có.
- **Tự động chuyển đổi CPU dự phòng:** Khi GPU bận hoặc gặp sự cố, hệ thống tự động chuyển sang mã hóa CPU libx264, giữ nguyên vẹn 100% tọa độ vùng che và phụ đề.
- **Tra cứu thời gian phân đoạn (Interval Lookup):** Biểu thức vùng che được chia nhánh theo các khoảng thời gian để giảm việc xét vùng không hoạt động trên từng frame. Mức tăng tốc phụ thuộc video và số vùng che; không có hệ số tăng tốc chung cho mọi dự án.
- **Ràng buộc mã băm âm thanh thành phẩm:** Báo cáo đo AAC (ngưỡng đạt từ −14,5 đến −13,5 LUFS và true peak không vượt −1,5 dBTP) được liên kết chặt chẽ với mã SHA-256 và `stat_stamp` (`st_dev`, `st_ino`, `st_size`, `st_mtime_ns`) của file MP4; bước QA đối chiếu dấu vết tệp (đối với bản xuất từ 1 MiB trở lên, hoặc kiểm trực tiếp SHA-256 nếu có thay đổi/tệp nhỏ) để tái sử dụng kết quả kiểm tra âm thanh mà không phải đọc/giải mã lặp toàn bộ MP4.

QA đạt nghĩa là các cổng kỹ thuật áp dụng cho lượt xuất đã đạt. Mục **Duyệt nội dung** ở Bước 9 mở danh sách cue và mốc thời gian cho câu rút gọn mạnh, giọng có tốc độ hiệu dụng trên 1,20× và giọng nằm ngoài mốc phụ đề. Báo cáo JSON lưu cùng danh sách tại `content_review_items`. Kiểm lại nguồn ở các mốc được đánh dấu và xem/nghe MP4 trước khi đăng, kể cả khi không có cảnh báo tự động. Báo cáo lưu từ trước có thể chỉ có cảnh báo tổng hợp. Khi lập báo cáo mới, áp dụng điều kiện tạo lại giọng nếu clip trung gian đã được dọn.

---

### 5. Nghiệm thu và gói nội dung đăng kênh

Tab **Nghiệm thu** hiển thị LUFS/TruePeak đo trên AAC thành phẩm (LUFS từ −14,5 đến −13,5; true peak không quá −1,5 dBTP), lỗi và cảnh báo của báo cáo QA. Không có số đo thì hiển thị chưa có báo cáo. Hai nút SRT tách **mốc theo giọng** (`vi.dub.srt`) và **mốc nguồn của bản dịch** (`vi.srt`); tệp chỉ tải được khi công đoạn tương ứng đã sinh ra. Link tải MP4, hai bản SRT Việt, SRT gốc, timeline và báo cáo QA được cập nhật theo dự án ngay khi mở/làm mới Studio, không cần chạy lại QA; chuyển dự án sẽ đổi link theo dự án mới.

Trong khung **Đóng Gói Nội Dung Đăng Kênh**, bấm **Tạo Nội Dung Đăng Bài** để tạo ba tiêu đề tối đa 95 ký tự, mô tả YouTube/Shorts, bài Facebook và hashtag. **Tạo Lại** bỏ qua cache. Các nút Copy sao chép từng phần; nếu clipboard bị chặn, chọn văn bản và sao chép thủ công. Đây là bản nháp, cần kiểm tra nghĩa, quyền sử dụng và lịch đăng trước khi tự đăng lên kênh.

Gói đọc cue từ SQLite qua `orchestrator.get_cues`, bỏ cue tiếng cười không lời. Gemini dùng key pool và ghi usage hiện có; offline, pytest, thiếu key, lỗi provider hoặc JSON không hợp lệ dùng `offline_rule` dựa trên trích đoạn thật, không tự suy ra tập phim/thể loại. Video quá dài vượt ngân sách prompt cũng dùng quy tắc nội bộ. Lịch đăng và thông tin kênh đọc động từ biến `.env` (`PUBLISHING_CHANNEL_NAME`, `PUBLISHING_CHANNEL_HANDLE`, `PUBLISHING_CHANNEL_URL`, `PUBLISHING_SCHEDULE`, `PUBLISHING_HASHTAGS`; mặc định **11:30 & 19:30**, kênh **@NpzzSub Review**); đây là nội dung bản nháp, không phải tác vụ lên lịch đăng.

Chọn **Thời lượng mỗi tập** (2 / 3 / 5 / 10 phút; mặc định 3 phút; API cho phép số nguyên 60–900 giây) rồi tạo gói. `part_splits` chứa mốc bắt đầu/kết thúc, số cue, tiêu đề phần tối đa 95 ký tự kèm `char_count` và lệnh FFmpeg để Copy. Thời lượng là mục tiêu, không phải trần: bộ chia chọn khe gần mục tiêu trong ±45 giây, ưu tiên khe từ 0,15 giây; không có khe phù hợp thì phần có thể dài hơn. Không ép cắt cụm liên tục, tránh phần đuôi dưới 35 giây.

Khoảng bảo vệ là bao của mốc phụ đề Việt hợp lệ và mốc giọng `actual_start..actual_end` trong `voiceover/timeline.json`; hợp nhất vùng chồng nhau trước khi chia. Luôn bỏ row `unplaced`, bỏ mốc không hữu hạn/sai thứ tự, làm tròn biên ra ngoài đến mili giây để không thu hẹp vùng bảo vệ. Thiếu timeline/mốc giọng thì chỉ bảo vệ mốc phụ đề và hiện cảnh báo; timeline tồn tại nhưng JSON hỏng phải được sửa hoặc tạo lại. Đây là khe theo metadata, **không phải phép đo im lặng âm thanh**, và chỉ xét cue đã dịch, không gồm chú thích tiếng cười không lời. Điểm cuối là cuối vùng nội dung hợp lệ, không phải thời lượng toàn bộ MP4; phần đuôi im lặng có thể nằm ngoài kế hoạch.

`youtube_chapters` / `youtube_chapters_text` bắt đầu `00:00`, nhãn trích từ phụ đề thật, không quá 60 ký tự. Các mốc chương tăng ít nhất 10 giây và chỉ dùng giây nguyên còn nằm trong khe an toàn. Nếu các ranh giới phần chưa đủ, chọn thêm khe gần các mốc một phần ba/hai phần ba nội dung. YouTube yêu cầu ít nhất 3 mốc và mỗi chương, kể cả chương cuối, dài ít nhất 10 giây; không đủ thì gói giữ mốc tham khảo và hiện cảnh báo, không tự tạo điểm giữa câu. Xem [hướng dẫn YouTube](https://support.google.com/youtube/answer/9884579?hl=en).

Nút **Copy Mốc Chương**, **Copy Tiêu Đề Phần** và **Copy Lệnh Cắt FFmpeg** chỉ sao chép nội dung. Lệnh `ffmpeg_copy_cmd` chạy từ thư mục dự án có `output/video_vi.mp4` (chế độ server dùng artifact pointer cần tải/copy MP4 thành phẩm về đúng đường dẫn này trước); khi render mới qua `render_one_pass`, hệ thống tự động chèn `-force_key_frames` khớp các mốc `part_splits` (với file đã xuất trước khi có cập nhật hoặc nguồn cắt ngoài luồng này, `-c copy` vẫn phụ thuộc keyframe có sẵn và nên kiểm tra lại đầu/cuối clip). Không có lệnh nào được tự chạy hoặc tự đăng bài.

Artifact `output/publishing_pack.json` ghi nguyên tử, tải qua loại `publishing_pack`. Schema `publishing-pack-v2` kiểm tra cache theo phiên bản/chính sách, dự án, tiêu đề, cấu hình thương hiệu/lịch đăng, nội dung/role/mốc cue, mốc giọng hợp lệ và thời lượng chia mục tiêu; đổi nội dung hoặc thương hiệu làm gói cũ không còn hiển thị/tải qua API. Lỗi ghi tệp hoặc lưu usage vẫn trả lỗi. Không thay đổi model/schema/các cache dịch của Bước 5 hoặc cổng QA video.

API: `GET /api/projects/{id}/publishing-pack?target_part_seconds=180` chỉ đọc, trả `{exists, pack}`; `POST` cùng đường dẫn nhận `{force: false, target_part_seconds: 180}` và trả gói đã tạo. Link tải `files/publishing_pack` cũng kèm `target_part_seconds` để kiểm tra đúng gói. Thao tác tạo yêu cầu dự án rảnh; GET/tải không gọi AI. Tạo gói không đòi MP4 đã QA và không chứng nhận chất lượng MP4.

---

## Khắc phục sự cố, Duyệt thủ công & Tiếp tục Auto

### 1. Chốt nguồn tự động (`AUTO_SKIP_UNCERTAIN=true`)

Auto chốt các cue còn nghi vấn khi có ba khung nguồn, hai lượt OCR trên mỗi khung và kết quả nghe có mốc từ. Bộ trọng tài ưu tiên dòng chữ Hán rõ ở giữa vùng phụ đề, sau đó xét lời Whisper đủ rõ trong mốc riêng, rồi ghi lý do loại các mảnh nhiễu hoặc trùng còn lại. Từng quyết định giữ hay loại có bằng chứng trong báo cáo đối chiếu và lịch sử dự án.

- Cue khôi phục được dịch và tạo giọng. Cue loại theo bằng chứng được tính vào `excluded_cues_with_reason`. Thiếu dữ liệu ảnh hoặc lỗi nghe vẫn được báo để xử lý.
- Tắt tùy chọn để giữ cổng duyệt thủ công. Chạy riêng Bước 4 dùng quy trình đối chiếu thông thường.
- Chế độ làm phụ đề giữ tiếng gốc có thể bỏ qua bản dịch lỗi theo chính sách dịch. Auto lồng tiếng yêu cầu bản dịch và giọng cho các cue thoại còn lại.

### 2. Nối tiếp luồng Auto sau khi duyệt bằng tay

- Khi Auto dừng lại do cần duyệt OCR hoặc sửa câu dịch, bạn chỉ cần chỉnh sửa các câu được đánh dấu rồi bấm **Phê duyệt OCR** hoặc **Phê duyệt bản dịch**.
- Nếu đã xử lý hết câu nghi vấn, ứng dụng sẽ tự động tiếp tục các công đoạn tiếp theo theo đúng thiết lập đã lưu, không cần phải bấm chạy từng bước riêng lẻ.
- Khi Studio hiện **Tiếp tục toàn bộ Auto**, dùng nút đó để chạy tiếp bằng cấu hình đã lưu sau khi xử lý lỗi hoặc hoàn thành phần cần duyệt.

### 3. Theo dõi chi tiết tại Tab Log

Tab **Log** của dự án cung cấp thông tin minh bạch, chính xác từ cơ sở dữ liệu:

- Tải tối đa 200 dòng `JobLog` mới nhất của chính dự án theo thứ tự thời gian.
- Auto đã qua QA ghi công đoạn `VERIFIED` và tiến độ 100% cho job. Sau khi Studio cá nhân khởi động lại, lượt còn ghi `RUNNING` được đánh dấu gián đoạn và giữ trống thời điểm kết thúc nếu không đo được. Một lần chặn quá thời lượng ghi một lỗi `VIDEO_TOO_LONG`.
- Phân biệt rõ 4 thành phần độc lập: **Log tác vụ đã lưu**, **Trạng thái tác vụ hiện tại**, **Mốc công đoạn đã lưu (Checkpoints)**, và **Cảnh báo chất lượng hiện tại**.
- Bảng quan sát bất thường (`pipeline_observations`): Tự động lưu lại các sự kiện có cảnh báo hoặc chạy lỗi vào database để phục vụ công tác rà soát. Xuất báo cáo quan sát:
  ```powershell
  python scripts/report_pipeline_observations.py --output storage/maintenance/observations_YYYYMMDD.json
  ```

### 4. Phòng vệ tệp tin Windows (Atomic File Replace)

- Các đường ghi dùng helper thay tệp nguyên tử thử tối đa 6 lần tổng cộng (lượt đầu và tối đa 5 lượt thử lại) khi gặp lỗi khóa tệp Windows (`WinError 5 / 32 / 33`). Nếu vẫn lỗi, hệ thống trả lỗi thay vì coi dữ liệu đã lưu thành công.

---

## Kho cloud tùy chọn: YouTube Private, Drive và Telegram

**Truyền video lớn:** Drive và YouTube gửi tuần tự từng phần 8 MiB (bội số 256 KiB, phần cuối có thể nhỏ hơn), chỉ giữ một phần dữ liệu đang gửi trong bộ nhớ. Đây là chia yêu cầu truyền; MP4 gốc không bị cắt thành nhiều tập hoặc nén lại để upload. Khi mất kết nối hoặc gặp HTTP 429/500/502/503/504, hệ thống hỏi phiên tải đã nhận đến byte nào rồi gửi tiếp. Tối đa 5 lần phục hồi liên tiếp không tiến triển, với ngân sách 180 giây giữa các lần thử và chờ tăng dần có xét `Retry-After`; một request đang thực hiện vẫn chịu timeout mạng riêng. Hết ngân sách thì giữ phiên để người dùng tiếp tục, không lặp vô hạn. Hủy được kiểm tra giữa các request và trong lúc chờ. Lỗi quyền, checksum, sai phạm vi byte hoặc nguồn thay đổi không được coi là lỗi mạng để thử mù quáng. Drive vẫn kiểm tra kích thước/MD5 từ xa; YouTube cần biên nhận cuối xác nhận Private. Không tự gửi lại file Telegram khi chưa rõ lần gửi trước đã thành công hay chưa.

Chế độ cá nhân cho phép cloud khi `OFFLINE_MODE=false`; chế độ offline chặn cloud. Cloud không tự bật sao lưu hệ thống server. Auto Disk Guard kiểm tra trước khi nhận link Telegram và chỉ tự giải phóng media của dự án đủ điều kiện bên dưới. Trong pytest, transport mạng Google/Telegram và polling nền bị chặn mặc định; các kiểm thử dùng transport giả lập chủ động.

1. Mở **Kho cloud**, nhập Google OAuth JSON loại **Desktop app**, rồi kết nối Drive. Cấu hình/token được mã hóa DPAPI trên Windows; không chia sẻ JSON hay token. Google Cloud project cần bật Drive API và YouTube Data API v3; OAuth ở chế độ thử nghiệm cần thêm tài khoản của bạn làm test user.
2. Chọn **Hồ sơ nhẹ** (mặc định): lưu các SRT có sẵn, `publishing_pack.json`, `verify_report.json` và `audio_qc.json` vào thư mục dự án trên Drive. Gói đăng kênh được tạo khi thiếu hoặc khi phiên bản lưu không còn khớp. Nếu tên nguồn sau khi lọc chỉ còn ký hiệu, nhãn `【AI】` hoặc chữ nối `vs`, gói dùng tên dự phòng **Video thuyết minh Việt**. Mỗi tệp phải khớp MD5/kích thước từ Drive trước khi ghi `output/drive_metadata_archive.json`. Khi lưu lại hồ sơ không đổi, kiểm tra lại checksum từ xa và dùng lại tệp đã lưu. Biên nhận MP4 đầy đủ tại `output/drive_archive.json` được giữ riêng, không bị gói nhẹ ghi đè. Gói này không chứa nguồn hay MP4. Chế độ **MP4 đầy đủ** giữ điều kiện đạt QA và kiểm tra byte gốc như trước; cần đủ dung lượng tài khoản Drive.
3. Ở Bước 9, **Kết nối YouTube** dùng OAuth Desktop đã nhập nhưng scope upload và token được lưu riêng. Chọn tiêu đề rồi bấm **Đẩy Lên YouTube (Private)**. Tiêu đề bị giới hạn 95 ký tự; mô tả gồm chương, tag bỏ dấu #. Mọi upload đều yêu cầu `privacyStatus=private`. Upload theo chunk 8 MiB, lưu phiên mã hóa để tiếp tục sau gián đoạn/hủy. Chạy lại thao tác dùng phiên khớp nguồn/metadata/kết nối; không tự tạo upload mới khi phiên hết hạn để tránh tải trùng. API cho phép `force=true` khi người dùng chủ động muốn tạo bản khác.
4. `output/youtube_upload.json` ghi video ID, liên kết Studio, SHA-256/kích thước MP4 đã gửi và thời điểm YouTube nhận video. YouTube có thể còn đang xử lý; biên nhận không chứng nhận hoàn tất xử lý/phát lại. YouTube mã hóa lại video, không phải kho giữ MP4 gốc khớp byte hay dung lượng vô hạn được bảo đảm. Video dài hơn 15 phút cần tài khoản phù hợp; giới hạn công bố là 256 GB hoặc 12 giờ, tùy mức nào đến trước; quota/chính sách API vẫn áp dụng. Xem [giới hạn YouTube](https://support.google.com/youtube/answer/71673) và [giao thức resumable](https://developers.google.com/youtube/data/guides/using_resumable_upload_protocol).
5. Trước **Giải phóng dung lượng**, hộp xác nhận hiển thị riêng biên nhận YouTube Private và Drive có khớp dữ liệu hiện tại không. Hồ sơ nhẹ không thay bản MP4; biên nhận chỉ phản ánh lần tải đã xác nhận, chưa kiểm tra lại từ xa. Cần tự mở Studio/Drive kiểm tra nếu muốn chắc chắn bản từ xa còn dùng được. Tải cloud lỗi không thay trạng thái QA/VERIFIED của dự án.

### Trợ lý Telegram hai chiều

Mở **Hướng dẫn sử dụng** trên thanh bên để xem 4 thẻ thao tác nhanh: gửi link qua Telegram, duyệt từ xa/xem Demo, Kho cloud và bảo vệ ổ dữ liệu. Hướng dẫn giữ nguyên trong giao diện cá nhân; Telegram và Kho cloud cần Internet, trợ lý chỉ nhận lệnh khi ứng dụng trên máy tính đang mở và không ở chế độ Offline.

1. Trong **Kho cloud → Trợ lý Telegram**, nhập bot token và chat ID dạng số của chat được phép điều khiển, bật Telegram và nhận lệnh hai chiều, rồi **Lưu cấu hình** và **Gửi tin nhắn test**. Để trống hai trường bí mật khi lưu lần sau để giữ giá trị cũ. Cấu hình được mã hóa bằng DPAPI tại `storage/private/telegram_config.json`; API không trả lại token/chat ID. Không gửi token trong hội thoại hoặc chia sẻ tệp cấu hình. Trong server mode chỉ quản trị viên được cấu hình/xem trạng thái này; bot là bộ điều khiển cục bộ được quản trị viên tin cậy, không phải tài khoản nhiều người.
2. Kết nối Drive và YouTube bằng OAuth Desktop. Tài khoản yêu cầu là **nhocnam2111@gmail.com**. Drive đối chiếu email OAuth; OAuth YouTube hiện chỉ cấp quyền upload và không trả email. Mở YouTube Studio kiểm tra tài khoản rồi đánh dấu xác nhận trong Kho cloud. Xác nhận này gắn với `connection_id` hiện tại, phải làm lại sau khi kết nối YouTube mới. Không coi một địa chỉ email ghi trên giao diện là bằng chứng đã đăng nhập tài khoản đó. OAuth Desktop JSON nhập trong Kho cloud là cấu hình chung cho Drive và YouTube; đổi client sẽ yêu cầu kết nối/xác nhận YouTube lại, không dùng token từ project cũ.
3. Gửi link Bilibili (`bilibili.com/video/…`, `b23.tv/…`) hoặc Douyin (`douyin.com/…`, `iesdouyin.com/…`). Bot kiểm tra ổ dữ liệu trước, tạo dự án qua luồng chuẩn rồi xếp hàng Auto: `screen_dub`, `piper:ngochuyen`, `+15%`, `mix`, `skip_uncertain=true`. Khi còn dưới 35 GiB, chọn `compact`; nếu còn dưới ngưỡng an toàn sau khi kiểm tra/dọn, không tạo dự án. Trả mã dự án và nút hủy; tác vụ vẫn chịu các gate nguồn, timing và QA như Studio.
4. `/help`, `/status`, `/list`, “trạng thái ổ D”, “danh sách”, “demo”, “đẩy youtube”, “dọn ổ D”, “hủy” dùng xử lý lệnh cục bộ, không phụ thuộc Gemini. Có nhiều tác vụ thì chọn nút hủy đúng dự án. Demo/upload mặc định chọn dự án VERIFIED còn MP4 cục bộ cập nhật gần nhất trong toàn bộ DB hoặc nhận mã `proj_…` cụ thể; không bị giới hạn bởi danh sách 5 dự án gần đây. Hỏi hội thoại khác dùng Gemini qua key pool, một request với ngân sách 10 giây, lỗi thì trả trạng thái thực. AI chỉ giải thích và đưa nút thao tác, không tự phê duyệt/xóa/upload. Dung lượng từng dự án được đo nền và có thể hiển thị “đang đo”; không quét toàn bộ media trên đường xử lý `/status`.
5. Khi dừng cần duyệt, Telegram phân loại theo **mã lỗi**: OCR có nút tiếp tục theo chính sách bỏ qua/xử lý câu chưa chắc hoặc duyệt nguồn; timing/QA có nút thử Auto và xem MP4 hiện có. “Bỏ qua” dùng lại cơ chế Auto hiện tại, không ép duyệt cue thiếu bằng chứng và không nới QA. Nút duyệt gắn với generation của lượt Auto; nút hủy gắn với task_id thực tế, kể cả tác vụ cloud chưa có hồ sơ Auto. Không dùng nút cũ cho lượt/tác vụ mới. Sau VERIFIED, tự lưu hồ sơ nhẹ nếu Drive đã kết nối đúng tài khoản, rồi gửi các nút Demo, YouTube Private, Drive nhẹ và yêu cầu xóa. Video từ 15 phút có nhắc xem Demo nổi bật. Hồ sơ nhẹ không chứa MP4.
6. Demo là bản trích tối đa 120 giây từ MP4 hiện có, ưu tiên đoạn dày cue theo transcript DB (kể cả transcript rỗng có chủ ý). Không có DB mới dùng timeline. Mã hóa riêng bằng H.264/AAC, chiều rộng tối đa 1280, kiểm tra tệp thực **nhỏ hơn 35 MiB** trước khi `sendVideo`; luôn xóa tệp demo tạm sau gửi hoặc lỗi. Bản demo không thay thế kiểm tra video đầy đủ và không sửa MP4 gốc.
7. Xóa thủ công có hai bước: yêu cầu hiển thị dung lượng, tình trạng biên nhận và phạm vi **media nguồn + WAV + MP4**; sau đó xác nhận đúng tin nhắn trong 10 phút. Xác nhận chỉ dùng một lần và khớp danh tính media đã hiển thị. Hủy hoặc media thay đổi thì không xóa. SRT, JSON, kịch bản và SQLite được giữ. Không thao tác trên dự án đang chạy hoặc thư mục liên kết ra ngoài.

**Auto Disk Guard:** mặc định bật, ngưỡng 8 GiB trên ổ thực sự chứa `settings.storage_path` (thường là D:). Kiểm tra trước khi tiếp nhận link Telegram, chọn dự án cũ nhất trước: DB `VERIFIED`, không có tác vụ hoạt động, media trên 100 MiB và có biên nhận khớp dữ liệu/tài khoản của **YouTube Private hoặc Drive full_delivery**. Gói `metadata_only` không đủ điều kiện tự xóa video. Dừng dọn khi đủ ngưỡng, báo dự án và số byte thực thu hồi. Biên nhận khớp lần tải trước chưa chứng minh bản từ xa vẫn còn; kiểm tra cloud nếu bạn đã xóa/sửa tệp từ xa. Lệnh “dọn ổ D” vẫn hỏi xác nhận, không tự biến thành thao tác xóa. Câu hỏi như “hủy tác vụ là gì?” hoặc “đẩy YouTube là gì?” được xử lý như hỏi đáp, không tự hủy hoặc upload.

**Vòng đời:** bot chỉ nhận đúng chat ID đã cấu hình; một tiến trình giữ khóa polling dù mở nhiều Studio. Khi đóng ứng dụng bình thường, bot ngừng polling và thử gửi thông báo nghỉ trong timeout 2 giây; mất điện/kill tiến trình không bảo đảm gửi được tin cuối. `last_update_id` được lưu nguyên tử trong `storage/private/telegram_offset.json` **trước khi thực thi lệnh** để không phát lại khi mở máy. Lần cài bot mới bỏ backlog cũ. Lệnh đang chờ thực thi khi máy tắt có thể chưa chạy; kiểm tra trạng thái rồi gửi lại chủ động. Offset hỏng hoặc không ghi được thì dừng nhận lệnh, không đoán để chạy lại. Đừng xóa offset để thử lại lệnh xóa/upload.

### Studio từ xa qua menu Telegram

Bot đăng ký nút Menu và các lệnh `/menu`, `/new`, `/projects`, `/running`, `/review`, `/storage`, `/status`, `/help` cho đúng chat đã cấu hình. Máy tính và Studio phải đang mở, có mạng; chưa có dịch vụ điều phối nhận việc khi máy tắt. Không cần mở cổng Studio ra Internet.

- **Tạo có cấu hình:** Menu → Tạo dự án → gửi một link Bilibili/Douyin → tải và kiểm định nguồn → chọn cấu hình → Kiểm tra & chạy Auto → xác nhận. Tải nguồn không tự khởi chạy dịch/giọng/render trong luồng này. Có thể đóng chat rồi vào Dự án để tiếp tục. Dán link ngoài trình tạo vẫn dùng luồng Auto nhanh hiện hữu; link đã tạo bằng trình này được hỏi mở bản cũ hay tạo bản mới khi gửi lại đúng URL.
- **Cấu hình Auto:** ba thành phẩm (giọng + phụ đề, chỉ phụ đề giữ tiếng gốc, chỉ giọng giữ hình gốc), đọc chữ/nghe thoại, sáu mẫu phụ đề như Web, chín lựa chọn giọng, tốc độ 1.0/1.1/1.15/1.2/1.25x, bốn profile fast/balanced/compact/quality và quy tắc xử lý câu chưa chắc. Chỉ phụ đề luôn giữ tiếng gốc và không tạo giọng; chỉ giọng không cho chọn tắt giọng, giữ hình gốc bằng style original. Menu hiển thị lựa chọn giọng, không bảo đảm model đã được cài hoặc nhà cung cấp luôn sẵn sàng. Bản nháp cấu hình lưu riêng; chỉ khi xác nhận chạy mới gửi RunAllRequest chuẩn và lưu auto_workflow.json. Tiếp tục Auto dùng cấu hình đã lưu, khác với mở bảng cấu hình mới.
- **Điều hành:** danh sách phân trang, Đang xử lý, Cần duyệt/lỗi, Kho lưu trữ và thẻ từng dự án. Các tác vụ dùng chung hàng đợi của Web. Thẻ đang chạy được cập nhật nền khoảng 30 giây khi có thay đổi, tối đa bốn thẻ mỗi lượt; có nút Cập nhật thủ công. Yêu cầu dừng không có nghĩa đã dừng. Thông báo tải/Auto kết thúc không thay đổi kết quả nếu gửi Telegram thất bại.
- **Duyệt/sửa:** xem từng câu nguồn–Việt, xem khung hình giữa câu, nghe file giọng đã có, sửa nguồn hoặc lời Việt bằng tin nhắn rồi xác nhận Lưu. Gửi `/cancel` hoặc về Menu để thoát nhập và hủy xác nhận sửa. API Studio ghi SQLite và vô hiệu hóa phần dịch/giọng/video phụ thuộc như trên Web. Nếu Web đã sửa hoặc dự án chạy lượt mới, nút cũ bị từ chối. Duyệt/tiếp tục Auto có bước xác nhận; không bỏ qua cổng nguồn, timing hay QA.
- **Sau hoàn tất:** Demo, YouTube Private, hồ sơ nhẹ Drive và giải phóng media dùng lại chức năng hiện hữu. Có nút mở liên kết cloud đã lưu và lựa chọn tải MP4 đầy đủ lên Drive với xác nhận dung lượng riêng. Biên nhận là lịch sử lưu, không phải xác nhận vừa kiểm tra lại cloud. VERIFIED là QA kỹ thuật, vẫn cần người dùng xem/nghe nội dung.
- **Dự án lỗi:** mở thẻ để xem mã lỗi, thử tải nguồn lại khi chưa có media, cấu hình/chạy tiếp hoặc chọn Xóa cả dự án. Xóa cả dự án là hai bước, chỉ khi rảnh, mất cả nguồn/giọng/MP4/phụ đề/hồ sơ và bản ghi dự án trên máy; bản cloud giữ nguyên. Giải phóng media là thao tác khác, giữ phụ đề/hồ sơ. Không tự xóa dự án chỉ vì tác vụ thất bại. Nút xác nhận bị ràng buộc với tin nhắn, tài khoản, phiên bản dự án và hạn dùng; dùng một lần. Hủy xóa vô hiệu hóa nút xác nhận cũ.

### Kiểm tra trước chạy và vận hành video dài

- Khi chọn rõ OCR hoặc nghe thoại, Auto kiểm tra loại nguồn của câu đã lưu **trước khi duyệt/dịch/tạo giọng**. Nếu không khớp, giữ nguyên dữ liệu và dừng. Trên Telegram: Cấu hình Auto → chọn Nguồn để dịch → **Nhận dạng lại nguồn** → đọc cảnh báo và xác nhận. Nhận dạng lại có thể thay thế câu và bản sửa cũ; sau đó mở cấu hình chạy Auto. Chọn `auto` vẫn cho phép bộ nhận dạng tự quyết định.
- Thẻ dự án, xác nhận tiếp tục và thông báo VERIFIED hiển thị cảnh báo dịch dự phòng/AI sửa nguồn nếu pipeline ghi nhận. `TRANSLATION_REVIEWED` trong Auto là phê duyệt theo quy tắc phần mềm; không phải bằng chứng người dùng đã xem. Cần đối chiếu nguồn–Việt và nghe Demo trước khi phát hành.
- Bảng cấu hình Telegram hiển thị **dự toán chạy lại đầy đủ**. Auto kiểm tra chỗ trống trước OCR, trước tạo giọng và trước render theo các phần còn cần làm; không chặn lượt chỉ kiểm tra lại QA bằng dự toán render mới. Dự toán gồm proxy/ảnh/âm thanh đối chiếu, các thế hệ WAV, hai thế hệ render và khoảng trống dự phòng. Đây là mô hình bảo thủ dựa trên thời lượng/dung lượng nguồn, không phải đo kích thước MP4 cuối hoặc cam kết đủ đĩa cho mọi nội dung. `compact` tiết kiệm phần render, không làm nhỏ nguồn đã tải và không giảm QA.
- Khi `PERSONAL_MODE=true` và Auto Disk Guard bật, kiểm tra tài nguyên trong lúc chạy cũng áp dụng ngưỡng trống của Disk Guard (mặc định 8 GiB). Thiếu chỗ thì tác vụ chuyển gián đoạn và giữ checkpoint; không tự xóa dự án đang chạy. Giải phóng media đã sao lưu rồi tiếp tục.
- `PIPELINE_SERIALIZE_HEAVY_TASKS=true` mặc định xếp lần lượt OCR, giọng, render, Auto và đối chiếu audio trên cùng hàng đợi. Server luôn giới hạn như trước. Tải/upload không lấy khóa tác vụ nặng nhưng vẫn phụ thuộc worker còn rảnh; không cam kết vượt hàng đợi. Có thể tắt cờ này trên máy đủ tài nguyên sau khi đo thực tế, không tăng số job chỉ để tăng phần trăm tiến độ.
- Downloader Range chỉ giữ tối đa bốn future đang xử lý mỗi luồng, không tạo trước toàn bộ future của tệp dài. Khi ghi nhận retry, giảm số đoạn mới được cấp; tăng dần sau các đoạn thành công. Request đang chạy được hoàn tất an toàn. Vẫn kiểm tra Content-Range/byte/hash và manifest trước khi dùng lại. Không khẳng định tăng băng thông thực tế khi chưa đo ngoài pytest.
- Tiến độ Telegram có thời gian đã xử lý và ghi rõ phần trăm là mốc công việc, **không phải ETA**. Tải nhanh không đồng nghĩa OCR/giọng/render nhanh; video ngắn nhiều lời có thể cần phục hồi timing lâu hơn video dài ít lời.

Mặc định: `TELEGRAM_NOTIFY_ENABLED=false`, token/chat ID rỗng; `TELEGRAM_POLLING_ENABLED=true`, `TELEGRAM_POLL_INTERVAL_SECONDS=2.5` (0,5–30), `TELEGRAM_DEMO_TRIGGER_DURATION_SECONDS=900` (tối thiểu 60), `AUTO_DISK_GUARD_ENABLED=true`, `AUTO_DISK_GUARD_MIN_FREE_GB=8` (2–100). Thông báo thường chia tối đa 3.800 ký tự; SRT/JSON trong dự án phải nhỏ hơn 45 MiB. Demo gửi multipart theo luồng, kèm thời lượng/kích thước khung hình đo từ tệp đã encode; timeout kết nối tối đa 10 giây, đọc/ghi tối đa 180 giây; polling có timeout riêng để dừng được. Lỗi thông báo không làm hỏng kết quả render/QA. Tác vụ giữ mã lỗi Telegram an toàn cùng trạng thái HTTP/thời gian chờ rate-limit; không lưu token, chat ID hay URL. Không tự gửi lại file khi kết quả truyền chưa rõ để tránh gửi trùng. API: `GET /api/notifications/telegram/status`, `POST /api/notifications/telegram/config`, `POST /api/notifications/telegram/test`. Trạng thái/bảng kho Web và Telegram cùng đọc DB, hàng đợi và biên nhận cục bộ, không phải một danh sách dự án riêng.

---

## Hướng dẫn CLI & Bộ công cụ Benchmark / Chẩn đoán

### 1. Lệnh dòng lệnh chính (`main.py`)

Hỗ trợ tự động hóa và thao tác trực tiếp qua console:

```powershell
# Tạo dự án mới từ liên kết hoặc tệp video
python main.py add "https://www.douyin.com/video/..." --trim-start 00:00:10 --trim-end 00:02:30

# Tải và kiểm định video nguồn
python main.py download MA_DU_AN

# Lưu tọa độ vùng phụ đề (tọa độ chuẩn hóa 0..1)
python main.py roi MA_DU_AN --x 0.05 --y 0.85 --width 0.90 --height 0.12

# Bóc tách phụ đề hardsub
python main.py ocr MA_DU_AN

# Phê duyệt các câu tiếng Trung sau khi đã kiểm tra và sửa hết nghi vấn
python main.py approve-source MA_DU_AN

# Dịch sang tiếng Việt
python main.py translate MA_DU_AN

# Phê duyệt bản dịch
python main.py approve-translation MA_DU_AN

# Tạo giọng đọc và căn timeline
python main.py voiceover MA_DU_AN --voice piper:ngochuyen --rate +15%

# Render video thành phẩm
python main.py render MA_DU_AN --style cover

# Chạy kiểm định chất lượng kỹ thuật QA
python main.py verify MA_DU_AN

# Chạy tự động trọn gói từ đầu đến cuối
python main.py run-all MA_DU_AN --voice piper:ngochuyen --rate +15% --audio-mode mix

# Xem trạng thái dự án
python main.py status MA_DU_AN

# Mở máy chủ Studio
python main.py serve --host 127.0.0.1 --port 9000
```

CLI `run-all` mặc định dùng nguồn `auto`, tốc độ `+0%`; thêm `--extraction-mode hardsub` để yêu cầu OCR hoặc `--extraction-mode stt` để nghe lời thoại. Các ví dụ `+15%` là lựa chọn minh họa. `approve-source --auto-resolve` có thể tự chọn biến thể cho cue nghi vấn, chỉ dùng sau khi hiểu và kiểm tra lựa chọn đó. `main.py serve` mặc định cổng 8088; ví dụ trên chỉ định 9000 cho khớp launcher cá nhân. Tên chiến lược timing `cluster-budget-v1` trong báo cáo khác với phiên bản backend mà launcher kiểm tra (`cluster-budget-v3`).

### 2. Các công cụ kiểm thử, Benchmark & Chẩn đoán độc lập

*Lưu ý:* Luôn chạy các script này **ngoài pytest**, sử dụng thư mục đầu ra mới trong `storage/maintenance/` để không ghi đè dữ liệu dự án thực tế.

Kiểm thử hồi quy ngoại tuyến chạy bằng `python -m pytest -q`. Bộ fixture trong `tests/conftest.py` cô lập database/thư mục tạm và chặn các đường gọi mạng provider trong bài test; audit video/provider thật bên dưới là bước kiểm chứng riêng. Với thử nghiệm tối ưu, ghi rõ công đoạn sẽ tích hợp vào dự án chính và tiêu chí đo chấp nhận; giữ bằng chứng đang dùng và chỉ dọn các đường dẫn thử nghiệm đã xác minh theo yêu cầu.

- **Đo hiệu năng các cỡ batch dịch thuật Gemini:**
  So sánh thời gian và tỷ lệ thành công của các kích thước batch khác nhau (ví dụ 100 câu so với 60 câu) trên dữ liệu phụ đề thực tế:
  ```powershell
  python scripts/benchmark_translation_batches.py MA_DU_AN --output storage/maintenance/ten_audit_dich --sizes 100 60
  ```
- **Audit cô lập quy trình căn giọng Bước 7 (`cluster-budget-v1`):**
  Chạy kiểm định và mô phỏng xếp lịch âm thanh trên một bản sao độc lập của dự án:
  ```powershell
  python scripts/audit_budget_workflow.py MA_DU_AN --output storage/maintenance/ten_audit_timing --voice-only --shorten
  ```
- **Chẩn đoán ảnh và câu nghi vấn OCR:**
  ```powershell
  python scripts/audit_ocr_review.py --project MA_DU_AN --output storage/maintenance/ten_audit_ocr --source-samples 6 --controls 6
  ```
- **Đo đạc chi tiết toàn bộ pipeline qua link video:**
  ```powershell
  python scripts/profile_video_pipeline.py --url "LINK_VIDEO" --output storage/maintenance/ten_profile_moi
  ```
- **So sánh luồng Auto và thao tác từng bước:**
  ```powershell
  python scripts/compare_video_workflows.py --checkpoint-project MA_DU_AN --output storage/maintenance/ten_so_sanh
  ```
- **Benchmark giọng đọc VieNeu GPU CUDA:**
  ```powershell
  python scripts/benchmark_vieneu.py --rounds 3 --output storage/maintenance/ten_do_vieneu
  python scripts/benchmark_vieneu_batches.py --reference storage/maintenance/ten_do_vieneu/summary.json --output storage/maintenance/ten_batch_moi --rounds 3
  ```
- **Kiểm chứng chất lượng nghe mù A/B VieNeu:**
  ```powershell
  python scripts/verify_vieneu_quality.py --reference storage/maintenance/ten_do_vieneu/summary.json --candidate storage/maintenance/ten_batch_moi/summary.json --output storage/maintenance/quality_verification_moi
  python scripts/verify_vieneu_quality.py --output storage/maintenance/quality_verification_moi --prepare-blind
  ```
  Chạy theo thứ tự: baseline → batch → verification → bộ nghe mù bổ sung. `--prepare-blind` cần verification đã hoàn tất ASR cho đủ 40 cặp. Baseline/candidate phải khớp corpus, runtime, giọng, đủ số vòng và các điều kiện mà script kiểm tra; benchmark batch yêu cầu CUDA. Các audit lịch sử có thể đã được dọn, không dùng đường dẫn cũ như dữ liệu mặc định có sẵn. Kết quả ASR chẩn đoán không thay thế đánh giá nghe.
- **Đo tối ưu I/O đọc video trong OCR:**
  ```powershell
  python scripts/benchmark_ocr_recheck.py --project MA_DU_AN_AUDIT --output storage/maintenance/ten_do_io
  ```
- **Đo luồng tạo proxy và đối chiếu điểm ảnh:** chạy ngoài pytest, ưu tiên khi không có video khác đang xử lý; kết quả chỉ phản ánh pha proxy trên nguồn và tải máy đã đo.
  ```powershell
  python scripts/benchmark_scan_proxy.py --source D:/Video/nguon.mp4 --output storage/maintenance/ten_do_proxy --start 60 --seconds 45 --rounds 3
  ```
  So sánh thời gian trung vị và checksum của mọi khung đã giải mã. Chỉ nhận cấu hình nhanh hơn khi giữ đủ khung/mốc và checksum; không dùng một lượt nhanh bất thường để kết luận tốc độ toàn pipeline.

---

## Bảng tra cứu cấu hình `.env`

Dưới đây là các tham số chính và mặc định trong mã nguồn. Khi cần thiết lập, tham khảo `.env.example`; giữ nguyên `.env` đang dùng và không công khai khóa/cookie. Tên model bên dưới là chuỗi cấu hình của ứng dụng, không bảo đảm provider còn cung cấp hoặc tài khoản của bạn có quyền dùng.

| Nhóm tham số | Tên biến môi trường | Mặc định | Ý nghĩa & Hướng dẫn |
| :--- | :--- | :--- | :--- |
| **Vận hành** | `PERSONAL_MODE` | `true` | Chế độ Studio cá nhân, tắt xác thực người dùng phức tạp |
| | `OFFLINE_MODE` | `false` | Chạy offline hoàn toàn với model máy tính khi bật `true` |
| | `LOG_LEVEL` | `INFO` | Mức chi tiết log hệ thống |
| **Lưu trữ & DB** | `DATABASE_URL` | `sqlite:///./storage/app.db` | Đường dẫn cơ sở dữ liệu SQLite chính của ứng dụng |
| | `STORAGE_DIR` | `./storage/projects` | Thư mục lưu video, âm thanh, phụ đề của các dự án |
| **Tải video** | `MAX_DOWNLOAD_SIZE_BYTES` | `25769803776` (24 GiB) | Dung lượng tệp tải tối đa (hỗ trợ video 1080p dài) |
| | `MAX_VIDEO_DURATION_SECONDS` | `21600` (6 giờ) | Thời lượng video tối đa được phép xử lý |
| | `BILIBILI_PREFERRED_QUALITY` | `80` (1080p) | Mã chất lượng ưu tiên. Chất lượng nguồn còn phụ thuộc vào video và luồng Bilibili cung cấp |
| | `BILIBILI_COOKIE` | *(để trống)* | Cookie tài khoản Bilibili; chất lượng tải phụ thuộc nguồn và quyền truy cập |
| | `BILIBILI_SESSDATA` | *(để trống)* | SESSDATA tài khoản nếu dùng thay cấu hình cookie |
| | `DOUYIN_COOKIE_FILE` | *(để trống)* | Đường dẫn file Netscape cookie cho Douyin |
| **STT & OCR** | `WHISPER_MODEL_SIZE` | `auto` | Kích thước model Whisper (`large-v3-turbo`, `small`, `base`) |
| | `WHISPER_DEVICE` | `auto` | Thiết bị chạy Whisper (`auto` dùng CUDA nếu có, hoặc `cpu`) |
| | `WHISPER_BEAM_SIZE` | `3` | Beam của luồng STT thông thường; bước nghe đối chiếu OCR dùng cấu hình riêng beam 1 |
| | `OCR_DEVICE` | `auto` | Bộ máy RapidOCR (`auto` dùng CUDA đã kiểm định, hoặc `cpu`) |
| | `VSF_USE_SCAN_PROXY` | `true` | Cho phép tạo proxy cho lượt dò phụ đề |
| | `VSF_PROXY_WIDTH` | `960` | Chiều rộng proxy tính bằng pixel |
| | `VSF_PROXY_FILTER_THREADS` | `1` | Luồng bộ lọc thu nhỏ proxy (1–16); không đổi FPS hay ngưỡng OCR |
| | `OCR_SOURCE_SPOOL_MAX_BYTES` | `2147483648` (2 GiB) | Trần tệp ảnh nguồn thô dùng chung; 0 dùng lô nhỏ, tối đa 16 GiB |
| | `OCR_AUDIO_RECHECK` | `true` | Bật tự động đối chiếu âm thanh với chữ OCR |
| | `AUTO_SKIP_UNCERTAIN` | `true` | Chốt cue nguồn còn nghi vấn bằng ảnh và lời có mốc khi chạy Auto |
| | `STT_AUDIO_FILTER` | `highpass=f=180,lowpass=f=3400,afftdn=nf=-25` | Bộ lọc làm rõ giọng nói tiếng Trung và khử tạp âm |
| **Dịch thuật AI** | `GEMINI_API_KEY` | *(điền key của bạn)* | Khóa API Google Gemini (lấy tại Google AI Studio) |
| | `TRANSLATION_MODEL` | `gemini-3.5-flash-lite` | Model chính thực hiện dịch thuật hàng loạt |
| | `TRANSLATION_FALLBACK_MODELS`| `gemini-3.1-flash-lite,gemini-3.8-flash,gemini-3.6-flash` | Danh sách model dự phòng theo thứ tự cấu hình |
| | `TRANSLATION_REPAIR_MODEL` | `gemini-3.8-flash` | Model chuyên dụng sửa các câu lỗi ngữ nghĩa hoặc token |
| | `TRANSLATION_BATCH_SIZE` | `100` | Số câu gửi dịch trong một batch Gemini |
| | `LOCAL_TRANSLATION_URL` | *(để trống)* | Địa chỉ runtime HY-MT cục bộ nếu có (`http://127.0.0.1:8091/v1`) |
| | `AZURE_TRANSLATOR_KEY` | *(để trống)* | Khóa API dịch thuật dự phòng Azure Translator |
| | `AZURE_TRANSLATOR_REGION` | *(để trống)* | Vùng của tài nguyên Azure nếu tài nguyên yêu cầu |
| **Giọng đọc TTS** | `TTS_PROVIDER` | `piper` | Bộ đọc mặc định trong cấu hình |
| | `DEFAULT_TTS_VOICE` | `piper:ngochuyen` | Giọng mặc định của Auto và bộ tạo timeline khi không chỉ định giọng |
| | `EDGE_TTS_VOICE` | `vi-VN-NamMinhNeural` | Giọng đọc Edge-TTS mặc định |
| | `VIENEU_DEVICE` | `auto` | Chế độ chạy VieNeu GPU (`auto`, `cuda` hoặc `cpu`) |
| | `TTS_TRIM_SILENCE` | `true` | Cắt khoảng lặng thừa ở đầu và cuối file âm thanh |
| | `TTS_TRIM_KEEP_MS` | `60` | Khoảng đệm PCM giữ lại để giọng đọc tự nhiên (ms) |
| | `TTS_MAX_TOTAL_SPEED` | `1.30` | Trần nén tốc độ tối đa của giọng đọc khi căn timeline |
| | `ORIGINAL_AUDIO_VOLUME` | `0.15` | Âm lượng tiếng gốc khi trộn lồng tiếng (15%) |
| | `VOICEOVER_VOLUME` | `1.7` | Âm lượng giọng tiếng Việt khi trộn lồng tiếng (170%) |
| **Sub & Render** | `SUBTITLE_MODE` | `burn` | Cấu hình cho luồng phụ đề dùng biến này. Auto Studio chọn hình thức xuất theo mục tiêu và cấu hình render |
| | `DEFAULT_PLATFORM_PRESET` | `auto` | Preset khung hình thương mại mặc định (`auto`, `ytb_16_9`, `tiktok_reels_9_16`, `fb_feed`) |
| | `DEFAULT_TARGET_PART_SECONDS` | `180` | Thời lượng mục tiêu mỗi tập (60–900 giây) dùng cho keyframe cắt phần |
| | `VIDEO_ENCODER` | `auto` | Bộ mã hóa video (`auto` ưu tiên GPU NVENC, sau đó CPU libx264) |
| | `CLEAN_OVERLAYS` | `true` | Bật dọn overlay ở luồng xử lý dùng biến này. Chế độ giữ hình gốc của Studio vẫn giữ chữ và logo nguồn |
| **Đăng kênh** | `PUBLISHING_CHANNEL_NAME` | `NpzzSub Review` | Tên kênh dùng trong mẫu mô tả và bài đăng tạo bởi `publishing_pack` |
| | `PUBLISHING_CHANNEL_HANDLE` | `@NpzzSub` | Handle kênh dùng trong mẫu mô tả và bài đăng |
| | `PUBLISHING_CHANNEL_URL` | `www.youtube.com/@NpzzSub` | Đường dẫn kênh gắn cuối mẫu mô tả xuất bản |
| | `PUBLISHING_SCHEDULE` | `11:30 & 19:30` | Khung giờ đăng hiển thị trong mô tả gói đăng kênh |
| | `PUBLISHING_HASHTAGS` | `#NpzzSub,#NpzzSubReview,#Vietsub` | Danh sách hashtag mặc định (phân tách bởi dấu phẩy) |

---

## Nguyên tắc vận hành & An toàn dữ liệu

1. **Cơ sở dữ liệu là nguồn chân lý duy nhất (SSOT):** Bảng phụ đề và các bản ghi trong cơ sở dữ liệu `storage/app.db` là dữ liệu xác thực cao nhất, kể cả trường hợp transcript cố ý để trống. Tuyệt đối không bao giờ che giấu lỗi ghi database bằng cách chỉ xuất file JSON.
2. **Bảo toàn dữ liệu người dùng:** Giữ nguyên vẹn toàn bộ các dự án trong `storage/projects`. Các đợt kiểm thử hoặc chẩn đoán video thật bắt buộc phải chạy trên một bản sao dự án độc lập (ví dụ qua script `audit_budget_workflow.py`).
3. **Pytest cô lập và ngoại tuyến:** Pytest chạy cô lập, chặn gọi provider mạng và sử dụng database tạm thời. Không bao giờ hạ ngưỡng tiêu chuẩn kỹ thuật QA để ép bài test vượt qua.
4. **Tái sử dụng Cache có danh tính:** Chỉ tái sử dụng các file cache đã hoàn tất và có mã nhận dạng khớp chính xác về nguồn, cấu hình, phiên bản engine và tham số xử lý. Một file đang cấp phát dở hoặc một lượt quét chưa xong không được coi là kết quả hoàn thành.
5. **Dọn dẹp rác đúng quy định:** Chỉ dọn dẹp các tệp tạm thời không còn sử dụng theo đúng yêu cầu; không tạo các bản nén lưu trữ (archive) của những tệp rác đã xóa. Giữ nguyên các tài sản mô hình đã kiểm định (`storage/models/`, `tools/`).
