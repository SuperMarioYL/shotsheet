"""Command line entry point."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from shotsheet import __version__
from shotsheet.pipeline.analyze import DEFAULT_ASR_MODEL, AnalyzeError, analyze_video
from shotsheet.schema import Platform

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
    """Detect shot boundaries in VIDEO and write shots.json."""
    try:
        sheet = analyze_video(
            video,
            platform=platform,
            transcribe=transcribe,
            asr_model=asr_model,
            language=language,
        )
    except AnalyzeError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(sheet.model_dump_json(indent=2) + "\n", encoding="utf-8")
    typer.echo(f"{len(sheet.shots)} shots -> {output}")
