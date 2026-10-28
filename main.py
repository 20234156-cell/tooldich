"""Command-line interface for the Fast Hardsub Dubbing Studio."""

import json
import sys
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel

from config.settings import settings
from src.jobs.logger import PipelineException
from src.jobs.system import manager
from src.pipeline.orchestrator import orchestrator
from src.pipeline.render import RenderConfig

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

app = typer.Typer(help="Fast Hardsub: tải video, OCR sub Trung, dịch, lồng tiếng và render.")
console = Console(highlight=False)


def _fail(exc: Exception) -> None:
    if isinstance(exc, PipelineException):
        console.print(f"[bold red][{exc.code}][/bold red] {exc.message}")
    else:
        console.print(f"[bold red][ERROR][/bold red] {exc}")
    raise typer.Exit(code=1)


def _progress(percent: int, stage: str) -> None:
    console.print(f"[{max(0, min(100, percent)):3d}%] {stage}")


@app.command("add")
def add_project(
    source: str = typer.Argument(..., help="URL Douyin/Bilibili/TikTok hoặc file video cục bộ"),
    trim_start: Optional[str] = typer.Option(None, "--trim-start"),
    trim_end: Optional[str] = typer.Option(None, "--trim-end"),
):
    """Tạo dự án mới; chưa tự chạy các công đoạn nặng."""
    try:
        result = manager.create_project(source, trim_start=trim_start, trim_end=trim_end)
        console.print_json(json.dumps(result, ensure_ascii=False, default=str))
    except Exception as exc:
        _fail(exc)


@app.command("download")
def download(project_id: str):
    """Tải và kiểm tra video nguồn."""
    try:
        result = manager.download_and_validate_project(project_id, quiet=False)
        console.print_json(json.dumps(result, ensure_ascii=False, default=str))
    except Exception as exc:
        _fail(exc)


@app.command("roi")
def set_roi(
    project_id: str,
    x: float = typer.Option(..., min=0.0, max=1.0),
    y: float = typer.Option(..., min=0.0, max=1.0),
    width: float = typer.Option(..., min=0.001, max=1.0),
    height: float = typer.Option(..., min=0.001, max=1.0),
):
    """Lưu ROI phụ đề bằng tọa độ chuẩn hóa 0..1."""
    try:
        result = orchestrator.save_roi_set(project_id, [{
            "id": "roi_sub", "type": "chinese_subtitle",
            "x": x, "y": y, "width": width, "height": height,
        }])
        console.print_json(result.model_dump_json())
    except Exception as exc:
        _fail(exc)


@app.command("ocr")
def ocr(project_id: str):
    """Bóc cue hardsub bằng VideoSubFinder/RapidOCR."""
    try:
        cues = orchestrator.run_ocr(project_id, progress_callback=_progress)
        console.print(f"[green]Đã nhận dạng {len(cues)} cue.[/green]")
    except Exception as exc:
        _fail(exc)


@app.command("approve-source")
def approve_source(project_id: str):
    """Xác nhận các cue tiếng Trung sau khi đã kiểm tra thủ công."""
    try:
        console.print_json(orchestrator.approve_source_cues(project_id).model_dump_json())
    except Exception as exc:
        _fail(exc)


@app.command("translate")
def translate(project_id: str):
    """Dịch các cue đã duyệt sang tiếng Việt."""
    try:
        cues = orchestrator.run_translation(project_id, progress_callback=_progress)
        console.print(f"[green]Đã dịch {len(cues)} cue.[/green]")
    except Exception as exc:
        _fail(exc)


@app.command("approve-translation")
def approve_translation(project_id: str):
    """Xác nhận bản dịch trước khi tạo giọng đọc."""
    try:
        console.print_json(orchestrator.approve_translation(project_id).model_dump_json())
    except Exception as exc:
        _fail(exc)


@app.command("voiceover")
def voiceover(
    project_id: str,
    voice: Optional[str] = typer.Option(None, "--voice", "-v"),
    rate: str = typer.Option("+0%", "--rate"),
    pitch: str = typer.Option("+0Hz", "--pitch"),
):
    """Tạo track tiếng Việt và căn từng câu theo timeline."""
    try:
        result = orchestrator.run_voiceover(
            project_id, voice=voice, rate=rate, pitch=pitch, progress_callback=_progress
        )
        console.print_json(json.dumps(result, ensure_ascii=False, default=str))
    except Exception as exc:
        _fail(exc)


@app.command("render")
def render(
    project_id: str,
    style: str = typer.Option("cover", help="cover | clean | bilingual"),
    cpu: bool = typer.Option(False, "--cpu", help="Không dùng NVENC"),
):
    """Render video cuối trong một pass."""
    try:
        result = orchestrator.run_render(
            project_id, RenderConfig(style=style, use_nvenc=not cpu), progress_callback=_progress
        )
        console.print(f"[green]{result.output_path}[/green]")
    except Exception as exc:
        _fail(exc)


@app.command("verify")
def verify(project_id: str):
    """Chạy bộ nghiệm thu chất lượng đầu ra."""
    try:
        report = orchestrator.verify(project_id)
        console.print_json(json.dumps(report.to_dict(), ensure_ascii=False))
        if not report.passed:
            raise typer.Exit(code=2)
    except typer.Exit:
        raise
    except Exception as exc:
        _fail(exc)


@app.command("run-all")
def run_all(
    project_id: str,
    voice: Optional[str] = typer.Option(None, "--voice", "-v"),
    style: Optional[str] = typer.Option(None, help="cover | clean | bilingual; mặc định giữ cấu hình đã lưu"),
    extraction_mode: str = typer.Option("hardsub", help="hardsub | auto | stt"),
):
    """Chạy OCR → dịch → TTS → render → QA; yêu cầu ROI đã được chọn."""
    try:
        result = orchestrator.run_all(project_id, voice=voice, style=style, extraction_mode=extraction_mode, progress_callback=_progress)
        console.print(Panel(str(result["output_path"]), title="Hoàn tất Fast Hardsub", style="green"))
    except Exception as exc:
        _fail(exc)


@app.command("status")
def status(project_id: str):
    """Xem state machine và artifact của dự án."""
    try:
        console.print_json(orchestrator.get_state(project_id).model_dump_json())
    except Exception as exc:
        _fail(exc)


@app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8088, "--port", "-p"),
    reload: bool = typer.Option(False, "--reload"),
):
    """Khởi động REST API và giao diện Studio."""
    import ipaddress
    try:
        local_bind=ipaddress.ip_address(host).is_loopback
    except ValueError:
        local_bind=host.lower()=='localhost'
    if not local_bind:
        if not settings.SERVER_MODE:
            _fail(RuntimeError('Hãy bật và thiết lập SERVER_MODE trước khi phục vụ máy khác.'))
        from src.server import store
        if not any(u['enabled'] and u['role']=='admin' for _,u in store.rows('user:')):
            _fail(RuntimeError('Tạo quản trị viên trên máy chủ qua localhost trước khi mở LAN.'))
    if settings.SERVER_MODE and reload:
        _fail(RuntimeError('Chế độ máy chủ không dùng autoreload.'))
    import uvicorn
    uvicorn.run("src.api.app:app", host=host, port=port, reload=reload,
                proxy_headers=bool(settings.TRUSTED_PROXY_IPS),
                forwarded_allow_ips=",".join(settings.TRUSTED_PROXY_IPS))


if __name__ == "__main__":
    app()
