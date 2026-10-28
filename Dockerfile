FROM python:3.12-slim
# Đối với hệ thống có GPU NVIDIA muốn dùng CUDA/NVENC đầy đủ trong Docker,
# có thể dùng base image: nvidia/cuda:12.4.1-runtime-ubuntu22.04 kết hợp Python 3.12

# ffmpeg/ffprobe là bắt buộc cho pipeline media; tesseract là tuỳ chọn cho OCR
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        tesseract-ocr \
        tesseract-ocr-chi-sim \
        tesseract-ocr-eng \
        tesseract-ocr-vie \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cài dependency trước để tận dụng cache tầng image khi chỉ sửa code
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Chạy bằng user thường, không dùng root
RUN useradd --create-home --uid 10001 appuser && \
    mkdir -p /app/storage/projects && \
    chown -R appuser:appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_URL=sqlite:///./storage/app.db \
    STORAGE_DIR=./storage/projects

EXPOSE 8088

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8088/api/projects', timeout=4)" || exit 1

CMD ["uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "8088"]
