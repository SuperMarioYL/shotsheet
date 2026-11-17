"""Tests for the vision-description wiring that do not touch the network."""

from __future__ import annotations

import json
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
)
from shotsheet.schema import Platform, Shot, ShotSheet, Source


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
