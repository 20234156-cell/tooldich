from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, AliasChoices, ConfigDict


class ErrorResponse(BaseModel):
    """Định dạng lỗi chuẩn của toàn bộ REST API."""
    error_code: str
    message: str


class CreateProjectRequest(BaseModel):
    url: str = Field(..., description="URL video Douyin/Bilibili/TikTok hoặc đường dẫn file cục bộ")
    trim_start: Optional[str] = Field(
        default=None, description="Mốc bắt đầu, ví dụ 300 hoặc 5:00. Trống = từ đầu video"
    )
    trim_end: Optional[str] = Field(
        default=None, description="Mốc kết thúc, ví dụ 900 hoặc 15:00. Trống = tới hết video"
    )


class TrimRequest(BaseModel):
    trim_start: Optional[str] = Field(default=None)
    trim_end: Optional[str] = Field(default=None)


class CreateProjectResponse(BaseModel):
    project_id: str
    job_id: str
    platform: str
    status: str
    storage_path: str


class ProjectListItem(BaseModel):
    id: str
    title: str
    status: str
    platform: Optional[str] = None
    input_url: Optional[str] = None
    created_at: Optional[str] = None
    job_status: Optional[str] = None
    stage: Optional[str] = None
    progress: int = 0
    error_code: Optional[str] = None
    eta: Optional[Dict[str, Any]] = None


class VoiceTimingOptions(BaseModel):
    voice: Optional[str] = None
    rate: Optional[str] = '+0%'
    pitch: Optional[str] = '+0Hz'
    fit_to_video: bool = True
    shorten_to_fit: bool = True
    repair_existing: bool = False


class RunAllRequest(VoiceTimingOptions):
    """Tuỳ chọn cho việc chạy liên tiếp toàn bộ pipeline."""
    voice: Optional[str] = None
    subtitle_style: Optional[str] = None  # omitted: preserve saved style
    extraction_mode: Literal["hardsub", "auto", "stt"] = "hardsub"
    translation_provider: Literal["gemini", "local_hymt"] = "gemini"
    translation_model: Optional[str] = None
    translation_batch_size: Optional[int] = Field(default=None,ge=1,le=150)


class ExtractionRequest(BaseModel):
    extraction_mode: Literal["hardsub", "auto", "stt"] = "hardsub"


class VoiceoverRequest(VoiceTimingOptions):
    """Tuỳ chọn lồng tiếng Việt cho video."""
    voice: Optional[str] = None
    keep_original_audio: bool = True
    original_volume: Optional[float] = None
    voice_volume: Optional[float] = None
    fit_to_video: bool = True
    rate: Optional[str] = "+0%"
    pitch: Optional[str] = "+0Hz"


class RemixRequest(BaseModel):
    """Chỉnh cân bằng âm lượng cho bản nghe thử audio."""
    voice_volume: Optional[float] = Field(None, ge=0, le=4, description="Âm lượng giọng Việt")
    original_volume: Optional[float] = Field(None, ge=0, le=4, description="Âm lượng tiếng gốc")


class TaskResponse(BaseModel):
    """Phản hồi khi một công đoạn pipeline được đưa vào hàng đợi."""
    task_id: str
    project_id: str
    job_id: Optional[str] = None
    action: str
    state: str
    progress: int = 0
    stage: Optional[str] = None
    result: Optional[Any] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    error_details: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[float] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    elapsed_seconds: float = 0
    eta_seconds: Optional[float] = None
    estimated_finish_at: Optional[float] = None
    events: List[Dict[str, Any]] = Field(default_factory=list)
    owner: Optional[str] = None
    interrupted_stage: Optional[str] = None
    interrupted_reason_code: Optional[str] = None
    retained_summary: Dict[str, Any] = Field(default_factory=dict)


class JobStatusResponse(BaseModel):
    job_id: str
    project_id: str
    status: str
    stage: str
    progress: int
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    task: Optional[Dict[str, Any]] = None
    eta: Optional[Dict[str, Any]] = None
    logs: List[Dict[str, Any]] = Field(default_factory=list)


# ----------------- Fast Hardsub Schemas -----------------

class SaveROIRequest(BaseModel):
    rois: List[Dict[str, Any]] = Field(default_factory=list)


class OCRPreviewRequest(BaseModel):
    sample_seconds: float = Field(default=30.0, description="Độ dài trích mẫu thử")
    start_time: float = Field(default=0.0, description="Mốc thời gian bắt đầu trích mẫu")


class TranslateBatchRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    cues: Optional[List[int]] = Field(default=None,min_length=1,validation_alias=AliasChoices('cues','target_cues'), description="Danh sách index các cue cần dịch; None = dịch toàn bộ")
    model: Optional[str] = None
    batch_size: Optional[int] = Field(default=None,ge=1,le=150)
    tone: str = "natural"
    provider: Literal["gemini", "local_hymt"] = "gemini"


class RenderOptionsRequest(BaseModel):
    style: str = Field(default="cover", description="cover | clean | bilingual")
    font_family: Optional[str] = Field(default='Arial', validation_alias=AliasChoices('font_family', 'font_name'))
    font_size: Optional[int] = 48
    font_color: Optional[str] = "&H00FFFFFF"
    outline_color: Optional[str] = "&H00000000"
    outline_width: Optional[int] = 2
    font_bold: bool = True
    font_italic: bool = False
    font_shear: float = Field(default=0, ge=0, le=.5)
    uppercase: bool = False
    font_scale_x: int = Field(default=100, ge=50, le=120)
    prefer_single_line: bool = False
    text_width_percent: int = Field(default=100, ge=30, le=100)
    cover_full_width: bool = False
    cover_effect: Literal['solid', 'blur'] = 'solid'
    blur_strength: int = Field(default=24, ge=1, le=100)
    box_color: Optional[str] = "black"
    box_opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    box_padding: Optional[int] = 6
    margin_v: Optional[int] = Field(default=None, ge=0, le=500)
    preset: str = Field(default='p4', validation_alias=AliasChoices('preset', 'encoder_preset'))
    voice_volume: Optional[float] = 1.7
    bg_volume: Optional[float] = 0.15
    audio_mode: Literal['mix', 'voice_only', 'original_only'] = 'mix'
    use_nvenc: bool = True

    def to_render_config(self):
        from src.pipeline.render import RenderConfig
        values = self.model_dump(exclude_none=True)
        values['font_name'] = values.pop('font_family', 'Arial')
        values['encoder_preset'] = values.pop('preset', 'p4')
        return RenderConfig.model_validate(values)


class KeyPoolEntry(BaseModel):
    id: str
    masked_key: str
    status: str
    latency_ms: Optional[float] = None
    last_error: Optional[str] = None


class AddKeysRequest(BaseModel):
    keys: str = Field(..., min_length=1, description="Gemini API keys separated by newlines or commas")


class KeyProjectRequest(BaseModel):
    project_id: str = Field(default='', max_length=100, pattern=r'^[A-Za-z0-9:_-]*$')


class SplitCueRequest(BaseModel):
    split_time: float
    text1: Optional[str] = ""
    text2: Optional[str] = ""


class MergeCueRequest(BaseModel):
    target_cue_index: int


class FindReplaceRequest(BaseModel):
    find_text: str
    replace_text: str
    match_case: bool = False


class SingleCueTTSRequest(BaseModel):
    voice: Optional[str] = "vi-VN-HoaiMyNeural"
    rate: Optional[str] = "+0%"

