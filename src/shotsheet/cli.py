"""Command line entry point."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import typer

from shotsheet import __version__
from shotsheet.pipeline.analyze import DEFAULT_ASR_MODEL, AnalyzeError, analyze_video
from shotsheet.pipeline.describe import (
    DEFAULT_PROVIDER,
    describe_sheet,
    require_api_key,
    vision_coverage,
)
from shotsheet.pipeline.rules import RulesError, Violation, apply_platform_rules
from shotsheet.render.shotlist_html import render_shotlist_html
from shotsheet.schema import CaptionBox, Platform, ShotSheet

app = typer.Typer(add_completion=False, help="Shot-level teardown for short-form video.")


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"shotsheet {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_show_version, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """Analyse a local clip into a per-shot sheet."""


@app.command()
def analyze(
    video: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Local video file (v0.1 does not download platform URLs).",
        ),
    ],
    output: Annotated[
        Path, typer.Option("--output", "-o", help="Where to write the shot sheet JSON.")
    ] = Path("shots.json"),
    html: Annotated[
        Path | None,
        typer.Option(
            "--html",
            help="Where to write the printable HTML sheet; defaults to the JSON path with .html.",
        ),
    ] = None,
    platform: Annotated[
        Platform, typer.Option("--platform", help="Platform whose rules the sheet targets.")
    ] = Platform.DOUYIN,
    transcribe: Annotated[
        bool,
        typer.Option("--transcribe", help="Run local ASR and attach captions to each shot."),
    ] = False,
    asr_model: Annotated[
        str,
        typer.Option("--asr-model", help="faster-whisper model size (downloaded on first use)."),
    ] = DEFAULT_ASR_MODEL,
    language: Annotated[
        str | None,
        typer.Option("--language", help="Spoken language, e.g. zh; auto-detect when omitted."),
    ] = None,
    caption_box: Annotated[
        str | None,
        typer.Option(
            "--caption-box",
            help="Planned caption box as top,bottom,left,right percentages (default 88,100,4,96).",
        ),
    ] = None,
) -> None:
    """Detect shot boundaries in VIDEO and write shots.json plus a printable sheet."""
    try:
        box = _caption_box(caption_box)
        sheet = analyze_video(
            video,
            platform=platform,
            transcribe=transcribe,
            asr_model=asr_model,
            language=language,
        )
        if box is not None:
            sheet = _with_caption_box(sheet, box)
        report = apply_platform_rules(sheet)
    except (AnalyzeError, RulesError) as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    html_path = _write_sheet(report.sheet, report.violations, output, html)
    typer.echo(f"{len(report.sheet.shots)} shots -> {output} (sheet: {html_path})")
    _echo_rule_summary(report.violations)


@app.command()
def describe(
    source: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Local video file, or a shots.json from analyze (detected by .json suffix).",
        ),
    ],
    sheet_path: Annotated[
        Path | None,
        typer.Option(
            "--sheet",
            exists=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Existing shots.json; SOURCE is then the video used to extract frames.",
        ),
    ] = None,
    video: Annotated[
        Path | None,
        typer.Option(
            "--video",
            exists=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Source video when SOURCE is a sheet and it is not beside the JSON.",
        ),
    ] = None,
    output: Annotated[
        Path, typer.Option("--output", "-o", help="Where to write the described sheet JSON.")
    ] = Path("shots.json"),
    html: Annotated[
        Path | None,
        typer.Option(
            "--html",
            help="Where to write the printable HTML sheet; defaults to the JSON path with .html.",
        ),
    ] = None,
    platform: Annotated[
        Platform,
        typer.Option("--platform", help="Platform used when the video is analysed from scratch."),
    ] = Platform.DOUYIN,
    provider: Annotated[
        str,
        typer.Option("--provider", help=f"Vision provider: {DEFAULT_PROVIDER} or qwen."),
    ] = DEFAULT_PROVIDER,
    no_vision: Annotated[
        bool,
        typer.Option("--no-vision", help="Keep vision fields empty; no API key is needed."),
    ] = False,
    caption_box: Annotated[
        str | None,
        typer.Option(
            "--caption-box",
            help="Planned caption box as top,bottom,left,right percentages (default 88,100,4,96).",
        ),
    ] = None,
) -> None:
    """Fill 景别/机位/运镜/道具/场景 for each shot via a vision model.

    SOURCE may be a video (analysed first) or a shots.json produced by analyze.
    With a sheet, the video is looked up beside the JSON and can be overridden
    with --video.
    """
    try:
        box = _caption_box(caption_box)
        sheet_input, media = _describe_inputs(source, sheet_path, video)
        sheet = _read_sheet(sheet_input) if sheet_input is not None else None

        if not no_vision:
            # Validate the key before touching the video, so `describe out.json`
            # without one names ARK_API_KEY/DASHSCOPE_API_KEY instead of
            # complaining that the sheet is not a video.
            _provider, api_key = require_api_key(provider)
            if media is None and sheet is not None:
                media = _sibling_video(sheet_input, sheet)
            if media is None:
                missing = sheet.source.file if sheet is not None else source.name
                raise AnalyzeError(
                    f"找不到源视频 {missing}；请用 --video 指定它的路径，"
                    "或加 --no-vision 只校验规则、不做视觉描述。"
                )
            if sheet is None:
                sheet = analyze_video(media, platform=platform)
            sheet = describe_sheet(sheet, media, provider=provider, api_key=api_key)
            covered, total = vision_coverage(sheet)
            typer.echo(f"vision coverage: {covered}/{total} shots")
            if covered < total:
                typer.secho(
                    f"warning: 视觉覆盖 {covered}/{total} 镜；未覆盖镜头的字段为空，"
                    "详见上面的模型返回值警告。",
                    fg=typer.colors.YELLOW,
                    err=True,
                )
        elif sheet is None:
            sheet = analyze_video(media or source, platform=platform)

        if box is not None:
            sheet = _with_caption_box(sheet, box)
        report = apply_platform_rules(sheet)
    except (AnalyzeError, RulesError) as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    html_path = _write_sheet(report.sheet, report.violations, output, html)
    skipped = " (vision skipped)" if no_vision else ""
    typer.echo(f"{len(report.sheet.shots)} shots -> {output} (sheet: {html_path}){skipped}")
    _echo_rule_summary(report.violations)


def _describe_inputs(
    source: Path, sheet_option: Path | None, video_option: Path | None
) -> tuple[Path | None, Path | None]:
    """Return ``(sheet_json, video)`` from the flexible describe arguments."""
    if sheet_option is not None:
        if source.suffix.lower() == ".json":
            raise AnalyzeError(
                "同时给出 --sheet 时，位置参数应是源视频；"
                "或去掉 --sheet，直接把 JSON 放在位置参数。"
            )
        return sheet_option, video_option or source
    if source.suffix.lower() == ".json":
        return source, video_option
    return None, source


def _read_sheet(path: Path) -> ShotSheet:
    try:
        return ShotSheet.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AnalyzeError(f"not a shot sheet JSON {path}: {exc}") from exc


def _sibling_video(sheet_path: Path, sheet: ShotSheet) -> Path | None:
    candidate = sheet_path.parent / sheet.source.file
    return candidate if candidate.is_file() else None


@app.command()
def rules(
    sheet_file: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="shots.json to validate.",
        ),
    ],
    platform: Annotated[
        Platform | None,
        typer.Option("--platform", help="Override the platform recorded in the sheet."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print findings as a JSON array.")
    ] = False,
    caption_box: Annotated[
        str | None,
        typer.Option(
            "--caption-box",
            help="Planned caption box as top,bottom,left,right percentages (default 88,100,4,96).",
        ),
    ] = None,
) -> None:
    """Validate a sheet against the platform rules and print every finding."""
    try:
        box = _caption_box(caption_box)
        sheet = ShotSheet.model_validate_json(sheet_file.read_text(encoding="utf-8"))
        if platform is not None:
            sheet = sheet.model_copy(
                update={"source": sheet.source.model_copy(update={"platform": platform})}
            )
        if box is not None:
            sheet = _with_caption_box(sheet, box)
        report = apply_platform_rules(sheet)
    except (ValueError, RulesError) as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    if as_json:
        typer.echo(
            json.dumps(
                [violation.as_dict() for violation in report.violations],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        _echo_rules(report.profile.label, report.violations)

    if report.errors:
        raise typer.Exit(code=1)


@app.command()
def serve(
    port: Annotated[int, typer.Option("--port", help="Local port for the single-page UI.")] = 8787,
) -> None:
    """Serve the local single-page UI on 127.0.0.1."""
    try:
        import uvicorn

        from shotsheet.web.app import app as web_app
    except ImportError as exc:
        typer.secho(
            f"error: the local UI needs FastAPI and uvicorn ({exc})",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from exc

    typer.echo(f"ShotSheet UI: http://127.0.0.1:{port}")
    uvicorn.run(web_app, host="127.0.0.1", port=port, log_level="info")


def _caption_box(value: str | None) -> CaptionBox | None:
    """Parse ``top,bottom,left,right`` percentages into a planned caption box."""
    if value is None:
        return None
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4:
        raise typer.BadParameter(
            "--caption-box needs four comma-separated percentages: top,bottom,left,right"
        )
    try:
        top, bottom, left, right = (float(part) for part in parts)
    except ValueError as exc:
        raise typer.BadParameter(f"--caption-box values must be numbers: {value!r}") from exc
    if not all(0 <= number <= 100 for number in (top, bottom, left, right)):
        raise typer.BadParameter("--caption-box percentages must stay within 0..100")
    if top >= bottom or left >= right:
        raise typer.BadParameter("--caption-box must satisfy top < bottom and left < right")
    return CaptionBox(top=top, bottom=bottom, left=left, right=right)


def _with_caption_box(sheet: ShotSheet, box: CaptionBox) -> ShotSheet:
    return sheet.model_copy(update={"source": sheet.source.model_copy(update={"caption_box": box})})


def _write_sheet(
    sheet: ShotSheet,
    violations: Sequence[Violation],
    output: Path,
    html: Path | None,
) -> Path:
    html_path = html or output.with_suffix(".html")
    output.parent.mkdir(parents=True, exist_ok=True)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(sheet.model_dump_json(indent=2) + "\n", encoding="utf-8")
    html_path.write_text(render_shotlist_html(sheet, violations), encoding="utf-8")
    return html_path


def _echo_rule_summary(violations: Sequence[Violation]) -> None:
    if not violations:
        return
    errors = sum(1 for violation in violations if violation.severity == "error")
    typer.echo(f"rules: {len(violations)} finding(s), {errors} error(s) — see the sheet")


def _echo_rules(label: str, violations: Sequence[Violation]) -> None:
    errors = sum(1 for violation in violations if violation.severity == "error")
    warnings = len(violations) - errors
    typer.echo(f"{label}: {len(violations)} finding(s), {errors} error(s), {warnings} warning(s)")
    for violation in violations:
        typer.echo(f"  [{violation.rule_id}] {violation.message}")
