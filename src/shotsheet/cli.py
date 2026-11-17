"""Command line entry point."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import typer

from shotsheet import __version__
from shotsheet.pipeline.analyze import DEFAULT_ASR_MODEL, AnalyzeError, analyze_video
from shotsheet.pipeline.describe import DEFAULT_PROVIDER, describe_sheet
from shotsheet.pipeline.rules import RulesError, Violation, apply_platform_rules
from shotsheet.render.shotlist_html import render_shotlist_html
from shotsheet.schema import Platform, ShotSheet

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
) -> None:
    """Detect shot boundaries in VIDEO and write shots.json plus a printable sheet."""
    try:
        sheet = analyze_video(
            video,
            platform=platform,
            transcribe=transcribe,
            asr_model=asr_model,
            language=language,
        )
        report = apply_platform_rules(sheet)
    except (AnalyzeError, RulesError) as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    html_path = _write_sheet(report.sheet, report.violations, output, html)
    typer.echo(f"{len(report.sheet.shots)} shots -> {output} (sheet: {html_path})")
    _echo_rule_summary(report.violations)


@app.command()
def describe(
    video: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Local video file used to extract per-shot frames.",
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
            help="Existing shots.json; when omitted, VIDEO is analysed (without ASR) first.",
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
        typer.Option("--platform", help="Platform used when VIDEO is analysed from scratch."),
    ] = Platform.DOUYIN,
    provider: Annotated[
        str,
        typer.Option("--provider", help=f"Vision provider: {DEFAULT_PROVIDER} or qwen."),
    ] = DEFAULT_PROVIDER,
    no_vision: Annotated[
        bool,
        typer.Option("--no-vision", help="Keep vision fields empty; no API key is needed."),
    ] = False,
) -> None:
    """Fill 景别/机位/运镜/道具/场景 for each shot via a vision model."""
    try:
        if sheet_path is not None:
            try:
                sheet = ShotSheet.model_validate_json(sheet_path.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise AnalyzeError(f"invalid sheet JSON {sheet_path}: {exc}") from exc
        else:
            sheet = analyze_video(video, platform=platform)

        if not no_vision:
            sheet = describe_sheet(sheet, video, provider=provider)
        report = apply_platform_rules(sheet)
    except (AnalyzeError, RulesError) as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    html_path = _write_sheet(report.sheet, report.violations, output, html)
    skipped = " (vision skipped)" if no_vision else ""
    typer.echo(f"{len(report.sheet.shots)} shots -> {output} (sheet: {html_path}){skipped}")
    _echo_rule_summary(report.violations)


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
) -> None:
    """Validate a sheet against the platform rules and print every finding."""
    try:
        sheet = ShotSheet.model_validate_json(sheet_file.read_text(encoding="utf-8"))
        if platform is not None:
            sheet = sheet.model_copy(
                update={"source": sheet.source.model_copy(update={"platform": platform})}
            )
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
