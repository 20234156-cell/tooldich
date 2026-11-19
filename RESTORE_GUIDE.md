# Hướng Dẫn Khôi Phục Hệ Thống DuanLayLink

Tài liệu này mô tả cấu trúc bản sao lưu đã đẩy lên **Google Drive** (thư mục `DuanLayLink - ThanhPham` / thư mục con sao lưu ngày `2026-11-19` trên tài khoản `nhocnam2111@gmail.com`) và **GitHub** (`https://github.com/20234156-cell/tooldich.git`, nhánh `main`), cùng các bước dựng lại hệ thống khi cần sử dụng lại.

---

## 1. Cấu Trúc Các Gói Sao Lưu Trên Google Drive

Để phù hợp dung lượng trống ổ đĩa cục bộ và dễ kiểm soát checksum, hệ thống được tách thành các gói `.zip` độc lập (đã bỏ qua thư mục log/audit cũ `storage/maintenance/`):

1. **`01_duanlaylink_core_config_db.zip`**:
   - Toàn bộ mã nguồn (`src/`, `web/`, `tests/` gồm cả fixtures, `scripts/`, `docs/`, `config/`, các tệp gốc như `main.py`, `start_server.bat`, `requirements*.txt`, `README.md`, `RESTORE_GUIDE.md`).
   - Cấu hình & khóa cục bộ: `.env`, `config/gemini_keys.bin`, `config/local_translation.key`.
   - Cơ sở dữ liệu & tài nguyên nhẹ: bản snapshot sạch `storage/app.db` (đã checkpoint WAL qua `sqlite3.backup`), `assets/`, `learning/`, `storage/private/`, `storage/tessdata/`, `storage/ocr_cache/`, `storage/voice_refs/`, `storage/downloads/`, `storage/voices/`, `storage/benchmarks/`, `storage/logs/`, `scratch/`.
   - Bản sao lịch sử Git đầy đủ (`git_all_refs_20261119.bundle`) và danh sách thư viện Python đang cài (`pip_freeze.txt`).
2. **`02_duanlaylink_models.zip`**:
   - Thư mục `storage/models/` (`whisper-large-v3-turbo`, `hy-mt1.5-1.8b`, `lama`).
3. **`03_duanlaylink_tools_vieneu_gpu.zip`**:
   - Thư mục `tools/vieneu-gpu/` (runtime PyTorch CUDA 12.6 dùng cho VieNeu TTS & DLL CUDA dùng chung cho `ocr-gpu`).
4. **`04_duanlaylink_tools_other.zip`**:
   - Các công cụ còn lại trong `tools/`: `llama-cuda124-b11815/`, `piper/`, `ocr-gpu/`, `videosubfinder/`.
5. **`05_duanlaylink_projects_part_*.zip`**:
   - Các thư mục dự án trong `storage/projects/`.
6. **`BACKUP_MANIFEST.json`**:
   - Danh sách toàn bộ tệp `.zip`, kích thước byte, `MD5`, `SHA-256` và `remote_id` trên Google Drive để đối chiếu toàn vẹn sau khi tải về.

---

## 2. Quy Trình Dựng Lại Hệ Thống Trên Máy

### Bước 1: Tải và giải nén về đúng thư mục gốc
- Tải các gói `.zip` từ Google Drive về và giải nén chồng vào cùng một thư mục gốc khuyến nghị: **`D:\duanlaylink`** (hoặc giữ nguyên mã nguồn từ `git clone https://github.com/20234156-cell/tooldich.git` rồi giải nén đè `.env`, `config/`, `storage/`, `tools/` từ các gói `.zip`).
- Kiểm tra tệp `storage/app.db`, `.env`, `config/gemini_keys.bin` đã nằm đúng vị trí tương đối bên trong `D:\duanlaylink`.

### Bước 2: Cài Python 3.12 và thư viện phụ thuộc
Khuyến nghị tạo môi trường ảo `.venv` ngay trong `D:\duanlaylink` (trình khởi chạy `start_server.bat` tự ưu tiên `.venv\Scripts\python.exe`):

```powershell
cd D:\duanlaylink
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-optional.txt
```

- Tham khảo `pip_freeze.txt` (trong gói `01_duanlaylink_core_config_db.zip`) nếu cần khóa chính xác phiên bản gói như tại thời điểm sao lưu (`2026-11-19`).

### Bước 3: Kiểm tra các công cụ hệ thống ngoài thư mục dự án
Trong cấu hình `.env` thời điểm sao lưu, hệ thống trỏ tới các công cụ cài trên Windows:
- **FFmpeg & FFprobe** (`FFMPEG_PATH` / `FFPROBE_PATH` trong `.env`):
  Nếu cài lại Windows/máy mới, cài lại FFmpeg (ví dụ `winget install BtbN.FFmpeg.GPL.7.1`) hoặc cập nhật lại đường dẫn trong `.env` (hoặc để trống `FFMPEG_PATH=` / `FFPROBE_PATH=` nếu đã có trên `PATH`).
- **Tesseract OCR** (`TESSERACT_PATH` trong `.env`):
  Cài Tesseract OCR vào `C:\Program Files\Tesseract-OCR\tesseract.exe` hoặc cập nhật `TESSERACT_PATH` trong `.env`. Dữ liệu ngôn ngữ `storage\tessdata` đã có sẵn trong gói `01`.

### Bước 4: Lưu ý cho `tools/vieneu-gpu` và `tools/ocr-gpu` khi đổi đường dẫn / cài lại Python
- `tools/vieneu-gpu` và `tools/ocr-gpu` là các Python `venv` tạo với `--system-site-packages` (gắn với đường dẫn `D:\duanlaylink` và Python 3.12 tại `C:\Users\QT\AppData\Local\Programs\Python\Python312`).
- Nếu giải nén đúng `D:\duanlaylink` trên cùng máy và cùng Python 3.12, hai thư mục này chạy lại được ngay.
- Nếu chuyển sang máy khác hoặc đổi thư mục/Python mới, có thể dựng lại sạch theo `requirements-vieneu-gpu.txt` và `requirements-ocr-gpu.txt`:
  ```powershell
  python -m venv --system-site-packages tools/vieneu-gpu
  .\tools\vieneu-gpu\Scripts\python.exe -m pip install -r requirements-vieneu-gpu.txt
  python -m venv --system-site-packages tools/ocr-gpu
  .\tools\ocr-gpu\Scripts\python.exe -m pip install -r requirements-ocr-gpu.txt
  ```
- Runtime `llama-cuda124-b11815` cũng có thể tải lại tự động bằng:
  ```powershell
  python scripts/setup_local_translation_runtime.py
  ```

### Bước 5: Lưu ý cực kỳ quan trọng về Windows DPAPI (nếu cài lại Windows hoặc đổi máy)
- Các biến dạng văn bản thuần trong **`.env`** (như `GEMINI_API_KEY`, `BILIBILI_SESSDATA`, đường dẫn...) dùng lại được ngay sau khi giải nén.
- Tuy nhiên, các dữ liệu mã hóa bằng **Windows DPAPI** gồm:
  - Kho key xoay vòng trong `config/gemini_keys.bin`
  - Token OAuth Google Drive / YouTube trong `storage/app.db` (bảng `cloud_connections`)
  - Cấu hình Telegram Bot trong `storage/private/telegram_config.json`
  chỉ tự giải mã được trên **đúng tài khoản Windows và máy đã tạo ra chúng**. Nếu Đại Ca cài lại Windows hoặc chuyển sang máy khác, sau khi mở Studio chỉ cần:
  1. Nhập lại danh sách Gemini API key trong **Quản lý API Key**.
  2. Nạp lại OAuth Desktop JSON và bấm kết nối lại Google Drive / YouTube trong **Kho cloud**.
  3. Lưu lại Bot Token / Chat ID trong **Kho cloud → Trợ lý Telegram** (nếu sử dụng).

### Bước 6: Khởi chạy và kiểm tra lại
```powershell
cd D:\duanlaylink
.\start_server.bat
```
Kiểm tra bộ kiểm thử ngoại tuyến:
```powershell
python -m pytest -q
```
