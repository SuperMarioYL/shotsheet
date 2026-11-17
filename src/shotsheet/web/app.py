"""Local FastAPI surface: serve the single-page UI, analyse one upload, save edits.

The server is intentionally small and local-only: it binds to 127.0.0.1 when
started through ``shotsheet serve``, accepts either a multipart upload or a
local path, and returns the sheet JSON plus a server-rendered shot table.

Two write paths matter for m2:

- ``POST /analyze`` runs the pipeline and stores the resulting sheet on disk.
- ``PUT /sheet`` validates a complete sheet with the pydantic contract, re-runs
  the platform rules, and writes it back to ``shots.json``. The web page calls
  it whenever an inline cell changes, so edits are saved immediately.

An API key pasted into the page travels only in the request body; the server
uses it for that call and never writes or echoes it.
"""

from __future__ import annotations

import json
import os
import queue
import tempfile
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Annotated

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import ValidationError

from shotsheet.pipeline.analyze import AnalyzeError, analyze_video
from shotsheet.pipeline.describe import (
    DEFAULT_PROVIDER,
    PROVIDERS,
    describe_sheet,
    get_provider,
    require_api_key,
    vision_coverage,
)
from shotsheet.pipeline.rules import RuleReport, RulesError, apply_platform_rules
from shotsheet.render.shotlist_html import render_shot_table
from shotsheet.schema import CaptionBox, Platform, ShotSheet

WEB_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title="ShotSheet",
    description="本地短视频拉片：镜头切分、视觉描述与平台规则校验。",
    docs_url=None,
    redoc_url=None,
)


class SheetStore:
    """The one sheet the local page edits, persisted as ``shots.json``.

    The file lives in a process-lifetime temporary directory: local-only, no
    database, and gone when the server exits.
    """

    def __init__(self) -> None:
        self._dir = tempfile.TemporaryDirectory(prefix="shotsheet-sheets-")
        self.path = Path(self._dir.name) / "shots.json"
        self._lock = threading.Lock()

    def save(self, sheet: ShotSheet) -> None:
        payload = sheet.model_dump_json(indent=2) + "\n"
        with self._lock:
            temporary = self.path.with_name(".shots.json.tmp")
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, self.path)


SHEET_STORE = SheetStore()


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html", media_type="text/html")


@app.get("/config")
def config() -> dict[str, object]:
    """Expose provider availability so the page can disable vision without a key."""
    return {
        "default_provider": DEFAULT_PROVIDER,
        "providers": [
            {
                "id": provider.name,
                "label": provider.label,
                "model": provider.model,
                "api_key_env": provider.api_key_env,
                "available": bool(os.environ.get(provider.api_key_env)),
            }
            for provider in PROVIDERS.values()
        ],
    }


@app.post("/analyze")
async def analyze_clip(
    file: Annotated[UploadFile | None, File(description="本地上传的视频文件")] = None,
    path: Annotated[str | None, Form(description="或本机视频路径")] = None,
    platform: Annotated[Platform, Form(description="目标平台")] = Platform.DOUYIN,
    vision: Annotated[bool | None, Form(description="是否调用视觉模型")] = None,
    transcribe: Annotated[bool, Form(description="是否本地转写字幕")] = False,
    provider: Annotated[str, Form(description="视觉 provider")] = DEFAULT_PROVIDER,
    api_key: Annotated[str | None, Form(description="页面粘贴的 Key，仅本次请求使用")] = None,
    caption_box: Annotated[
        str | None,
        Form(description="计划字幕区 top,bottom,left,right 百分比；留空用默认值"),
    ] = None,
    stream: Annotated[bool, Form(description="返回逐镜进度事件流")] = False,
) -> Response:
    """Analyse one clip and return the sheet, findings, warnings, and table HTML."""
    if file is None and not path:
        raise HTTPException(status_code=400, detail="请上传视频文件或提供本机路径。")

    box = _caption_box(caption_box)
    try:
        provider_config = get_provider(provider)
        pasted_key = (api_key or "").strip() or None
        key = pasted_key or os.environ.get(provider_config.api_key_env)
        vision_enabled = bool(key) if vision is None else vision
        if vision_enabled and not key:
            # Raises the exact ARK_API_KEY / DASHSCOPE_API_KEY message.
            _, key = require_api_key(provider)
    except AnalyzeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    initial_warnings: list[str] = []
    if not vision_enabled and not key:
        initial_warnings.append(
            f"未设置 {provider_config.api_key_env}，本次没有视觉描述；"
            "景别、机位、运镜、道具、场景保持为空。"
        )

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if file is not None:
        temp_dir = tempfile.TemporaryDirectory(prefix="shotsheet-")
        name = Path(file.filename or "upload.mp4").name
        if name in {"", ".", ".."}:
            name = "upload.mp4"
        target = Path(temp_dir.name) / name
        with target.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                handle.write(chunk)
    else:
        target = Path(path or "").expanduser()

    if stream:
        return StreamingResponse(
            _stream_analysis(
                target,
                platform=platform,
                vision=vision_enabled,
                transcribe=transcribe,
                provider=provider,
                api_key=key,
                caption_box=box,
                warnings=initial_warnings,
                cleanup=temp_dir,
            ),
            media_type="application/x-ndjson",
        )

    try:
        payload = _analyse(
            target,
            platform=platform,
            vision=vision_enabled,
            transcribe=transcribe,
            provider=provider,
            api_key=key,
            caption_box=box,
            warnings=initial_warnings,
        )
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()
    return JSONResponse(payload)


@app.put("/sheet")
def put_sheet(body: Annotated[dict, Body(description="完整 ShotSheet JSON")]) -> JSONResponse:
    """Validate and persist an edited sheet, then return refreshed verdicts."""
    try:
        sheet = ShotSheet.model_validate(body)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise HTTPException(status_code=422, detail=details) from exc

    try:
        report = apply_platform_rules(sheet)
    except RulesError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    SHEET_STORE.save(report.sheet)
    return JSONResponse(_payload(report, warnings=[]))


def _analyse(
    video: Path,
    *,
    platform: Platform,
    vision: bool,
    transcribe: bool,
    provider: str,
    api_key: str | None,
    caption_box: CaptionBox | None,
    warnings: list[str],
    on_stage: Callable[[str], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, object]:
    try:
        if on_stage is not None:
            on_stage("切分镜头与转写…")
        sheet = analyze_video(video, platform=platform, transcribe=transcribe)
        if vision:
            if on_stage is not None:
                on_stage(f"视觉描述（{len(sheet.shots)} 镜）…")
            sheet = describe_sheet(
                sheet,
                video,
                provider=provider,
                api_key=api_key,
                progress=on_progress,
            )
            covered, total = vision_coverage(sheet)
            if covered < total:
                warnings.append(
                    f"视觉覆盖 {covered}/{total} 镜；未覆盖镜头的字段为空，详见服务端日志。"
                )
        if on_stage is not None:
            on_stage("按平台规则校验…")
        if caption_box is not None:
            sheet = sheet.with_caption_box(caption_box)
        report = apply_platform_rules(sheet)
    except (AnalyzeError, RulesError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # unexpected decoder/library failures still get a clean message
        raise HTTPException(status_code=500, detail=f"分析失败：{exc}") from exc

    SHEET_STORE.save(report.sheet)
    return _payload(report, warnings)


def _payload(report: RuleReport, warnings: list[str]) -> dict[str, object]:
    return {
        "sheet": report.sheet.model_dump(mode="json"),
        "violations": [violation.as_dict() for violation in report.violations],
        "warnings": warnings,
        "table_html": render_shot_table(report.sheet, report.violations, editable=True),
        "sheet_path": str(SHEET_STORE.path),
    }


def _caption_box(value: str | None) -> CaptionBox | None:
    """Parse an optional ``top,bottom,left,right`` form value into a caption box."""
    if value is None or not value.strip():
        return None
    try:
        return CaptionBox.from_csv(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _stream_analysis(
    video: Path,
    *,
    platform: Platform,
    vision: bool,
    transcribe: bool,
    provider: str,
    api_key: str | None,
    caption_box: CaptionBox | None,
    warnings: list[str],
    cleanup: tempfile.TemporaryDirectory[str] | None,
) -> Iterator[str]:
    """Run the pipeline in a worker thread and yield NDJSON progress events."""
    events: queue.Queue[dict[str, object] | None] = queue.Queue()

    def emit(event: dict[str, object]) -> None:
        events.put(event)

    def stage(text: str) -> None:
        emit({"type": "stage", "text": text})

    def progress(done: int, total: int) -> None:
        emit({"type": "progress", "done": done, "total": total})

    def work() -> None:
        try:
            payload = _analyse(
                video,
                platform=platform,
                vision=vision,
                transcribe=transcribe,
                provider=provider,
                api_key=api_key,
                caption_box=caption_box,
                warnings=list(warnings),
                on_stage=stage,
                on_progress=progress,
            )
            emit({"type": "done", **payload})
        except HTTPException as exc:
            emit({"type": "error", "detail": str(exc.detail)})
        finally:
            if cleanup is not None:
                cleanup.cleanup()
            events.put(None)

    threading.Thread(target=work, daemon=True).start()
    while True:
        event = events.get()
        if event is None:
            break
        yield json.dumps(event, ensure_ascii=False) + "\n"
