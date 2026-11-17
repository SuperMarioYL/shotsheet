"""Tests for the Jianying draft exporter; no 剪映 install or media is needed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shotsheet.cli import app
from shotsheet.render.jianying import (
    DRAFT_NEW_VERSION,
    DRAFT_VERSION,
    describe_draft_plan,
    export_jianying_draft,
    plan_jianying_draft,
)
from shotsheet.schema import Caption, Platform, Shot, ShotSheet, Source


def _sheet(
    *,
    platform: Platform = Platform.DOUYIN,
    captions: bool = True,
    fps: float = 30.0,
) -> ShotSheet:
    shots = [
        Shot(
            index=0,
            tc_in="00:00:00:00",
            tc_out="00:00:02:00",
            duration_ms=2000,
            caption=Caption(text="开场就在厨房", in_ms=200, out_ms=1800) if captions else None,
        ),
        Shot(
            index=1,
            tc_in="00:00:02:00",
            tc_out="00:00:05:00",
            duration_ms=3000,
            caption=Caption(text="她把杯子推过来", in_ms=2100, out_ms=4900) if captions else None,
        ),
        Shot(
            index=2,
            tc_in="00:00:05:00",
            tc_out="00:00:10:00",
            duration_ms=5000,
        ),
    ]
    return ShotSheet(
        source=Source(file="demo.mp4", duration_ms=10_000, fps=fps, platform=platform),
        shots=shots,
    )


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_export_writes_schema_tracks_and_microsecond_timing(tmp_path: Path) -> None:
    result = export_jianying_draft(_sheet(), tmp_path, draft_name="demo")

    content = _load(result.content_path)
    assert content["version"] == DRAFT_VERSION == 360000
    assert content["new_version"] == DRAFT_NEW_VERSION == "75.0.0"
    assert content["duration"] == 10_000_000
    assert content["canvas_config"] == {"height": 1920, "ratio": "original", "width": 1080}
    assert content["id"]
    for key in ("canvas_config", "tracks", "materials", "config", "color_space"):
        assert key in content

    assert result.video_segments == 3
    assert result.text_segments == 2
    video_track = next(track for track in content["tracks"] if track["type"] == "video")
    text_track = next(track for track in content["tracks"] if track["type"] == "text")
    assert [segment["target_timerange"] for segment in video_track["segments"]] == [
        {"duration": 2_000_000, "start": 0},
        {"duration": 3_000_000, "start": 2_000_000},
        {"duration": 5_000_000, "start": 5_000_000},
    ]
    assert [segment["source_timerange"] for segment in video_track["segments"]] == [
        {"duration": 2_000_000, "start": 0},
        {"duration": 3_000_000, "start": 2_000_000},
        {"duration": 5_000_000, "start": 5_000_000},
    ]
    assert len(content["materials"]["videos"]) == 1
    assert len(content["materials"]["texts"]) == 2
    assert len(text_track["segments"]) == 2
    assert text_track["segments"][0]["target_timerange"] == {
        "duration": 1_600_000,
        "start": 200_000,
    }
    assert text_track["segments"][1]["target_timerange"] == {
        "duration": 2_800_000,
        "start": 2_100_000,
    }


def test_export_writes_meta_and_root_meta_for_registration(tmp_path: Path) -> None:
    result = export_jianying_draft(_sheet(), tmp_path, draft_name="demo")

    content = _load(result.content_path)
    meta = _load(result.meta_path)
    root_meta = _load(result.root_meta_path)

    assert result.project_dir == tmp_path / "demo"
    assert meta["draft_id"] == content["id"]
    assert meta["draft_name"] == "demo"
    assert meta["draft_json_file"] == "draft_content.json"
    assert meta["draft_fold_path"] == str(result.project_dir.resolve())
    assert meta["tm_duration"] == 10_000_000
    assert meta["draft_materials"][0]["type"] == 0
    assert meta["draft_materials"][0]["value"][0]["metetype"] == "video"

    assert root_meta["draft_ids"] == [content["id"]]
    assert root_meta["root_path"] == str(tmp_path.resolve())
    entry = root_meta["all_draft_store"][0]
    assert entry["draft_id"] == content["id"]
    assert entry["draft_fold_path"] == str(result.project_dir.resolve())
    assert entry["draft_root_path"] == str(tmp_path.resolve())


def test_canvas_follows_the_platform_aspect_ratio(tmp_path: Path) -> None:
    result = export_jianying_draft(
        _sheet(platform=Platform.XIAOHONGSHU), tmp_path, draft_name="xhs"
    )

    assert _load(result.content_path)["canvas_config"] == {
        "height": 1440,
        "ratio": "original",
        "width": 1080,
    }


def test_microseconds_follow_the_real_frame_rate(tmp_path: Path) -> None:
    sheet = ShotSheet(
        source=Source(
            file="ntsc.mp4", duration_ms=6006, fps=30000 / 1001, platform=Platform.DOUYIN
        ),
        shots=[Shot(index=0, tc_in="00:00:00:00", tc_out="00:00:06:00", duration_ms=6006)],
    )
    result = export_jianying_draft(sheet, tmp_path, draft_name="ntsc")

    segment = _load(result.content_path)["tracks"][0]["segments"][0]
    # Frame 180 at 29.97 fps is 6.006 s, not 6.000 s.
    assert segment["target_timerange"]["duration"] == 6_006_000


def test_export_is_byte_identical_when_rerun(tmp_path: Path) -> None:
    export_jianying_draft(_sheet(), tmp_path, draft_name="demo")
    first = {path.name: path.read_bytes() for path in (tmp_path / "demo").glob("*.json")}
    first["root_meta_info.json"] = (tmp_path / "root_meta_info.json").read_bytes()

    export_jianying_draft(_sheet(), tmp_path, draft_name="demo")
    second = {path.name: path.read_bytes() for path in (tmp_path / "demo").glob("*.json")}
    second["root_meta_info.json"] = (tmp_path / "root_meta_info.json").read_bytes()

    assert first == second


def test_without_captions_there_is_no_text_track(tmp_path: Path) -> None:
    result = export_jianying_draft(_sheet(captions=False), tmp_path, draft_name="silent")

    content = _load(result.content_path)
    assert result.text_segments == 0
    assert [track["type"] for track in content["tracks"]] == ["video"]
    assert content["materials"]["texts"] == []


def test_video_path_is_embedded_as_material_path(tmp_path: Path) -> None:
    clip = tmp_path / "demo.mp4"
    clip.write_bytes(b"not really a video")
    out = tmp_path / "draft"

    result = export_jianying_draft(_sheet(), out, draft_name="demo", video_path=clip)

    video = _load(result.content_path)["materials"]["videos"][0]
    assert video["path"] == str(clip.resolve())
    assert video["media_path"] == str(clip.resolve())
    assert _load(result.meta_path)["draft_materials"][0]["value"][0]["file_Path"] == str(
        clip.resolve()
    )


def test_export_merges_an_existing_root_meta(tmp_path: Path) -> None:
    root_meta = tmp_path / "root_meta_info.json"
    root_meta.write_text(
        json.dumps(
            {
                "all_draft_store": [{"draft_id": "other", "draft_name": "other"}],
                "draft_ids": ["other"],
                "root_path": "/keep/this",
            }
        ),
        encoding="utf-8",
    )

    result = export_jianying_draft(_sheet(), tmp_path, draft_name="demo")

    data = _load(root_meta)
    ids = [entry["draft_id"] for entry in data["all_draft_store"]]
    assert ids == ["other", _load(result.content_path)["id"]]
    assert data["draft_ids"] == ids
    assert data["root_path"] == "/keep/this"


def test_export_refuses_to_replace_a_different_draft_folder(tmp_path: Path) -> None:
    export_jianying_draft(_sheet(), tmp_path, draft_name="demo")
    content_path = tmp_path / "demo" / "draft_content.json"
    original_id = _load(content_path)["id"]
    other = _sheet().model_copy(
        update={"source": _sheet().source.model_copy(update={"file": "other.mp4"})}
    )

    with pytest.raises(ValueError, match="different draft"):
        export_jianying_draft(other, tmp_path, draft_name="demo")

    assert _load(content_path)["id"] == original_id


@pytest.mark.parametrize("name", ["../escape", "nested/name", "..", "  "])
def test_draft_name_must_be_a_single_folder(name: str) -> None:
    with pytest.raises(ValueError):
        plan_jianying_draft(_sheet(), draft_name=name)


def test_describe_plan_prints_tracks_and_does_not_write(tmp_path: Path) -> None:
    plan = plan_jianying_draft(_sheet(), draft_name="demo")

    text = describe_draft_plan(plan, tmp_path)

    assert "root_meta_info.json" in text
    assert "canvas 1080x1920 (9:16), 00:00:10.000 total" in text
    assert "video track: 3 segment(s)" in text
    assert "#01 00:00:00.000 -> 00:00:02.000" in text
    assert "text track: 2 segment(s)" in text
    assert "'开场就在厨房'" in text
    assert list(tmp_path.iterdir()) == []


def test_cli_export_dry_run_writes_nothing(tmp_path: Path) -> None:
    sheet_path = tmp_path / "shots.json"
    sheet_path.write_text(_sheet().model_dump_json(indent=2), encoding="utf-8")
    out = tmp_path / "draft-root"

    result = CliRunner().invoke(
        app,
        ["export", str(sheet_path), "--jianying-dir", str(out), "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert "video track: 3 segment(s)" in result.output
    assert "text track: 2 segment(s)" in result.output
    assert not out.exists()


def test_cli_export_writes_a_draft_root(tmp_path: Path) -> None:
    sheet_path = tmp_path / "shots.json"
    sheet_path.write_text(_sheet().model_dump_json(indent=2), encoding="utf-8")
    out = tmp_path / "draft-root"

    result = CliRunner().invoke(
        app,
        [
            "export",
            str(sheet_path),
            "--jianying-dir",
            str(out),
            "--draft-name",
            "cli-demo",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (out / "root_meta_info.json").is_file()
    assert (out / "cli-demo" / "draft_content.json").is_file()
    assert (out / "cli-demo" / "draft_meta_info.json").is_file()
    assert "3 video + 2 text segment(s)" in result.output


def test_cli_export_rejects_a_bad_sheet(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"source": {}}', encoding="utf-8")

    result = CliRunner().invoke(app, ["export", str(bad), "--jianying-dir", str(tmp_path)])

    assert result.exit_code == 1
    assert "not a shot sheet" in result.output
