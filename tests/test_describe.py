"""Tests for the vision-description wiring that do not touch the network."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shotsheet.cli import app
from shotsheet.pipeline.analyze import AnalyzeError
from shotsheet.pipeline.describe import (
    DEFAULT_PROVIDER,
    PROVIDERS,
    describe_sheet,
    get_provider,
    has_api_key,
    require_api_key,
    vision_coverage,
)
from shotsheet.schema import Camera, Movement, Platform, Shot, ShotSheet, ShotSize, Source


def _sheet() -> ShotSheet:
    return ShotSheet(
        source=Source(file="demo.mp4", duration_ms=5000, fps=30.0, platform=Platform.DOUYIN),
        shots=[
            Shot(index=0, tc_in="00:00:00:00", tc_out="00:00:02:00", duration_ms=2000),
            Shot(index=1, tc_in="00:00:02:00", tc_out="00:00:05:00", duration_ms=3000),
        ],
    )


def test_default_provider_is_the_planned_doubao_model() -> None:
    provider = get_provider()

    assert provider.name == DEFAULT_PROVIDER == "doubao"
    assert provider.model == "doubao-seed-2-1-pro-260915"
    assert provider.api_key_env == "ARK_API_KEY"
    assert provider.base_url.startswith("https://ark.cn-beijing.volces.com/api/v3")
    assert PROVIDERS["qwen"].model == "qwen3-vl-plus"


def test_missing_key_fails_before_touching_the_video(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    with pytest.raises(AnalyzeError) as excinfo:
        describe_sheet(_sheet(), Path("does-not-exist.mp4"))

    message = str(excinfo.value)
    assert "ARK_API_KEY" in message
    assert "DASHSCOPE_API_KEY" in message
    assert "--no-vision" in message


def test_missing_qwen_key_names_dashscope_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    with pytest.raises(AnalyzeError, match="DASHSCOPE_API_KEY"):
        describe_sheet(_sheet(), Path("does-not-exist.mp4"), provider="qwen")


def test_unknown_provider_lists_the_choices() -> None:
    with pytest.raises(AnalyzeError, match="doubao, qwen"):
        get_provider("openai")


def test_key_helpers_read_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARK_API_KEY", "test-key")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    provider, key = require_api_key()
    assert provider.name == "doubao"
    assert key == "test-key"
    assert has_api_key("doubao") is True
    assert has_api_key("qwen") is False


def test_cli_describe_no_vision_leaves_fields_empty(tmp_path: Path) -> None:
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"not a real video")
    sheet_path = tmp_path / "shots.json"
    sheet_path.write_text(_sheet().model_dump_json(), encoding="utf-8")
    output = tmp_path / "described.json"

    result = CliRunner().invoke(
        app,
        [
            "describe",
            str(video),
            "--sheet",
            str(sheet_path),
            "--no-vision",
            "-o",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["rules"] == "cn-platforms.yaml#douyin"
    for shot in payload["shots"]:
        assert shot["shot_size"] is None
        assert shot["camera"] is None
        assert shot["movement"] is None
        assert shot["props"] == []
        assert shot["scene"] is None
    assert output.with_suffix(".html").is_file()


def _install_fake_vision(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reply: str) -> Path:
    """Point describe_sheet at a fake HTTP client and a fake frame extractor."""
    import httpx

    from shotsheet.pipeline import describe as describe_module

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self) -> dict:
            return {"choices": [{"message": {"content": reply}}]}

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def post(self, *args: object, **kwargs: object) -> FakeResponse:
            return FakeResponse()

    video = tmp_path / "demo.mp4"
    video.write_bytes(b"fake video")
    monkeypatch.setattr(describe_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(describe_module, "_extract_frames", lambda *args, **kwargs: [b"frame"])
    monkeypatch.setattr(httpx, "Client", FakeClient)
    return video


def test_valid_vision_payload_merges_all_five_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reply = (
        '{"shot_size":"特写","camera":"平视","movement":"推",'
        '"props":["马克杯","手机"],"scene":"厨房台面"}'
    )
    video = _install_fake_vision(monkeypatch, tmp_path, reply)

    sheet = describe_sheet(_sheet(), video, api_key="test-key", concurrency=1)

    for shot in sheet.shots:
        assert shot.shot_size is ShotSize.CLOSE_UP
        assert shot.camera is Camera.EYE_LEVEL
        assert shot.movement is Movement.PUSH_IN
        assert shot.props == ["马克杯", "手机"]
        assert shot.scene == "厨房台面"
    assert vision_coverage(sheet) == (2, 2)


def test_invalid_enum_values_are_logged_and_left_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    reply = '{"shot_size":"半身","camera":"平视","movement":"推","props":[],"scene":"厨房台面"}'
    video = _install_fake_vision(monkeypatch, tmp_path, reply)

    with caplog.at_level(logging.WARNING, logger="shotsheet.pipeline.describe"):
        sheet = describe_sheet(_sheet(), video, api_key="test-key", concurrency=1)

    assert "半身" in caplog.text
    assert "shot_size" in caplog.text
    assert all(shot.shot_size is None for shot in sheet.shots)
    assert vision_coverage(sheet) == (0, 2)


def test_cli_describe_accepts_a_sheet_as_the_positional_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    sheet_path = tmp_path / "shots.json"
    sheet_path.write_text(_sheet().model_dump_json(), encoding="utf-8")

    result = CliRunner().invoke(
        app, ["describe", str(sheet_path), "-o", str(tmp_path / "out.json")]
    )

    assert result.exit_code == 1
    message = result.output + result.stderr
    assert "ARK_API_KEY" in message
    assert "DASHSCOPE_API_KEY" in message
    assert "--no-vision" in message
    assert "could not open" not in message
    assert "as a video" not in message


def test_cli_describe_sheet_positional_with_no_vision(tmp_path: Path) -> None:
    sheet_path = tmp_path / "shots.json"
    sheet_path.write_text(_sheet().model_dump_json(), encoding="utf-8")
    output = tmp_path / "described.json"

    result = CliRunner().invoke(
        app, ["describe", str(sheet_path), "--no-vision", "-o", str(output)]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["rules"] == "cn-platforms.yaml#douyin"
    assert len(payload["shots"]) == 2


def test_cli_reports_vision_coverage_and_warns_below_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "demo.mp4").write_bytes(b"fake video")
    sheet_path = tmp_path / "shots.json"
    sheet_path.write_text(_sheet().model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("ARK_API_KEY", "test-key")

    def fake_describe(sheet, video, *, provider, api_key, progress=None):
        described = sheet.shots[0].model_copy(
            update={
                "shot_size": ShotSize.CLOSE_UP,
                "camera": Camera.EYE_LEVEL,
                "movement": Movement.NONE,
                "scene": "厨房台面",
            }
        )
        return sheet.model_copy(update={"shots": [described, sheet.shots[1]]})

    monkeypatch.setattr("shotsheet.cli.describe_sheet", fake_describe)

    result = CliRunner().invoke(
        app, ["describe", str(sheet_path), "-o", str(tmp_path / "out.json")]
    )

    assert result.exit_code == 0, result.output
    message = result.output + result.stderr
    assert "vision coverage: 1/2 shots" in message
    assert "warning:" in message
