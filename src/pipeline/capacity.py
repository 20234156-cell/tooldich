"""Conservative remaining-space planning, not a promise of encoded file size."""
import math
from pathlib import Path

GIB = 1024 ** 3


def estimate_remaining(duration, source_bytes, *, profile=None, audio_mode=None, reserve_gib=8,
                       analysis_needed=True, voice_needed=True, render_needed=True):
    duration = float(duration)
    if not math.isfinite(duration) or duration <= 0 or source_bytes < 0:
        raise ValueError('Invalid media dimensions for storage planning')
    # Source already exists: budget additional proxy/images, voice generations,
    # two simultaneous render generations, and a free-space reserve.
    output_bps = {'compact': 2_500_000, 'fast': 8_000_000,
                  'balanced': 6_000_000, 'quality': 12_000_000}.get(profile, 8_000_000)
    render = max(source_bytes, math.ceil(duration * output_bps / 8)) * 2 if render_needed else 0
    voice = 0 if audio_mode == 'original_only' or not voice_needed else math.ceil(duration * 48000 * 2 * 2 * 3)
    analysis = math.ceil(duration * 350_000) if analysis_needed else 0
    reserve = math.ceil(max(2, reserve_gib) * GIB)
    return dict(render_bytes=render, voice_bytes=voice, analysis_bytes=analysis,
                reserve_bytes=reserve, required_free_bytes=render+voice+analysis+reserve,
                estimated=True)


def project_capacity(project_id, *, profile=None, audio_mode=None, **stages):
    from config.settings import settings
    from src.database import connection
    from src.database.models import Media
    from src.pipeline.media_storage import storage_free_bytes
    with connection.SessionLocal() as db:
        media = db.query(Media).filter_by(project_id=project_id).first()
        if media is None or not media.duration or not Path(media.file_path).is_file():
            return None
        duration, size = media.duration, Path(media.file_path).stat().st_size
    result = estimate_remaining(duration, size, profile=profile, audio_mode=audio_mode,
                                reserve_gib=settings.AUTO_DISK_GUARD_MIN_FREE_GB, **stages)
    result['free_bytes'] = storage_free_bytes()
    result['sufficient'] = result['free_bytes'] >= result['required_free_bytes']
    return result


def require_capacity(project_id, *, profile=None, audio_mode=None, **stages):
    from src.jobs.logger import PipelineException
    result = project_capacity(project_id, profile=profile, audio_mode=audio_mode, **stages)
    if result and not result['sufficient']:
        raise PipelineException('LOW_DISK_SPACE',
            f"Dự toán chỗ trống còn cần {result['required_free_bytes']/GIB:.1f} GiB "
            f"(gồm dự phòng); hiện có {result['free_bytes']/GIB:.1f} GiB. "
            'Chọn compact hoặc giải phóng media đã sao lưu rồi tiếp tục. Đây là dự toán, không tự xóa dữ liệu.')
    return result
