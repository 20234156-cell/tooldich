"""Durable Auto intent; manual review pauses one stage, not the workflow."""
import json
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time
from config.settings import settings
from src.atomic_files import replace_with_retry

lock = threading.RLock()


def subtitle_identity(project_id):
    target = path(project_id).parent / 'source' / 'subtitle_settings.json'
    return hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None


def resume_options(project_id, data):
    options = dict(data['options'])
    current = subtitle_identity(project_id)
    if current is not None and current != data.get('subtitle_settings_hash'):
        # A reviewed Studio configuration takes precedence over an old preset.
        for key in ('subtitle_preset','subtitle_style','audio_mode','voice_volume','bg_volume',
                    'font_family','font_color','outline_color','outline_width','font_size'):
            options.pop(key, None)
    return options


def path(project_id):
    from src.security.validators import validate_identifier
    return settings.storage_path / validate_identifier(project_id, 'Project ID') / 'auto_workflow.json'


def read(project_id):
    target = path(project_id)
    if not target.exists(): return None
    try:
        data = json.loads(target.read_text(encoding='utf-8'))
        if data.get('version') != 1 or not isinstance(data.get('options'), dict):
            raise ValueError('Invalid Auto profile')
        return data
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError('Thiết lập Auto đã lưu bị lỗi; chọn lại thiết lập và bấm Chạy tự động.') from exc


def write(project_id, data):
    write_raw(project_id, json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))


def write_raw(project_id, content):
    target = path(project_id); target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('wb', dir=target.parent, delete=False) as stream:
            temporary = stream.name
            stream.write(content)
            stream.flush(); os.fsync(stream.fileno())
        replace_with_retry(temporary, target)
    finally:
        if temporary: Path(temporary).unlink(missing_ok=True)


def update(project_id, generation, **changes):
    with lock:
        data = read(project_id)
        if data and data.get('generation') == generation:
            data.update(changes, updated_at=time.time()); write(project_id, data)


def review_details(details):
    """Persist only cue identifiers needed to explain a stopped Auto run."""
    if not isinstance(details, dict):
        return {}
    result = {}
    for field in ('cue_indices', 'timing_cues', 'timing_cause_cues', 'creator_credit_cues'):
        ids = details.get(field)
        if isinstance(ids, (list, tuple)):
            result[field] = sorted({cue for cue in ids if type(cue) is int and cue >= 0})
    source_rows = details.get('source_review_cues')
    if isinstance(source_rows, list):
        result['source_review_cues'] = [
            {key: row[key] for key in ('cue_id', 'start', 'end', 'reason')}
            for row in source_rows if isinstance(row, dict)
            and type(row.get('cue_id')) is int
            and type(row.get('start')) in (int, float)
            and type(row.get('end')) in (int, float)
            and row.get('reason') in {'temporal_vote_unresolved', 'source_review_required'}
        ]
    for key in ('excluded_cues_with_reason', 'omitted_uncertain_cues', 'deferred_unexamined_cues'):
        if type(details.get(key)) is int and details[key] >= 0:
            result[key] = details[key]
    clusters=details.get('timing_clusters')
    if isinstance(clusters,list):
        result['timing_clusters']=[]
        for cluster in clusters[:500]:
            if not isinstance(cluster,dict):continue
            ids=cluster.get('cau',[])
            if not isinstance(ids,list) or not all(type(i) is int and i>=0 for i in ids):continue
            conflicts=[dict(cue_id=c['cue_id'],reason=c.get('reason','unknown'),missing_seconds=c['missing_seconds'])
                       for c in cluster.get('conflicts',[]) if isinstance(c,dict)
                       and type(c.get('cue_id')) is int and type(c.get('missing_seconds')) in (int,float)]
            result['timing_clusters'].append(dict(cum_id=str(cluster.get('cum_id',''))[:100],
                cau=ids,trang_thai=cluster.get('trang_thai'),conflicts=conflicts))
    return result


def guidance(data):
    from src.jobs.logger import is_api_limit_error

    code = data.get('error_code') or ''
    if data.get('state') == 'interrupted':
        return {'tab': 'workflow', 'message': 'Lượt Auto trước đã gián đoạn. Bấm Tiếp tục toàn bộ Auto để kiểm tra kết quả đã lưu và chạy các bước còn thiếu.'}
    if code == 'CANCELLED' or code == 'USER_PAUSE':
        return {'tab': 'workflow', 'message': 'Bạn đã dừng lượt Auto này. Bấm Tiếp tục toàn bộ Auto khi muốn chạy tiếp.'}
    if is_api_limit_error(code, data.get('error_message') or ''):
        return {'tab': 'trans', 'message': 'API đang giới hạn lượt gọi. Khi có hạn mức trở lại, bấm Tiếp tục toàn bộ Auto để chạy các bước còn thiếu với thiết lập đã lưu.'}
    if code == 'OCR_REVIEW_REQUIRED':
        return {'tab': 'ocr', 'message': 'Đối chiếu câu được báo với ảnh hoặc tiếng gốc. Sửa câu sai, xác nhận câu đúng hoặc bỏ qua câu đã kiểm tra là chữ thừa. Bấm Phê duyệt OCR để Auto chạy tiếp.'}
    if code == 'TIMING_REVIEW_REQUIRED':
        if data.get('review',{}).get('timing_clusters'):
            return {'tab':'voice','message':'Auto đã chạy phục hồi theo cụm. Xem nguyên nhân và số giây thiếu được đo ở từng câu; chưa có lịch đầy đủ thì chưa render. Phần đã đạt và WAV hợp lệ được giữ để tiếp tục.'}
        return {'tab': 'voice', 'message': 'Ưu tiên kiểm tra các câu gây trễ. Nếu câu đúng nhưng quá dài, rút gọn bản dịch và giữ đủ ý. Nếu là chữ thừa, đối chiếu nguồn trước khi bỏ. Bấm Tiếp tục toàn bộ Auto để tạo lại giọng cần thiết và đo timing trước khi render.'}
    if code == 'QA_REVIEW_REQUIRED':
        return {'tab': 'verify', 'message': 'Mở kết quả kiểm tra QA để xem mục chưa đạt. Sửa mục được báo rồi bấm Tiếp tục toàn bộ Auto để kiểm tra lại đầu ra.'}
    if code == 'TRANSCRIPT_NOT_FOUND':
        return {'tab': 'ocr', 'message': 'Kiểm tra danh sách phụ đề và vùng nhận dạng. Nếu chưa có câu hợp lệ, bóc lại nguồn rồi kiểm tra kết quả trước khi tiếp tục Auto.'}
    if code in {'OCR_UNAVAILABLE', 'STT_MODEL_UNAVAILABLE'}:
        return {'tab': 'ocr', 'message': 'Bộ nhận dạng chưa sẵn sàng. Kiểm tra công cụ OCR hoặc model nhận dạng lời nói theo chế độ đã chọn, rồi tiếp tục Auto.'}
    if code in {'MEDIA_CORRUPTED', 'NO_VIDEO_STREAM', 'UNSUPPORTED_CODEC'}:
        return {'tab': 'workflow', 'message': 'Video nguồn không đọc được. Kiểm tra file có phát được và tải hoặc nhập lại đúng nguồn trước khi tiếp tục Auto.'}
    if code == 'STORAGE_ERROR':
        return {'tab': 'workflow', 'message': 'Ứng dụng chưa lưu được kết quả. Kiểm tra dung lượng và quyền ghi thư mục dự án, rồi tải lại trạng thái trước khi tiếp tục Auto.'}
    if code == 'LOW_DISK_SPACE':
        return {'tab': 'workflow', 'message': 'Ổ đĩa không đủ dung lượng an toàn. Giải phóng chỗ trống, giữ video nguồn và các file đang dùng, rồi bấm Tiếp tục Auto.'}
    if code in {'OUT_OF_MEMORY','GPU_OOM','LOW_MEMORY'}:
        return {'tab': 'workflow', 'message': 'Thiếu RAM/VRAM. Đóng tác vụ nặng khác rồi tiếp tục. Nếu đổi model/giọng, chọn Dùng thiết lập Studio hiện tại.'}
    if code in {'SOURCE_UNAVAILABLE','DOWNLOAD_NOT_ALLOWED','NETWORK_ERROR','MEDIA_NOT_FOUND'}:
        return {'tab': 'workflow', 'message': 'Kiểm tra link nguồn, kết nối và quyền truy cập video. Tải/nhập nguồn hợp lệ rồi tiếp tục Auto; không cần sửa phụ đề đã duyệt.'}
    if 'KEY' in code or 'QUOTA' in code or 'RATE_LIMIT' in code:
        return {'tab': 'trans', 'message': 'Kiểm tra API key/hạn mức dịch. Khi dịch vụ sẵn sàng, bấm Tiếp tục Auto; kết quả khớp đã hoàn tất được giữ.'}
    stage = (code+' '+str(data.get('last_stage', ''))).lower()
    if 'translat' in stage or 'dịch' in stage or 'dich' in stage:
        return {'tab': 'trans', 'message': 'Kiểm tra lỗi dịch/model/API và các câu được báo. Sau khi xử lý, bấm Tiếp tục Auto bằng thiết lập đã lưu.'}
    if 'tts' in stage or 'voice' in stage or 'giọng' in stage:
        return {'tab': 'voice', 'message': 'Kiểm tra giọng, file âm thanh và câu được báo. Sau khi xử lý, bấm Tiếp tục Auto; các câu có cache hợp lệ được giữ.'}
    if 'render' in stage or 'ffmpeg' in stage or 'nvenc' in stage:
        return {'tab': 'render', 'message': 'Kiểm tra lỗi render, dung lượng ổ đĩa và bộ mã hóa. Sau khi xử lý, bấm Tiếp tục Auto; nguồn và phụ đề đã duyệt được giữ.'}
    return {'tab': 'workflow', 'message': 'Xem thông báo và bước dừng, xử lý nguyên nhân rồi bấm Tiếp tục Auto. Ứng dụng kiểm tra checkpoint trước khi dùng.'}


def content_warnings(project_id):
    """Expose recorded quality caveats even after automatic technical approval."""
    from src.pipeline.orchestrator import orchestrator
    state = orchestrator.get_state(project_id)
    stage = state.stages.get('TRANSLATED')
    if stage is None:
        return ''
    flags = set(stage.degraded or [])
    lines = []
    if 'translation_provider_fallback' in flags:
        lines.append('Bản dịch đã dùng nguồn dự phòng; cần kiểm nghĩa.')
    if 'ai_source_corrections_review' in flags:
        lines.append('AI đã sửa chữ nguồn; cần đối chiếu ảnh/tiếng gốc.')
    if lines:
        lines.insert(0, '⚠️ Cần kiểm nội dung: duyệt tự động/VERIFIED không thay thế người xem.')
    return '\n'.join(lines)
