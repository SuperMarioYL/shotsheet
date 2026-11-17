"""Tests for shot analysis, the sheet contract, and the CLI wiring.

The video-dependent tests build a tiny three-cut clip with ffmpeg and are skipped
when ffmpeg is not installed; the schema and caption tests always run.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from itertools import pairwise
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shotsheet import __version__
from shotsheet.cli import app
from shotsheet.pipeline.analyze import CaptionSegment, analyze_video, attach_captions
from shotsheet.schema import Caption, Platform, Shot, ShotSheet, Source, parse_timecode


def _sample_sheet() -> ShotSheet:
    return ShotSheet(
        source=Source(file="demo.mp4", duration_ms=6000, fps=30.0, platform=Platform.DOUYIN),
        shots=[
            Shot(
                index=0,
                tc_in="00:00:00:00",
                tc_out="00:00:02:00",
                duration_ms=2000,
                caption=Caption(text="开场就在厨房", in_ms=200, out_ms=1800, safe_area_ok=True),
            ),
            Shot(index=1, tc_in="00:00:02:00", tc_out="00:00:06:00", duration_ms=4000),
        ],
    )


def test_sheet_json_round_trip() -> None:
    sheet = _sample_sheet()

    assert sheet.shots[0].subtitle_safe is True
    assert sheet.shots[1].subtitle_safe is None

    dumped = sheet.model_dump_json()
    payload = json.loads(dumped)
    assert payload["shots"][0]["subtitle_safe"] is True
    assert ShotSheet.model_validate_json(dumped) == sheet


def test_attach_captions_clamps_to_each_shot() -> None:
    shots = [
        Shot(index=0, tc_in="00:00:00:00", tc_out="00:00:02:00", duration_ms=2000),
        Shot(index=1, tc_in="00:00:02:00", tc_out="00:00:04:00", duration_ms=2000),
    ]
    segments = [
        CaptionSegment(start_s=0.2, end_s=1.7, text="第一句"),
        CaptionSegment(start_s=2.0, end_s=3.4, text="第二句"),
        CaptionSegment(start_s=3.8, end_s=4.6, text="片尾"),
    ]

    captioned = attach_captions(shots, segments, fps=30.0)

    assert captioned[0].caption == Caption(text="第一句", in_ms=200, out_ms=1700)
    assert captioned[1].caption == Caption(text="第二句片尾", in_ms=2000, out_ms=4000)


@pytest.fixture(scope="module")
def synthetic_clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A six-second clip made of three two-second solid-colour shots."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is not installed")

    output = tmp_path_factory.mktemp("clip") / "three-cuts.mp4"
    sources: list[str] = []
    for colour in ("red", "blue", "green"):
        sources += ["-f", "lavfi", "-i", f"color=c={colour}:s=160x120:r=30:d=2"]
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            *sources,
            "-filter_complex",
            "[0:v][1:v][2:v]concat=n=3:v=1:a=0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(output),
        ],
        check=True,
        capture_output=True,
    )
    return output


def test_analyze_finds_contiguous_monotonic_shots(synthetic_clip: Path) -> None:
    sheet = analyze_video(synthetic_clip, platform=Platform.DOUYIN)
    fps = sheet.source.fps
    shots = sheet.shots

    assert sheet.source.file == synthetic_clip.name
    assert sheet.source.platform is Platform.DOUYIN
    assert 2 <= len(shots) <= 4

    for index, shot in enumerate(shots):
        assert shot.index == index
        assert shot.duration_ms > 0
        assert shot.caption is None

    assert parse_timecode(shots[0].tc_in, fps) == 0.0
    for previous, current in pairwise(shots):
        assert previous.tc_out == current.tc_in
        assert parse_timecode(previous.tc_in, fps) < parse_timecode(current.tc_in, fps)

    assert sheet.source.duration_ms == pytest.approx(6000, abs=5)
    assert abs(sum(shot.duration_ms for shot in shots) - sheet.source.duration_ms) <= 10

    restored = ShotSheet.model_validate_json(sheet.model_dump_json())
    assert restored == sheet


def test_cli_version() -> None:
    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.output.strip() == f"shotsheet {__version__}"


def test_cli_analyze_writes_json(synthetic_clip: Path) -> None:
    output = synthetic_clip.parent / "shots.json"

    result = CliRunner().invoke(
        app, ["analyze", str(synthetic_clip), "-o", str(output), "--platform", "douyin"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["source"]["platform"] == "douyin"
    assert len(payload["shots"]) >= 2
