"""Local FastAPI surface: serve the single-page UI and analyse one upload.

The server is intentionally small and local-only: it binds to 127.0.0.1 when
started through ``shotsheet serve``, accepts either a multipart upload or a
local path, and returns the sheet JSON plus a server-rendered shot table.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from shotsheet.pipeline.analyze import AnalyzeError, analyze_video
from shotsheet.pipeline.describe import (
    DEFAULT_PROVIDER,
    PROVIDERS,
    describe_sheet,
    has_api_key,
    require_api_key,
)
from shotsheet.pipeline.rules import RulesError, apply_platform_rules
from shotsheet.render.shotlist_html import render_shot_table
from shotsheet.schema import Platform

WEB_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title="ShotSheet",
    description="本地短视频拉片：镜头切分、视觉描述与平台规则校验。",
    docs_url=None,
    redoc_url=None,
)


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
                "available": has_api_key(provider.name),
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
) -> JSONResponse:
    """Analyse one clip and return the sheet, findings, warnings, and table HTML."""
    if file is None and not path:
        raise HTTPException(status_code=400, detail="请上传视频文件或提供本机路径。")

    try:
        # Omitted ``vision`` means "use it when a key is configured".
        vision_enabled = has_api_key(provider) if vision is None else vision
        api_key = require_api_key(provider)[1] if vision_enabled else None
    except AnalyzeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if file is not None:
        with tempfile.TemporaryDirectory(prefix="shotsheet-") as tmp:
            name = Path(file.filename or "upload.mp4").name
            if name in {"", ".", ".."}:
                name = "upload.mp4"
            target = Path(tmp) / name
            with target.open("wb") as handle:
                while chunk := await file.read(1024 * 1024):
                    handle.write(chunk)
            return _run(target, platform, vision_enabled, transcribe, provider, api_key)

    local = Path(path or "").expanduser()
    return _run(local, platform, vision_enabled, transcribe, provider, api_key)


def _run(
    video: Path,
    platform: Platform,
    vision: bool,
    transcribe: bool,
    provider: str,
    api_key: str | None,
) -> JSONResponse:
    warnings: list[str] = []
    try:
        sheet = analyze_video(video, platform=platform, transcribe=transcribe)
        if vision:
            sheet = describe_sheet(sheet, video, provider=provider, api_key=api_key)
        elif not has_api_key(provider):
            warnings.append(
                f"未设置 {PROVIDERS[provider].api_key_env}，本次没有视觉描述；"
                "景别、机位、运镜、道具、场景保持为空。"
            )
        report = apply_platform_rules(sheet)
    except (AnalyzeError, RulesError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # unexpected decoder/library failures still get a clean message
        raise HTTPException(status_code=500, detail=f"分析失败：{exc}") from exc

    payload = {
        "sheet": report.sheet.model_dump(mode="json"),
        "violations": [violation.as_dict() for violation in report.violations],
        "warnings": warnings,
        "table_html": render_shot_table(report.sheet, report.violations),
    }
    return JSONResponse(payload)
