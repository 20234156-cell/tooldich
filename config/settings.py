from pathlib import Path
from typing import Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent / ".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # General
    APP_ENV: str = "development"
    LOG_LEVEL: str = "INFO"

    # Database
    DATABASE_URL: str = "sqlite:///./storage/app.db"

    # Storage Paths
    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    STORAGE_DIR: str = "./storage/projects"
    FFMPEG_PATH: Optional[str] = None
    FFPROBE_PATH: Optional[str] = None
    VSF_PATH: Optional[str] = None
    VSF_THREADS: int = 0  # 0 = auto, reserve one CPU core
    VSF_USE_CUDA: bool = False  # official Windows 6.10 build is CPU-first
    # Benchmark thật trên cùng đoạn 120s: quét 1080p trực tiếp 67.35s;
    # tạo + quét proxy 960px 34.06s, cùng tìm được 17 cue.
    VSF_USE_SCAN_PROXY: bool = True
    VSF_PROXY_WIDTH: int = 960

    # Network & Limits
    REQUEST_TIMEOUT_SECONDS: int = 30
    MAX_DOWNLOAD_SIZE_BYTES: int = 8 * 1024**3  # 8 GiB, including long-form 1080p
    MAX_VIDEO_DURATION_SECONDS: int = 3 * 3600  # 3 hours; measured example: 132 min
    # Trần cứng số lượt tải. MAX_RETRIES chỉ đếm những lượt KHÔNG nhích được byte nào
    # (xem DownloadManager._account_progress), nên lượt nào tải thêm được là ngân sách
    # hồi lại - nhờ vậy tệp cỡ GB trên đường truyền hay đứt vẫn về đích. Trần này để
    # một nguồn nhỏ giọt vài byte mỗi lượt không giữ pipeline chạy mãi không dứt.
    MAX_DOWNLOAD_ATTEMPTS: int = 30
    MAX_RETRIES: int = 3
    BACKOFF_FACTOR: float = 1.5
    ENABLE_TURBO_DOWNLOAD: bool = False
    FFMPEG_TIMEOUT_SECONDS: int = 1800                 # 30 phút cho tệp lớn
    USER_AGENT: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )

    # Mã chất lượng xin Bilibili (qn): 16=360P, 32=480P, 64=720P, 80=1080P, 116=1080P60.
    # Khi có tài khoản (SESSDATA/Cookie), hệ thống sẽ tải thẳng luồng 1080P Full HD siêu nét.
    BILIBILI_PREFERRED_QUALITY: int = 80
    BILIBILI_COOKIE: Optional[str] = None
    DOUYIN_COOKIE_FILE: Optional[str] = None  # User-supplied Netscape cookie file
    DOUYIN_BROWSER_PROFILE: Optional[str] = None  # Separate persistent Chromium profile
    BILIBILI_SESSDATA: Optional[str] = None

    # Supported Media Codecs
    SUPPORTED_VIDEO_CODECS: set[str] = {
        "h264", "h265", "hevc", "av1", "vp9", "vp8", "mpeg4"
    }
    SUPPORTED_AUDIO_CODECS: set[str] = {
        "aac", "mp3", "opus", "vorbis", "pcm_s16le", "pcm_s24le", "flac"
    }

    # Audio & STT
    AUDIO_SAMPLE_RATE: int = 16000
    # Bộ lọc ffmpeg áp lên audio TRƯỚC khi nhận dạng giọng nói: giữ dải tần tiếng
    # nói (180-3400 Hz) rồi khử nhiễu nền. Đo thực tế: cụm lặp nhiều nhất giảm từ
    # 17 lần xuống 3 lần. Để trống nếu muốn dùng audio nguyên bản.
    STT_AUDIO_FILTER: str = "highpass=f=180,lowpass=f=3400,afftdn=nf=-25"
    WHISPER_MODEL_SIZE: str = "auto"
    # auto = dùng GPU NVIDIA nếu có (nhanh ~3.6 lần, đo trên GTX 1050 Ti), không thì CPU
    WHISPER_DEVICE: str = "auto"          # auto | cuda | cpu
    WHISPER_COMPUTE_TYPE: str = "auto"    # auto | int8 | int8_float16 | float16 | float32
    # Đo thực tế: beam 3 nhanh hơn beam 5 khoảng 19% mà độ chính xác tương đương.
    # Beam 1 nhanh hơn nữa nhưng hay xuất chữ phồn thể thay vì giản thể.
    WHISPER_BEAM_SIZE: int = 3
    # Bộ lọc phát hiện tiếng nói (Silero VAD). BẬT mặc định: loại bỏ đoạn chỉ có nhạc nền
    # để tránh Whisper bị ảo giác lặp câu liên tục.
    WHISPER_VAD_FILTER: bool = True
    # Ngưỡng/đệm của bộ phát hiện giọng nói. Đo trên 8 video thật (2026-09-25): ngưỡng
    # 0.35 + đệm 400ms giữ 96-100% lời của video hội thoại và KHÔNG có câu lặp nào;
    # ngưỡng mặc định 0.5 mất tới 8%, còn 0.25 bắt đầu lọt câu lặp ảo giác trở lại.
    WHISPER_VAD_THRESHOLD: float = 0.35
    WHISPER_VAD_MIN_SILENCE_MS: int = 500
    WHISPER_VAD_SPEECH_PAD_MS: int = 400
    SUPPORTED_STT_LANGUAGES: set[str] = {"zh", "en", "vi", "ja", "ko"}

    # Scene Detection & OCR (Phase 4)
    SCENE_THRESHOLD: float = 0.35
    MIN_SCENE_DURATION: float = 1.0
    MAX_SCENES: int = 200
    KEYFRAME_WIDTH: int = 640
    OCR_ENABLED: bool = True
    OCR_LANGUAGES: str = "chi_sim+eng"
    TESSERACT_PATH: Optional[str] = None
    TESSDATA_PREFIX: Optional[str] = None

    # AI & Translation
    AI_PROVIDER: str = "gemini"  # gemini | anthropic
    ANTHROPIC_API_KEY: Optional[str] = None
    ANTHROPIC_MODEL: str = "claude-opus-5"
    GEMINI_API_KEY: Optional[str] = None
    GEMINI_MODEL: str = "gemini-3-flash-preview"
    # Bậc miễn phí hay báo 503 "quá tải" -> tự chuyển sang model dự phòng
    GEMINI_FALLBACK_MODELS: str = "gemini-3.8-flash,gemini-flash-latest"
    GEMINI_TIMEOUT_SECONDS: float = 60.0
    GEMINI_TOTAL_DEADLINE_SECONDS: float = 120.0
    OPENAI_API_KEY: Optional[str] = None
    TARGET_LANGUAGE: str = "vi"
    TRANSLATION_MODEL: str = 'gemini-3.5-flash-lite'
    TRANSLATION_FALLBACK_MODELS: str = 'gemini-3.8-flash,gemini-3.6-flash,gemini-3.1-flash-lite'
    TRANSLATION_REPAIR_MODEL: str = 'gemini-3.8-flash'
    TRANSLATION_BATCH_SIZE: int = 100
    TRANSLATION_BATCH_MAX_CHARS: int = 18000
    TRANSLATION_AUTO_GLOSSARY: bool = True
    TRANSLATION_GLOSSARY_MIN_CUES: int = 100
    TRANSLATION_KEY_RPM: int = 12
    TRANSLATION_MAX_WORKERS: int = 5
    LOCAL_TRANSLATION_URL: Optional[str] = None
    LOCAL_TRANSLATION_API_KEY_FILE: str = 'config/local_translation.key'
    TRANSLATION_MAX_TOKENS: int = 16000
    AI_MAX_TOKENS: int = 16000
    SCENE_ANALYSIS_BATCH_SIZE: int = 8
    # Số lượt gọi mô hình chạy song song (phân tích cảnh, dịch thuật)
    AI_CONCURRENCY: int = 3

    # Lồng tiếng (Text-to-Speech)
    TTS_PROVIDER: str = "edge"  # edge (miễn phí, không hạn mức) | gemini
    EDGE_TTS_VOICE: str = "vi-VN-NamMinhNeural"
    EDGE_TTS_RATE: str = "+12%"
    GEMINI_TTS_MODEL: str = "gemini-3.1-flash-tts-preview"
    GEMINI_TTS_FALLBACK_MODELS: str = "gemini-2.5-flash-preview-tts"
    GEMINI_TTS_VOICE: str = "Kore"
    TTS_SAMPLE_RATE: int = 24000
    # Cắt quãng lặng engine TTS tự chèn vào đầu/cuối mỗi câu. Đo trên 152 câu thật:
    # 0.16s đầu + 0.78s cuối mỗi câu = 144s hơi thừa, đủ để đẩy bản đọc từ 200s lên
    # 344s và làm phụ đề lệch tiếng tới 17 giây. Đặt False để giữ nguyên bản engine.
    TTS_TRIM_SILENCE: bool = True
    # Chừa lại chừng này ở hai đầu sau khi cắt, để câu không cụt và hai câu liền
    # nhau vẫn có nhịp nghỉ. 60ms: đủ nghe tự nhiên, không đáng kể so với 0.94s cắt đi.
    TTS_TRIM_KEEP_MS: int = 60
    # Sàn tuyệt đối của "coi như im lặng" (biên độ mẫu 16-bit, đỉnh 32767). Ngưỡng
    # thật lấy max(sàn này, 2% đỉnh của chính câu đó) nên không phụ thuộc độ to giọng.
    TTS_SILENCE_FLOOR: int = 300
    # Giãn nhịp giữa các lần gọi TTS (giây) - tránh chạm hạn mức theo phút
    # Số câu đọc song song (chỉ áp dụng cho engine không có hạn mức như edge).
    # Đo thực tế trên 40 câu: 8 luồng 15.4s, 16 luồng 10.3s, 32 luồng 7.2s - không
    # lỗi ở mức nào. Chọn 16 làm mặc định cân bằng; tăng lên 32 nếu cần nhanh hơn.
    TTS_CONCURRENCY: int = 4
    VIENEU_CPU_THREADS: int = Field(default=2, ge=1, le=8)
    TTS_NETWORK_CONCURRENCY: int = 8
    TTS_REQUEST_TIMEOUT_SECONDS: float = 15.0
    TTS_TOTAL_DEADLINE_SECONDS: float = 45.0
    # edge-tts thỉnh thoảng trả về rỗng dù tham số đúng. Nó không tốn hạn mức
    # nên cứ thử lại nhiều lần, rẻ hơn nhiều so với việc hỏng cả lượt lồng tiếng.
    TTS_MAX_RETRIES: int = 3
    # Giãn nhịp giữa các lần gọi, chỉ dùng cho engine có hạn mức (gemini)
    TTS_REQUEST_DELAY: float = 1.0
    # Phân tích cảm xúc trước khi đọc, để chọn nhanh/chậm, cao/thấp cho từng câu
    EMOTION_DUBBING: bool = True
    EMOTION_BATCH_SIZE: int = 40
    # Tốc độ đọc tiếng Việt trung bình (đo thực tế NamMinh: ~4.22 âm tiết/s)
    SPEECH_WORDS_PER_SECOND: float = 4.2
    # Giới hạn nén tốc độ đọc từng câu (atempo) để giữ nhịp tự nhiên
    MAX_CLIP_TEMPO: float = 1.3
    TTS_MAX_TOTAL_SPEED: float = Field(default=1.3, ge=1.0, le=1.3)
    TTS_VOICE_LEAD_SECONDS: float = Field(default=.1, ge=0.0, le=.2)
    TTS_MIN_PAUSE_SECONDS: float = Field(default=.04, ge=0.0, le=.15)
    TTS_MAX_BORROW_SECONDS: float = Field(default=.25, ge=0.0, le=.3)
    TIMING_REPAIR_MAX_CUES: int = Field(default=150, ge=1, le=1000)
    TIMING_REPAIR_MAX_PASSES: int = Field(default=2, ge=0, le=2)
    # Độ trôi tối đa cho phép trước khi kích hoạt nén câu tiếp theo để kéo về
    MAX_DRIFT_SECONDS: float = 1.0
    FIT_VOICEOVER_TO_VIDEO: bool = False
    # Âm lượng tiếng gốc khi lồng tiếng đè lên (khoảng vàng: 0.10 - 0.18, chuẩn 0.15 làm nền)
    ORIGINAL_AUDIO_VOLUME: float = 0.15
    VOICEOVER_VOLUME: float = 1.7

    # Phụ đề trên video lồng tiếng
    #   off  = không gắn phụ đề (tệp .srt vẫn được sinh ra để tải riêng)
    #   soft = nhúng thành luồng phụ đề bật/tắt được, KHÔNG encode lại video (đo: 0s)
    #   burn = khắc thẳng vào khung hình, xem được ở mọi nơi kể cả khi đăng lại
    #          lên mạng xã hội. Phải encode lại video (đo: 7s cho video 3 phút).
    # Mặc định 'burn' vì phần lớn nền tảng đăng video bỏ qua luồng phụ đề mềm.
    SUBTITLE_MODE: str = "burn"
    SUBTITLE_FONT: str = "Arial"
    SUBTITLE_FONT_SIZE: int = 20
    SUBTITLE_COLOR: str = "&H00FFFFFF"     # &H00FFFFFF (trắng tinh) hoặc &H0000D7FF (vàng hổ phách)
    SUBTITLE_OUTLINE: float = 2.5          # Viền đen sắc nét
    SUBTITLE_BOLD: bool = True             # In đậm chữ để dễ đọc
    # Khoảng cách từ đáy khung hình, đủ cao để không đè lên phụ đề gốc của video.
    # LƯU Ý: đây là đơn vị ASS, KHÔNG phải pixel - ffmpeg sinh tệp ass với
    # PlayResY=288 nên libass nhân giá trị này với (chiều_cao_video / 288).
    SUBTITLE_MARGIN_V: int = 28
    # Chất lượng khi phải encode lại để khắc phụ đề (thấp hơn = nét hơn, nặng hơn).
    # Đo thực tế trên video 30s 640x360: veryfast/23 1.07s - medium/20 2.49s -
    # slow/18 4.24s. Chọn medium/18 vì CRF cao làm MỀM đúng thứ mắt nhìn vào: nét
    # chữ phụ đề có viền và cạnh cứng của ô che. Không lên 'slow': thêm 9s cho cả
    # video chỉ đổi lấy ~2% bitrate.
    SUBTITLE_BURN_CRF: int = 18
    # Đo trên video 166s 640x360, CRF 18: medium 18.5s · faster 11.6s (+2% dung lượng)
    SUBTITLE_BURN_PRESET: str = "faster"
    # auto = dùng NVENC của GPU NVIDIA nếu ffmpeg mở được nó, không thì libx264 (CPU)
    VIDEO_ENCODER: str = "auto"  # auto | nvenc | cpu

    # Làm sạch khung hình: che watermark góc và dải phụ đề cứng của video gốc.
    # Bộ dò đo "chữ sáng có viền tối" rồi phân loại theo độ bền thời gian. Khi
    # không đủ chắc chắn thì KHÔNG che gì cả - vùng che sai đã nướng vào tệp xuất
    # ra thì không gỡ lại được, tệ hơn hẳn cái watermark vốn vẫn xem được.
    CLEAN_OVERLAYS: bool = True
    CLEAN_SAMPLE_FRAMES: int = 24
    CLEAN_MIN_CONFIDENCE: float = 0.6
    # 4 watermark thật đo được đều chỉ chiếm ~3,5% khung hình; vùng lớn hơn là dò sai
    CLEAN_WATERMARK_MAX_AREA: float = 0.06
    CLEAN_SUBTITLE_BAND_MAX_H: float = 0.14

    # Security (Phase 7)
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_REQUESTS: int = 120
    RATE_LIMIT_WINDOW_SECONDS: int = 60
    MAX_UPLOAD_SIZE_BYTES: int = 1024 * 1024 * 500  # 500MB
    ENFORCE_DOWNLOAD_HOST_ALLOWLIST: bool = True
    MAX_DOWNLOAD_REDIRECTS: int = 5
    TRUST_PROXY_HEADERS: bool = False
    # Ngăn nguồn local_file bị dùng để đọc tệp bất kỳ trên máy chủ
    ALLOWED_VIDEO_EXTENSIONS: set[str] = {
        ".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv", ".m4v", ".ts", ".mpg", ".mpeg"
    }
    # Danh sách thư mục nguồn cho phép, ngăn cách bằng ';'. Trống = từ chối (ngăn đọc tệp trái phép)
    LOCAL_FILE_ALLOWED_DIRS: str = ""

    # API Security
    API_TOKEN: Optional[str] = None
    UPLOAD_CHUNK_SIZE_BYTES: int = Field(default=8*1024*1024, ge=65536, le=20*1024*1024)
    UPLOAD_SESSION_HOURS: int = Field(default=48, ge=1, le=168)
    MAX_ACTIVE_UPLOADS_PER_USER: int = Field(default=3, ge=1, le=20)
    MIN_FREE_DISK_START_BYTES: int = Field(default=1024*1024*1024, ge=0)
    MIN_FREE_DISK_RUNTIME_BYTES: int = Field(default=256*1024*1024, ge=0)
    MAX_SOURCE_VIDEO_DURATION_SECONDS: int = Field(default=12*3600, ge=1)
    MAX_SOURCE_RESOLUTION_HEIGHT: int = Field(default=0, ge=0)
    MEDIA_SAMPLE_TIMEOUT_SECONDS: int = Field(default=30, ge=1, le=300)
    TRUSTED_PROXY_IPS: list[str] = Field(default_factory=list)

    @field_validator('TRUSTED_PROXY_IPS')
    @classmethod
    def validate_proxy_ips(cls, values):
        import ipaddress
        for value in values:ipaddress.ip_address(value)
        return values

    SERVER_MODE: bool = False
    SERVER_PUBLIC_URL: str = ""

    @field_validator("SERVER_PUBLIC_URL")
    @classmethod
    def validate_server_public_url(cls, value):
        from urllib.parse import urlsplit
        value=value.strip()
        if not value:return value
        parsed=urlsplit(value)
        if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or "*" in parsed.netloc:
            raise ValueError("SERVER_PUBLIC_URL phải là địa chỉ HTTPS gốc, không kèm tài khoản, đường dẫn, query hay fragment.")
        _=parsed.port
        return value.rstrip("/")+"/"

    SERVER_SESSION_HOURS: int = Field(default=8, ge=1, le=72)
    SERVER_COOKIE_SECURE: bool = False
    SERVER_LEASE_SECONDS: int = Field(default=60, ge=15, le=300)
    SERVER_AUTO_BACKUP_HOURS: int = Field(default=24, ge=0, le=168)
    SERVER_BACKUP_DIRECTORY: Optional[str] = None
    SERVER_BACKUP_ROOT: Optional[str] = None
    SERVER_BACKUP_ALLOW_SAME_DISK: bool = False
    ALLOWED_HOSTS: str = "127.0.0.1,localhost,testserver"

    @property
    def allowed_hosts_list(self) -> list[str]:
        from urllib.parse import urlsplit
        hosts=[h.strip() for h in (self.ALLOWED_HOSTS or "").split(",") if h.strip()]
        if self.SERVER_PUBLIC_URL:hosts.append(urlsplit(self.SERVER_PUBLIC_URL).hostname)
        return list(dict.fromkeys(hosts))

    @property
    def storage_path(self) -> Path:
        p = Path(self.STORAGE_DIR)
        if not p.is_absolute():
            p = self.BASE_DIR / p
        p.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()
