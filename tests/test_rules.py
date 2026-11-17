"""Table-driven tests for platform rule validation; no network or media needed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shotsheet.cli import app
from shotsheet.pipeline.rules import (
    Violation,
    apply_platform_rules,
    load_rulebook,
)
from shotsheet.schema import Caption, CaptionBox, Platform, Shot, ShotSheet, Source

# Comfortably inside every shipped profile: top 8-10%, bottom 20%,
# left 5%, right 10-12%.
INSIDE = CaptionBox(top=15, bottom=70, left=10, right=85)


def _sheet(
    platform: Platform = Platform.DOUYIN,
    *,
    shot_ms: tuple[int, ...] = (2000, 3000),
    captions: tuple[bool, ...] | None = None,
    caption_box: CaptionBox | None = None,
) -> ShotSheet:
    if captions is None:
        captions = tuple(False for _ in shot_ms)
    shots = [
        Shot(
            index=index,
            tc_in=f"00:00:0{index}:00",
            tc_out=f"00:00:0{index + 1}:00",
            duration_ms=duration_ms,
            caption=(
                Caption(text=f"第{index + 1}句", in_ms=0, out_ms=duration_ms)
                if captions[index]
                else None
            ),
        )
        for index, duration_ms in enumerate(shot_ms)
    ]
    source = Source(
        file="demo.mp4",
        duration_ms=sum(shot_ms),
        fps=30.0,
        platform=platform,
    )
    if caption_box is not None:
        source = source.model_copy(update={"caption_box": caption_box})
    return ShotSheet(source=source, shots=shots)


def test_rulebook_covers_every_platform_with_provenance() -> None:
    book = load_rulebook()

    assert set(book.platforms) == set(Platform)
    for profile in book.platforms.values():
        assert profile.provenance in {"doc", "heuristic"}
        assert profile.tiers_provenance in {"doc", "heuristic"}
        assert profile.safe_area_provenance in {"doc", "heuristic"}
        assert profile.hook_window_provenance in {"doc", "heuristic"}
        assert profile.hook_window_note
        assert "https://" in profile.source
        # Public policy pages, not login dashboards.
        for dashboard in (
            "creator.douyin.com",
            "creator.xiaohongshu.com",
            "channels.weixin.qq.com/platform",
        ):
            assert dashboard not in profile.source
        assert profile.aspect_ratio
        assert profile.min_s > 0
        assert profile.max_s > profile.min_s
        assert list(profile.duration_tiers_s) == sorted(profile.duration_tiers_s)
        assert profile.hook_window_s > 0
        assert 0 < profile.safe_area.top < profile.safe_area.max_y < 100
        assert 0 < profile.safe_area.left < profile.safe_area.max_x < 100


def test_hook_window_is_labelled_as_community_heuristic() -> None:
    book = load_rulebook()

    for profile in book.platforms.values():
        assert profile.hook_window_s == 3
        assert profile.hook_window_provenance == "heuristic"
        assert "社区" in profile.hook_window_note


@pytest.mark.parametrize("platform", list(Platform))
def test_duration_shorter_than_platform_minimum(platform: Platform) -> None:
    report = apply_platform_rules(_sheet(platform, shot_ms=(500,)))

    duration = [v for v in report.violations if v.rule_id == f"{platform.value}.duration"]
    assert len(duration) == 1
    assert duration[0].severity == "error"
    assert duration[0].shot_index is None
    assert "短于" in duration[0].message


@pytest.mark.parametrize("platform", list(Platform))
def test_duration_longer_than_platform_maximum(platform: Platform) -> None:
    profile = load_rulebook().platforms[platform]
    too_long_ms = round(profile.max_s * 1000) + 1000

    report = apply_platform_rules(_sheet(platform, shot_ms=(too_long_ms,)))

    duration = [v for v in report.violations if v.rule_id == f"{platform.value}.duration"]
    assert len(duration) == 1
    assert duration[0].severity == "error"
    assert "超过" in duration[0].message


@pytest.mark.parametrize("platform", list(Platform))
def test_opening_shot_over_hook_window_warns(platform: Platform) -> None:
    report = apply_platform_rules(_sheet(platform, shot_ms=(4000, 1000)))

    hook = [v for v in report.violations if v.rule_id == f"{platform.value}.hook_window"]
    assert len(hook) == 1
    assert hook[0].severity == "warning"
    assert hook[0].shot_index == 0
    assert "钩子窗口" in hook[0].message
    assert "heuristic" in hook[0].message


def test_opening_shot_within_hook_window_passes() -> None:
    report = apply_platform_rules(_sheet(shot_ms=(3000, 1000)))

    assert not [v for v in report.violations if v.rule_id.endswith(".hook_window")]


def test_total_duration_beyond_largest_tier_warns() -> None:
    report = apply_platform_rules(_sheet(shot_ms=(2000, 298_000)))

    tiers = [v for v in report.violations if v.rule_id == "douyin.duration_tier"]
    assert len(tiers) == 1
    assert tiers[0].severity == "warning"
    assert tiers[0].shot_index is None


def test_inside_caption_box_sets_verdicts_and_rule_id() -> None:
    report = apply_platform_rules(_sheet(captions=(True, False), caption_box=INSIDE))

    assert report.violations == ()
    assert report.sheet.rules == "cn-platforms.yaml#douyin"
    assert report.sheet.source.caption_box == INSIDE
    assert report.sheet.shots[0].caption.safe_area_ok is True
    assert report.sheet.shots[1].caption is None


@pytest.mark.parametrize("platform", list(Platform))
def test_default_caption_box_passes_for_every_shipped_platform(platform: Platform) -> None:
    """The default band must be usable, not a violation on every shipped profile."""
    report = apply_platform_rules(_sheet(platform, captions=(True, False)))

    findings = [
        v for v in report.violations if v.rule_id == f"{platform.value}.planned_caption_safe_area"
    ]
    assert findings == []
    assert report.sheet.shots[0].caption.safe_area_ok is True
    assert report.sheet.source.caption_box == CaptionBox()
    assert (CaptionBox().top, CaptionBox().bottom) == (72.0, 80.0)
    assert (CaptionBox().left, CaptionBox().right) == (6.0, 88.0)


def test_out_of_margin_caption_box_reports_every_caption_shot() -> None:
    box = CaptionBox(top=70, bottom=95, left=10, right=85)

    report = apply_platform_rules(_sheet(captions=(True, True), caption_box=box))

    findings = [v for v in report.violations if v.rule_id == "douyin.planned_caption_safe_area"]
    assert [finding.shot_index for finding in findings] == [0, 1]
    assert all(finding.severity == "error" for finding in findings)
    assert all(shot.caption.safe_area_ok is False for shot in report.sheet.shots)


def test_out_of_margin_caption_box_without_captions_is_a_sheet_error() -> None:
    """An invalid planned band fails the CLI even before captions exist."""
    outside = CaptionBox(top=88, bottom=100, left=4, right=96)
    report = apply_platform_rules(_sheet(caption_box=outside))

    findings = [v for v in report.violations if v.rule_id == "douyin.planned_caption_safe_area"]
    assert len(findings) == 1
    assert findings[0].shot_index is None
    assert findings[0].severity == "error"


def test_violation_serializes_for_the_web_payload() -> None:
    violation = Violation(
        rule_id="douyin.hook_window",
        platform=Platform.DOUYIN,
        shot_index=0,
        severity="warning",
        message="开场过长",
    )

    assert violation.as_dict() == {
        "rule_id": "douyin.hook_window",
        "platform": "douyin",
        "shot_index": 0,
        "severity": "warning",
        "message": "开场过长",
    }


def test_cli_rules_prints_findings_and_fails_on_errors(tmp_path: Path) -> None:
    path = tmp_path / "shots.json"
    path.write_text(_sheet(shot_ms=(500,)).model_dump_json(), encoding="utf-8")

    result = CliRunner().invoke(app, ["rules", str(path)])

    assert result.exit_code == 1
    assert "douyin.duration" in result.output
    assert "1 error(s)" in result.output


def test_cli_rules_json_output(tmp_path: Path) -> None:
    path = tmp_path / "shots.json"
    path.write_text(_sheet(shot_ms=(500,)).model_dump_json(), encoding="utf-8")

    result = CliRunner().invoke(app, ["rules", str(path), "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload[0]["rule_id"] == "douyin.duration"
    assert payload[0]["platform"] == "douyin"


def test_cli_rules_exits_zero_for_a_clean_sheet(tmp_path: Path) -> None:
    path = tmp_path / "shots.json"
    path.write_text(
        _sheet(captions=(True, False), caption_box=INSIDE).model_dump_json(), encoding="utf-8"
    )

    result = CliRunner().invoke(app, ["rules", str(path)])

    assert result.exit_code == 0, result.output
    assert "0 finding(s)" in result.output


def test_cli_caption_box_override_fires_a_finding(tmp_path: Path) -> None:
    path = tmp_path / "shots.json"
    path.write_text(
        _sheet(captions=(True, False), caption_box=INSIDE).model_dump_json(), encoding="utf-8"
    )

    result = CliRunner().invoke(app, ["rules", str(path), "--caption-box", "88,100,4,96"])

    assert result.exit_code == 1
    assert "planned_caption_safe_area" in result.output


def test_cli_caption_box_example_errors_on_a_captionless_quickstart_sheet(
    tmp_path: Path,
) -> None:
    """The README example runs on a freshly analysed sheet, before any captions."""
    path = tmp_path / "shots.json"
    path.write_text(_sheet(shot_ms=(2000, 2000, 2000)).model_dump_json(), encoding="utf-8")

    result = CliRunner().invoke(app, ["rules", str(path), "--caption-box", "88,100,4,96"])

    assert result.exit_code == 1
    assert "planned_caption_safe_area" in result.output
    assert "1 error(s)" in result.output
