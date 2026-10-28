import logging
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional

from config.settings import settings
from src.database.connection import SessionLocal
from src.database.models import Project

logger = logging.getLogger("app.cleanup")


def cleanup_stale_files(
    storage_dir: Optional[Path] = None,
    max_part_age_seconds: float = 3600.0,
    min_orphan_age_seconds: float = 300.0,
) -> Dict[str, List[str]]:
    """Tự động dọn dẹp các tệp .part / .part.bak cũ và thư mục proj_* không có trong DB.

    - max_part_age_seconds: Tệp phân đoạn .part dở dang quá thời gian này sẽ bị xoá.
    - min_orphan_age_seconds: Thư mục dự án không có trong DB và đã tạo lâu hơn mốc này sẽ bị xoá.
    """
    base_storage = Path(storage_dir or settings.storage_path)
    if settings.SERVER_MODE:
        return {'deleted_parts':[],'deleted_projects':[],'reason':'ADMIN_PREVIEW_REQUIRED'}
    if not base_storage.exists():
        return {"deleted_parts": [], "deleted_projects": []}

    protected=(settings.storage_path.parent/"uploads/sessions").resolve()
    now = time.time()
    deleted_parts: List[str] = []
    deleted_projects: List[str] = []

    # 1. Quét và dọn các tệp .part, .part.bak, .turbo.part
    for part_file in base_storage.rglob("*.part*"):
        try:
            if part_file.resolve().is_relative_to(protected):continue
            if part_file.is_file():
                age = now - part_file.stat().st_mtime
                if age >= max_part_age_seconds:
                    part_file.unlink(missing_ok=True)
                    deleted_parts.append(str(part_file))
        except Exception as e:
            logger.warning(f"Không thể xoá tệp tạm {part_file}: {e}")

    # 2. Quét các thư mục proj_* không có trong cơ sở dữ liệu
    try:
        with SessionLocal() as db:
            valid_ids = {row[0] for row in db.query(Project.id).all()}
    except Exception as e:
        logger.warning(f"Không thể truy vấn danh sách project từ DB để dọn dẹp: {e}")
        return {"deleted_parts": deleted_parts, "deleted_projects": []}

    # Quét các thư mục con cấp 1 (trong base_storage)
    for p_dir in base_storage.iterdir():
        try:
            if p_dir.is_dir() and p_dir.name.startswith("proj_"):
                if p_dir.name not in valid_ids:
                    age = now - p_dir.stat().st_mtime
                    if age >= min_orphan_age_seconds:
                        shutil.rmtree(p_dir, ignore_errors=True)
                        deleted_projects.append(p_dir.name)
        except Exception as e:
            logger.warning(f"Không thể xoá thư mục mồ côi {p_dir}: {e}")

    if deleted_parts or deleted_projects:
        logger.info(
            f"Dọn dẹp hệ thống hoàn tất: {len(deleted_parts)} tệp tạm .part, "
            f"{len(deleted_projects)} thư mục dự án mồ côi ({', '.join(deleted_projects) or 'none'})."
        )

    return {
        "deleted_parts": deleted_parts,
        "deleted_projects": deleted_projects,
    }
