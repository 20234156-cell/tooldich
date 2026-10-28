import datetime
import enum
from typing import List, Optional
from sqlalchemy import (
    Column,
    String,
    Integer,
    Float,
    Text,
    DateTime,
    ForeignKey,
    Enum,
    Boolean,
    JSON,
)
from sqlalchemy.orm import DeclarativeBase, relationship, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ServerRecord(Base):
    """Additive server-control schema; no changes to authoritative project tables."""
    __tablename__ = 'server_records'
    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[float] = mapped_column(Float, default=0)


class JobStatus(str, enum.Enum):
    PENDING = "PENDING"
    RESOLVING = "RESOLVING"
    DOWNLOADING = "DOWNLOADING"
    DOWNLOADED = "DOWNLOADED"
    PROCESSING = "PROCESSING"
    EXTRACTING_AUDIO = "EXTRACTING_AUDIO"
    TRANSCRIBING = "TRANSCRIBING"
    TRANSLATING = "TRANSLATING"
    TRANSCRIBED = "TRANSCRIBED"
    ANALYZING = "ANALYZING"
    ANALYZED = "ANALYZED"
    DUBBING = "DUBBING"
    DUBBED = "DUBBED"
    GENERATING_SCRIPT = "GENERATING_SCRIPT"
    SCRIPT_READY = "SCRIPT_READY"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    WAITING_REVIEW = "WAITING_REVIEW"
    EXPORTING = "EXPORTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_upload_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, unique=True)
    title: Mapped[str] = mapped_column(String(255), default="Untitled Project")
    status: Mapped[str] = mapped_column(
        String(32), default=JobStatus.PENDING.value, index=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow, index=True
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    # Relationships
    source: Mapped[Optional["Source"]] = relationship(
        "Source", back_populates="project", uselist=False, cascade="all, delete-orphan"
    )
    media: Mapped[Optional["Media"]] = relationship(
        "Media", back_populates="project", uselist=False, cascade="all, delete-orphan"
    )
    transcripts: Mapped[List["Transcript"]] = relationship(
        "Transcript", back_populates="project", cascade="all, delete-orphan"
    )
    scenes: Mapped[List["Scene"]] = relationship(
        "Scene", back_populates="project", cascade="all, delete-orphan"
    )
    scripts: Mapped[List["Script"]] = relationship(
        "Script", back_populates="project", cascade="all, delete-orphan"
    )
    jobs: Mapped[List["Job"]] = relationship(
        "Job", back_populates="project", cascade="all, delete-orphan"
    )
    exports: Mapped[List["Export"]] = relationship(
        "Export", back_populates="project", cascade="all, delete-orphan"
    )


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id"), unique=True
    )
    platform: Mapped[str] = mapped_column(String(32))  # douyin, bilibili, local_file
    input_url: Mapped[str] = mapped_column(Text)
    resolved_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    video_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    title: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    author: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    duration: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    thumbnail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_metadata: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    project: Mapped["Project"] = relationship("Project", back_populates="source")


class Media(Base):
    __tablename__ = "media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id"), unique=True
    )
    file_path: Mapped[str] = mapped_column(Text)
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    duration: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    width: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    fps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    video_codec: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    audio_codec: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    checksum_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=False)

    project: Mapped["Project"] = relationship("Project", back_populates="media")


class Transcript(Base):
    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id"), index=True
    )
    language: Mapped[str] = mapped_column(String(16), default="zh")
    target_language: Mapped[str] = mapped_column(String(16), default="vi")
    segments: Mapped[list] = mapped_column(JSON, default=list)
    raw_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    srt_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    project: Mapped["Project"] = relationship("Project", back_populates="transcripts")


class Scene(Base):
    __tablename__ = "scenes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id"), index=True
    )
    scene_number: Mapped[int] = mapped_column(Integer)
    start_time: Mapped[float] = mapped_column(Float)
    end_time: Mapped[float] = mapped_column(Float)
    duration: Mapped[float] = mapped_column(Float)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    camera_shot: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    lighting: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    action: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ocr_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    project: Mapped["Project"] = relationship("Project", back_populates="scenes")


class Script(Base):
    __tablename__ = "scripts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id"), index=True
    )
    concept_type: Mapped[str] = mapped_column(
        String(64), default="Transformative Adaptation"
    )
    title: Mapped[str] = mapped_column(String(255))
    duration: Mapped[float] = mapped_column(Float, default=60.0)
    aspect_ratio: Mapped[str] = mapped_column(String(16), default="9:16")
    content_json: Mapped[dict] = mapped_column(JSON, default=dict)
    generation_plan_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False)

    project: Mapped["Project"] = relationship("Project", back_populates="scripts")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id"), index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default=JobStatus.PENDING.value, index=True
    )
    current_attempt_id: Mapped[int] = mapped_column(Integer, default=0)
    active_attempt_id: Mapped[int] = mapped_column(Integer, default=0)
    current_fencing_token: Mapped[int] = mapped_column(Integer, default=0)
    input_config_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    interrupted_stage: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    interrupted_reason_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    retained_summary: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    current_stage: Mapped[str] = mapped_column(String(64), default="INIT")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    project: Mapped["Project"] = relationship("Project", back_populates="jobs")
    logs: Mapped[List["JobLog"]] = relationship(
        "JobLog", back_populates="job", cascade="all, delete-orphan"
    )


class JobLog(Base):
    __tablename__ = "job_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("jobs.id"), index=True
    )
    timestamp: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow
    )
    stage: Mapped[str] = mapped_column(String(64))
    level: Mapped[str] = mapped_column(String(16), default="INFO")
    message: Mapped[str] = mapped_column(Text)
    duration: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    job: Mapped["Job"] = relationship("Job", back_populates="logs")


class Export(Base):
    __tablename__ = "exports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id"))
    export_path: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow
    )
    summary_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    project: Mapped["Project"] = relationship("Project", back_populates="exports")


class APIRequest(Base):
    __tablename__ = "api_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    video_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    cloud_project: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    key_id: Mapped[str] = mapped_column(String(64), index=True)
    key_suffix: Mapped[str] = mapped_column(String(4))
    model: Mapped[str] = mapped_column(String(128), index=True)
    operation_id: Mapped[str] = mapped_column(String(36), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(32), default="pending")
    http_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    retry_after_s: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    input_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cached_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reasoning_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class CloudConnection(Base):
    __tablename__ = 'cloud_connections'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    encrypted_data: Mapped[str] = mapped_column(Text)


class CloudUpload(Base):
    __tablename__ = 'cloud_uploads'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    account_id: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default='PENDING')
    file_size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    md5: Mapped[str] = mapped_column(String(32))
    remote_id: Mapped[str] = mapped_column(String(128))
    encrypted_session: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    uploaded_bytes: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    verified_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    local_removed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow)


class UploadSession(Base):
    __tablename__ = 'upload_sessions'
    upload_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_username: Mapped[str] = mapped_column(String(40), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    total_size: Mapped[int] = mapped_column(Integer)
    chunk_size: Mapped[int] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String(64), default='')
    expected_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    received_offset: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default='UPLOADING')
    created_project_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, unique=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float)


class UploadChunk(Base):
    __tablename__ = 'upload_chunks'
    upload_id: Mapped[str] = mapped_column(String(64), ForeignKey('upload_sessions.upload_id'), primary_key=True)
    start_offset: Mapped[int] = mapped_column(Integer, primary_key=True)
    end_offset: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))


class ProcessingAttempt(Base):
    __tablename__='processing_attempts'
    id: Mapped[str] = mapped_column(String(128),primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64),index=True)
    job_id: Mapped[str] = mapped_column(String(64),index=True)
    kind: Mapped[str] = mapped_column(String(32))
    number: Mapped[int] = mapped_column(Integer)
    fence: Mapped[int] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String(64))
    inputs: Mapped[dict] = mapped_column(JSON)
    options: Mapped[dict] = mapped_column(JSON)
    directory: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32),default='STAGING')
    manifest: Mapped[Optional[dict]] = mapped_column(JSON,nullable=True)
    created_at: Mapped[float] = mapped_column(Float)


class ArtifactPointer(Base):
    __tablename__='artifact_pointers'
    project_id: Mapped[str] = mapped_column(String(64),primary_key=True)
    kind: Mapped[str] = mapped_column(String(32),primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String(128))
    directory: Mapped[str] = mapped_column(Text)


class FileBaseline(Base):
    __tablename__ = 'file_baselines'
    path: Mapped[str] = mapped_column(Text, primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    provenance: Mapped[str] = mapped_column(String(40))
    recorded_at: Mapped[float] = mapped_column(Float)


class LegacyBackupReference(Base):
    __tablename__ = 'legacy_backup_references'
    path: Mapped[str] = mapped_column(Text, primary_key=True)
    reference: Mapped[str] = mapped_column(Text)
    sha256: Mapped[Optional[str]] = mapped_column(String(64),nullable=True)
    size: Mapped[Optional[int]] = mapped_column(Integer,nullable=True)


class FileLifecycle(Base):
    __tablename__='file_lifecycles'
    path: Mapped[str] = mapped_column(Text,primary_key=True)
    state: Mapped[str] = mapped_column(String(32))
    candidate_id: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[float] = mapped_column(Float)
    error: Mapped[Optional[dict]] = mapped_column(JSON,nullable=True)


class CleanupBatch(Base):
    __tablename__='cleanup_batches'
    id: Mapped[str] = mapped_column(String(64),primary_key=True)
    actor: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float)


class CleanupCandidate(Base):
    __tablename__='cleanup_candidates'
    id: Mapped[str] = mapped_column(String(64),primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(64),index=True)
    kind: Mapped[str] = mapped_column(String(32))
    owner_id: Mapped[str] = mapped_column(String(128))
    path: Mapped[str] = mapped_column(Text)
    expected_state: Mapped[str] = mapped_column(String(32))
    stamp: Mapped[dict] = mapped_column(JSON)
    reason_code: Mapped[str] = mapped_column(String(64))
    blocked_reason: Mapped[Optional[str]] = mapped_column(Text,nullable=True)
    result: Mapped[str] = mapped_column(String(64),default='PREVIEW')


class StageResetAudit(Base):
    __tablename__='stage_reset_audits'
    id: Mapped[str] = mapped_column(String(64),primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(40))
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[float] = mapped_column(Float)
