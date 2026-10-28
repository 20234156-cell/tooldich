"""Phát hiện GPU NVIDIA và chọn cấu hình chạy nhanh nhất cho máy hiện tại.

faster-whisper (CTranslate2) cần cuBLAS + cuDNN của CUDA 12. Trên Windows cài bằng
`pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12>=9,<10"` - các DLL nằm trong
site-packages/nvidia/*/bin nhưng Windows không tự tìm thấy, nên phải đăng ký thư
mục đó trước khi CTranslate2 nạp thư viện.
"""

import functools
import glob
import os
import site
import subprocess
import sys
from typing import List, Optional, Tuple

from config.settings import settings

_registered = False


def register_cuda_dlls() -> List[str]:
    """Thêm các thư mục DLL CUDA cài qua pip vào đường tìm kiếm của tiến trình."""
    global _registered
    if _registered or sys.platform != "win32":
        return []
    _registered = True

    roots = list(site.getsitepackages())
    try:
        roots.append(site.getusersitepackages())
    except Exception:
        pass

    added = []
    for root in roots:
        for bin_dir in glob.glob(os.path.join(root, "nvidia", "*", "bin")):
            try:
                os.add_dll_directory(bin_dir)
            except (OSError, AttributeError):
                continue
            # CTranslate2 nạp cuBLAS/cuDNN lúc chạy (LoadLibrary theo tên), đường này
            # chỉ đọc PATH chứ không đọc add_dll_directory - nên phải thêm cả hai.
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
            added.append(bin_dir)
    return added


@functools.lru_cache(maxsize=1)
def cuda_available() -> bool:
    """Có GPU NVIDIA mà CTranslate2 dùng được không."""
    register_cuda_dlls()
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def whisper_runtime() -> Tuple[str, str]:
    """(device, compute_type) cho Whisper, tự chọn khi cấu hình để 'auto'.

    Đo trên GTX 1050 Ti, audio 166s, model base, beam 3:
      cuda int8 9.4s · cuda float32 10.2s · cuda int8_float32 11.2s · cpu int8 34.3s
    GPU đời Pascal (GTX 10xx) không có float16 nhanh nên int8 là lựa chọn tốt nhất;
    GPU đời mới hơn có float16 thì dùng int8_float16.
    """
    device = (settings.WHISPER_DEVICE or "auto").lower()
    compute = (settings.WHISPER_COMPUTE_TYPE or "auto").lower()

    if device == "auto":
        device = "cuda" if cuda_available() else "cpu"
    if device == "cuda":
        register_cuda_dlls()

    if compute == "auto":
        compute = "int8"
        if device == "cuda":
            try:
                import ctranslate2

                if "int8_float16" in ctranslate2.get_supported_compute_types("cuda"):
                    compute = "int8_float16"
            except Exception:
                pass
    return device, compute


@functools.lru_cache(maxsize=4)
def nvenc_available(ffmpeg_bin: str) -> bool:
    """ffmpeg có encode được bằng NVENC trên máy này không (thử thật 1 khung hình).

    Có tên encoder trong `ffmpeg -encoders` chưa đủ: bản ffmpeg mới đòi driver mới
    hơn cả driver cuối cùng mà GPU cũ được hỗ trợ, khi đó encoder có mặt nhưng
    không mở được.
    """
    try:
        res = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=black:s=256x256:d=0.1",
             "-frames:v", "1", "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True, timeout=20,
        )
        return res.returncode == 0
    except Exception:
        return False


def invalidate_nvenc_cache() -> None:
    """Xoá cache probe NVENC khi gặp lỗi encode NVENC lúc chạy thật."""
    nvenc_available.cache_clear()


def h264_encoder_args(
    ffmpeg_bin: str, crf: int = 22, preset: Optional[str] = None
) -> List[str]:
    """Tham số encode H.264: NVENC nếu có và dùng được, không thì libx264."""
    mode = (settings.VIDEO_ENCODER or "auto").lower()
    if mode in ("auto", "nvenc") and nvenc_available(str(ffmpeg_bin)):
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq",
                "-rc", "vbr", "-cq", "23", "-b:v", "0"]
    return ["-c:v", "libx264", "-preset", preset or settings.SUBTITLE_BURN_PRESET,
            "-crf", str(crf)]


def describe() -> str:
    """Một dòng mô tả phần cứng đang dùng, để in lúc khởi động server."""
    device, compute = whisper_runtime()
    name = ""
    if device == "cuda":
        try:
            res = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5,
            )
            name = res.stdout.strip().splitlines()[0] if res.returncode == 0 else ""
        except Exception:
            name = ""
    label = f"GPU {name}".strip() if device == "cuda" else "CPU"
    return f"Whisper: {label} ({compute})"
