"""Tests for shot analysis, the sheet contract, the HTML sheet, and the CLI wiring.

The video-dependent tests build tiny clips with ffmpeg and are skipped when
ffmpeg is not installed; the schema, caption, transcribe, and renderer tests
always run.
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
from shotsheet.pipeline.analyze import (
    AnalyzeError,
    CaptionSegment,
    analyze_video,
    attach_captions,
    transcribe_video,
)
from shotsheet.pipeline.rules import Violation
from shotsheet.render.shotlist_html import render_shotlist_html
from shotsheet.schema import (
    Camera,
    Caption,
    Movement,
    Platform,
    Shot,
    ShotSheet,
    ShotSize,
    Source,
    format_timecode,
    parse_timecode,
)


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


def test_timecode_frame_count_matches_real_duration() -> None:
    fps = 30000 / 1001

    assert format_timecode(180, fps) == "00:00:06:00"
    # Frame 180 happens 6.006 s into a 29.97 fps clip, so the timecode must
    # parse back to 6006 ms rather than 6000 ms.
    assert parse_timecode("00:00:06:00", fps) * 1000 == pytest.approx(6006, abs=1)
    assert format_timecode(1800, fps) == "00:01:00:00"
    for frame_num in range(0, 1800, 7):
        timecode = format_timecode(frame_num, fps)
        assert parse_timecode(timecode, fps) == pytest.approx(frame_num / fps)


def test_render_shotlist_html_has_timeline_caption_and_rule_columns() -> None:
    html = render_shotlist_html(_sample_sheet())

    assert html.count("</th>") == 3
    assert "时间码 / 镜头" in html
    assert "字幕" in html
    assert "规则" in html
    assert "00:00:00:00 → 00:00:02:00" in html
    assert "开场就在厨房" in html
    assert html.count("<tr>") == 3


def test_render_shotlist_html_shows_vision_fields_and_violations() -> None:
    sheet = _sample_sheet()
    sheet.shots[0].shot_size = ShotSize.CLOSE_UP
    sheet.shots[0].camera = Camera.EYE_LEVEL
    sheet.shots[0].movement = Movement.NONE
    sheet.shots[0].props = ["马克杯"]
    sheet.shots[0].scene = "厨房台面"
    violations = [
        Violation(
            rule_id="douyin.hook_window",
            platform=Platform.DOUYIN,
            shot_index=0,
            severity="warning",
            message="开场镜头过长",
        ),
        Violation(
            rule_id="douyin.duration",
            platform=Platform.DOUYIN,
            shot_index=None,
            severity="error",
            message="整条视频超过上限",
        ),
    ]

    html = render_shotlist_html(sheet, violations)

    assert "特写" in html
    assert "平视" in html
    assert "道具：马克杯" in html
    assert "场景：厨房台面" in html
    assert "[douyin.hook_window] 开场镜头过长" in html
    assert html.index("整条视频超过上限") < html.index("<table>")


class _FakeSegment:
    start = 0.0
    end = 6.0
    text = "三镜头"


def test_transcribe_wiring_attaches_captions(
    synthetic_clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import faster_whisper

    seen: dict[str, object] = {}

    class FakeModel:
        def __init__(self, model_size: str, device: str, compute_type: str) -> None:
            seen["model_size"] = model_size
            seen["device"] = device
            seen["compute_type"] = compute_type

        def transcribe(self, audio: str, language: str | None = None) -> tuple:
            seen["audio"] = audio
            seen["language"] = language
            return iter([_FakeSegment()]), object()

    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModel)

    sheet = analyze_video(synthetic_clip, transcribe=True, asr_model="tiny", language="zh")

    assert seen["model_size"] == "tiny"
    assert seen["language"] == "zh"
    assert str(seen["audio"]).endswith(synthetic_clip.name)
    assert all(shot.caption is not None for shot in sheet.shots)
    assert all(shot.caption.text == "三镜头" for shot in sheet.shots)


def test_transcribe_errors_become_analyze_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import faster_whisper

    class BrokenModel:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise TypeError("open() got an unexpected keyword argument 'metadata_errors'")

    monkeypatch.setattr(faster_whisper, "WhisperModel", BrokenModel)

    with pytest.raises(AnalyzeError, match="transcription failed.*metadata_errors"):
        transcribe_video(Path("clip.mp4"), model_size="tiny")


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


@pytest.fixture(scope="module")
def solid_clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A four-second single-colour clip: scene detection cannot find a cut."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is not installed")

    output = tmp_path_factory.mktemp("solid") / "solid.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x120:r=30:d=4",
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


@pytest.fixture(scope="module")
def clip_2997(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A two-second 29.97 fps clip, where nominal-30 timecodes drift from real time."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is not installed")

    output = tmp_path_factory.mktemp("clip2997") / "clip2997.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x120:r=30000/1001:d=2",
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


def test_single_shot_clip_warns_instead_of_failing(solid_clip: Path) -> None:
    with pytest.warns(UserWarning, match="found 1 shot"):
        sheet = analyze_video(solid_clip)

    assert len(sheet.shots) == 1


def test_2997_fps_tc_out_matches_source_duration(clip_2997: Path) -> None:
    sheet = analyze_video(clip_2997)
    fps = sheet.source.fps
    last = sheet.shots[-1]

    assert fps == pytest.approx(30000 / 1001)
    assert last.tc_out == "00:00:02:00"
    assert parse_timecode(last.tc_out, fps) * 1000 == pytest.approx(sheet.source.duration_ms, abs=1)


def test_cli_version() -> None:
    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.output.strip() == f"shotsheet {__version__}"


def test_cli_analyze_writes_json_and_html(synthetic_clip: Path) -> None:
    output = synthetic_clip.parent / "shots.json"
    html_path = output.with_suffix(".html")

    result = CliRunner().invoke(
        app, ["analyze", str(synthetic_clip), "-o", str(output), "--platform", "douyin"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["source"]["platform"] == "douyin"
    assert len(payload["shots"]) >= 2

    html = html_path.read_text(encoding="utf-8")
    assert html.count("</th>") == 3
    assert payload["shots"][0]["tc_in"] in html


def test_cli_rejects_non_video_input(tmp_path: Path) -> None:
    not_a_video = tmp_path / "notes.txt"
    not_a_video.write_text("not a video", encoding="utf-8")
    output = tmp_path / "shots.json"

    result = CliRunner().invoke(app, ["analyze", str(not_a_video), "-o", str(output)])

    assert result.exit_code == 1
    message = result.output + result.stderr
    assert "error: could not open" in message
    assert "Traceback" not in message
    assert not output.exists()
