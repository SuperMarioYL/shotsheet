"""Export a ShotSheet as a Jianying (剪映专业版) draft project folder.

Target format
-------------
The plaintext draft folder written by 剪映专业版 5.x/6.x: ``draft_content.json``
with ``version: 360000`` and ``new_version: "75.0.0"``, ``draft_meta_info.json``,
and a ``root_meta_info.json`` that registers the folder in a draft root. The
10.1 release stores the timeline in an encrypted ``draft_info.json`` instead and
ignores this plaintext file; this exporter neither writes nor decrypts it, so
the output targets versions that still read ``draft_content.json``. That target
was checked against the documented schema shape, not by importing into a
running 剪映 install -- see ``docs/demo-results.json`` for the smoke record.

Layout
------
``export_jianying_draft`` writes a draft root, not a bare project folder::

    <out>/
      root_meta_info.json          # registers the project below
      <draft_name>/
        draft_content.json         # canvas, tracks, segments, materials
        draft_meta_info.json       # project metadata for the draft list

Copy ``<draft_name>`` into 剪映's ``com.lveditor.draft`` directory and merge the
``all_draft_store`` entry into that directory's own ``root_meta_info.json``.
When the output root already has a ``root_meta_info.json`` this exporter
merges the new entry by draft id instead of replacing the file, so pointing
``--jianying-dir`` at a real editor folder does not delete other projects. An
existing project folder that holds a different draft id is left untouched and
the export fails, asking for another ``--draft-name``.

Timing is microseconds throughout, as real drafts store it. ``Shot.tc_in`` /
``tc_out`` are converted through ``schema.parse_timecode`` so the frame field
follows the clip's real frame rate instead of a nominal one.

Determinism: draft, track, material and segment ids are UUID5 values derived
from the draft name and the sheet's shots, and the two timestamps are a fixed
placeholder (剪映 rewrites them when it saves). Exporting the same sheet to the
same directory twice therefore produces byte-identical JSON.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from shotsheet.pipeline.rules import PlatformProfile, load_rulebook
from shotsheet.schema import CaptionBox, Platform, ShotSheet, parse_timecode

DRAFT_VERSION = 360000
DRAFT_NEW_VERSION = "75.0.0"
# 剪映 stores timestamps as microseconds. A constant keeps exports byte-identical;
# the editor overwrites both fields the first time it saves the project.
DRAFT_TIMESTAMP_US = 1_700_000_000_000_000
_NAMESPACE = uuid.UUID("6f1c0f0c-2a37-5c1e-9f34-9e3f0a6f4d21")

_VIDEO_TRACK_NAME = ""
_TEXT_TRACK_NAME = ""


@dataclass(frozen=True)
class PlannedSegment:
    """One timeline segment, in microseconds."""

    index: int
    timeline_start_us: int
    duration_us: int
    source_start_us: int
    caption: str | None = None

    @property
    def timeline_end_us(self) -> int:
        return self.timeline_start_us + self.duration_us


@dataclass(frozen=True)
class DraftPlan:
    """What the exporter will write, without touching the filesystem."""

    draft_name: str
    platform: Platform
    canvas_width: int
    canvas_height: int
    aspect_ratio: str
    total_us: int
    video_segments: tuple[PlannedSegment, ...]
    text_segments: tuple[PlannedSegment, ...]


@dataclass(frozen=True)
class JianyingExport:
    """Where the export landed and how many segments it wrote."""

    root: Path
    project_dir: Path
    content_path: Path
    meta_path: Path
    root_meta_path: Path
    video_segments: int
    text_segments: int
    duration_us: int


def export_jianying_draft(
    sheet: ShotSheet,
    out_dir: str | Path,
    *,
    draft_name: str | None = None,
    video_path: str | Path | None = None,
) -> JianyingExport:
    """Write SHEET as a Jianying draft root under OUT_DIR.

    ``video_path`` is embedded as the material path when given. Without it the
    material falls back to ``source.file``; 剪映 then asks the user to relink the
    clip, which is normal for a moved draft folder.
    """
    root = Path(out_dir)
    plan = plan_jianying_draft(sheet, draft_name=draft_name)
    project_dir = root / plan.draft_name

    material_path = (
        str(Path(video_path).expanduser().resolve())
        if video_path is not None
        else sheet.source.file
    )
    draft_id = _draft_id(sheet, plan)
    content = _draft_content(
        sheet,
        plan,
        draft_id=draft_id,
        material_path=material_path,
    )
    meta = _draft_meta_info(
        plan,
        draft_id=draft_id,
        draft_root=root,
        project_dir=project_dir,
        material_path=material_path,
    )
    root_meta = _root_meta_info(
        plan,
        draft_id=draft_id,
        draft_root=root,
        project_dir=project_dir,
    )

    content_path = project_dir / "draft_content.json"
    meta_path = project_dir / "draft_meta_info.json"
    root_meta_path = root / "root_meta_info.json"
    _guard_existing_draft(content_path, draft_id, project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    _write_json(content_path, content)
    _write_json(meta_path, meta)
    _register_root_meta(root_meta_path, root_meta)

    return JianyingExport(
        root=root,
        project_dir=project_dir,
        content_path=content_path,
        meta_path=meta_path,
        root_meta_path=root_meta_path,
        video_segments=len(plan.video_segments),
        text_segments=len(plan.text_segments),
        duration_us=plan.total_us,
    )


def plan_jianying_draft(sheet: ShotSheet, *, draft_name: str | None = None) -> DraftPlan:
    """Return the track/segment plan for SHEET without writing anything."""
    profile = load_rulebook().platforms[sheet.source.platform]
    name = (draft_name or _default_draft_name(sheet, profile)).strip()
    if not name.strip():
        raise ValueError("draft name must not be empty")
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise ValueError("draft name must be a single folder name")

    canvas_width, canvas_height = _canvas_size(profile.aspect_ratio)

    video_segments: list[PlannedSegment] = []
    timeline_us = 0
    for shot in sheet.shots:
        start_us = _seconds_to_us(parse_timecode(shot.tc_in, sheet.source.fps))
        end_us = _seconds_to_us(parse_timecode(shot.tc_out, sheet.source.fps))
        duration_us = max(1, end_us - start_us)
        # Timeline positions follow sheet order; source ranges keep the original
        # in/out points so every cut still points at the right frame.
        video_segments.append(
            PlannedSegment(
                index=shot.index,
                timeline_start_us=timeline_us,
                duration_us=duration_us,
                source_start_us=start_us,
            )
        )
        timeline_us += duration_us

    text_segments: list[PlannedSegment] = []
    for shot in sheet.shots:
        caption = shot.caption
        if caption is None or not caption.text.strip():
            continue
        start_us = caption.in_ms * 1000
        duration_us = max(1, (caption.out_ms - caption.in_ms) * 1000)
        text_segments.append(
            PlannedSegment(
                index=len(text_segments),
                timeline_start_us=start_us,
                duration_us=duration_us,
                source_start_us=start_us,
                caption=caption.text.strip(),
            )
        )

    return DraftPlan(
        draft_name=name,
        platform=sheet.source.platform,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        aspect_ratio=profile.aspect_ratio,
        total_us=timeline_us,
        video_segments=tuple(video_segments),
        text_segments=tuple(text_segments),
    )


def describe_draft_plan(plan: DraftPlan, root: str | Path) -> str:
    """Render the plan as the tree printed by ``shotsheet export --dry-run``."""
    root = Path(root)
    lines = [
        f"{root}/",
        "  root_meta_info.json",
        f"  {plan.draft_name}/",
        "    draft_content.json",
        "    draft_meta_info.json",
        "",
        (
            f"canvas {plan.canvas_width}x{plan.canvas_height} ({plan.aspect_ratio}), "
            f"{_format_us(plan.total_us)} total, platform {plan.platform.value}"
        ),
        f"video track: {len(plan.video_segments)} segment(s)",
    ]
    for segment in plan.video_segments:
        lines.append(
            f"  #{segment.index + 1:02d} {_format_us(segment.timeline_start_us)} -> "
            f"{_format_us(segment.timeline_end_us)}  "
            f"source {_format_us(segment.source_start_us)}  ({_format_us(segment.duration_us)})"
        )
    lines.append(f"text track: {len(plan.text_segments)} segment(s)")
    for segment in plan.text_segments:
        caption = segment.caption or ""
        lines.append(
            f"  #{segment.index + 1:02d} {_format_us(segment.timeline_start_us)} -> "
            f"{_format_us(segment.timeline_end_us)}  {caption!r}"
        )
    return "\n".join(lines)


def _default_draft_name(sheet: ShotSheet, profile: PlatformProfile) -> str:
    # Platform and aspect are part of the name: the same clip exported for
    # Douyin (1080x1920) and Xiaohongshu (1080x1440) must not collide in a
    # draft root, which lists projects by folder name.
    stem = Path(sheet.source.file).stem or "shotsheet"
    aspect_tag = profile.aspect_ratio.replace(":", "x")
    return f"{stem}-{sheet.source.platform.value}-{aspect_tag}"


def _draft_id(sheet: ShotSheet, plan: DraftPlan) -> str:
    # Platform and aspect stay in the key as well, so passing the same explicit
    # --draft-name for two platforms still yields distinct draft ids and the
    # overwrite guard fires instead of silently replacing a differently sized
    # canvas.
    key = "/".join(
        [
            "shotsheet",
            plan.draft_name,
            plan.platform.value,
            plan.aspect_ratio,
            sheet.source.file,
            str(sheet.source.duration_ms),
            *(f"{shot.tc_in}-{shot.tc_out}-{shot.duration_ms}" for shot in sheet.shots),
        ]
    )
    return str(uuid.uuid5(_NAMESPACE, key))


def _canvas_size(aspect_ratio: str) -> tuple[int, int]:
    try:
        width_part, height_part = (int(part) for part in aspect_ratio.split(":"))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"unsupported aspect ratio {aspect_ratio!r}") from exc
    if width_part <= 0 or height_part <= 0:
        raise ValueError(f"unsupported aspect ratio {aspect_ratio!r}")
    width = 1080
    # Keep the canvas height even; encoders and Jianying both prefer it.
    height = round(width * height_part / width_part / 2) * 2
    return width, height


def _draft_content(
    sheet: ShotSheet,
    plan: DraftPlan,
    *,
    draft_id: str,
    material_path: str,
) -> dict[str, object]:
    video_material_id = str(uuid.uuid5(_NAMESPACE, f"{draft_id}/material/video/0"))
    video_track_id = str(uuid.uuid5(_NAMESPACE, f"{draft_id}/track/video/0"))

    video_segments = [
        _video_segment(
            segment,
            draft_id=draft_id,
            video_material_id=video_material_id,
        )
        for segment in plan.video_segments
    ]
    tracks: list[dict[str, object]] = [
        {
            "attribute": 0,
            "flag": 0,
            "id": video_track_id,
            "is_default_name": True,
            "name": _VIDEO_TRACK_NAME,
            "segments": video_segments,
            "type": "video",
        }
    ]

    texts: list[dict[str, object]] = []
    if plan.text_segments:
        text_track_id = str(uuid.uuid5(_NAMESPACE, f"{draft_id}/track/text/0"))
        text_segments = []
        for segment in plan.text_segments:
            material_id = str(uuid.uuid5(_NAMESPACE, f"{draft_id}/material/text/{segment.index}"))
            texts.append(_text_material(segment.caption or "", material_id))
            text_segments.append(
                _text_segment(
                    segment,
                    draft_id=draft_id,
                    text_material_id=material_id,
                    caption_box=sheet.source.caption_box,
                )
            )
        tracks.append(
            {
                "attribute": 0,
                "flag": 0,
                "id": text_track_id,
                "is_default_name": True,
                "name": _TEXT_TRACK_NAME,
                "segments": text_segments,
                "type": "text",
            }
        )

    materials: dict[str, object] = _empty_materials()
    materials["videos"] = [
        {
            "aigc_type": "none",
            "audio_fade": None,
            "category_name": "local",
            "check_flag": 63487,
            "crop": {
                "lower_left_x": 0.0,
                "lower_left_y": 0.0,
                "lower_right_x": 1.0,
                "lower_right_y": 1.0,
                "upper_left_x": 0.0,
                "upper_left_y": 0.0,
                "upper_right_x": 1.0,
                "upper_right_y": 1.0,
            },
            "crop_ratio": "free",
            "crop_scale": 1.0,
            "duration": plan.total_us,
            "height": plan.canvas_height,
            "id": video_material_id,
            "local_material_id": "",
            "material_name": sheet.source.file,
            "media_path": material_path,
            "path": material_path,
            "type": "video",
            "width": plan.canvas_width,
        }
    ]
    materials["texts"] = texts

    return {
        "canvas_config": {
            "height": plan.canvas_height,
            "ratio": "original",
            "width": plan.canvas_width,
        },
        "color_space": 0,
        "config": _draft_config(),
        "duration": plan.total_us,
        "id": draft_id,
        "materials": materials,
        "tracks": tracks,
        "version": DRAFT_VERSION,
        "new_version": DRAFT_NEW_VERSION,
    }


def _draft_config() -> dict[str, object]:
    return {
        "adjust_max_index": 1,
        "attachment_info": [],
        "combination_max_index": 1,
        "export_range": None,
        "extract_audio_last_index": 1,
        "lyrics_recognition_id": "",
        "lyrics_sync": True,
        "lyrics_taskinfo": [],
        "maintrack_adsorb": True,
        "material_save_mode": 0,
        "multi_language_current": "none",
        "multi_language_list": [],
        "multi_language_main": "none",
        "multi_language_mode": "none",
        "original_sound_last_index": 1,
        "record_audio_last_index": 1,
        "sticker_max_index": 1,
        "subtitle_keywords_config": None,
        "subtitle_recognition_id": "",
        "subtitle_sync": True,
        "subtitle_taskinfo": [],
        "system_font_list": [],
        "video_mute": False,
        "zoom_info_params": None,
    }


def _empty_materials() -> dict[str, object]:
    """Material buckets a plaintext draft carries, even when unused."""
    keys = [
        "ai_translates",
        "audio_balances",
        "audio_effects",
        "audio_fades",
        "audio_track_indexes",
        "audios",
        "beats",
        "canvases",
        "chromas",
        "color_curves",
        "digital_humans",
        "drafts",
        "effects",
        "flowers",
        "green_screens",
        "handwrites",
        "hsl",
        "images",
        "log_color_wheels",
        "loudnesses",
        "manual_deformations",
        "masks",
        "material_animations",
        "material_colors",
        "multi_language_refs",
        "placeholders",
        "plugin_effects",
        "primary_color_wheels",
        "realtime_denoises",
        "shapes",
        "smart_crops",
        "smart_relights",
        "sound_channel_mappings",
        "speeds",
        "stickers",
        "tail_leaders",
        "text_templates",
        "texts",
        "time_marks",
        "transitions",
        "video_effects",
        "video_trackings",
        "videos",
        "vocal_beautifys",
        "vocal_separations",
    ]
    return {key: [] for key in keys}


def _video_segment(
    segment: PlannedSegment, *, draft_id: str, video_material_id: str
) -> dict[str, object]:
    return {
        "caption_info": None,
        "cartoon": False,
        "clip": _clip_transform(x=0.0, y=0.0),
        "common_keyframes": [],
        "enable_adjust": False,
        "enable_color_correct_adjust": False,
        "enable_color_curves": True,
        "enable_color_match_adjust": False,
        "enable_color_wheels": True,
        "enable_lut": True,
        "enable_smart_color_adjust": False,
        "extra_material_refs": [],
        "group_id": "",
        "hdr_settings": {"intensity": 1.0, "mode": 1, "nits": 1000},
        "id": str(uuid.uuid5(_NAMESPACE, f"{draft_id}/segment/video/{segment.index}")),
        "intensifies_audio": False,
        "is_placeholder": False,
        "is_tone_modify": False,
        "keyframe_refs": [],
        "last_nonzero_volume": 1.0,
        "material_id": video_material_id,
        "render_index": 0,
        "reverse": False,
        "source_timerange": {
            "duration": segment.duration_us,
            "start": segment.source_start_us,
        },
        "speed": 1.0,
        "target_timerange": {
            "duration": segment.duration_us,
            "start": segment.timeline_start_us,
        },
        "template_id": "",
        "template_scene": "default",
        "track_attribute": 0,
        "track_render_index": 0,
        "uniform_scale": {"on": True, "value": 1.0},
        "visible": True,
        "volume": 1.0,
    }


def _text_segment(
    segment: PlannedSegment,
    *,
    draft_id: str,
    text_material_id: str,
    caption_box: CaptionBox,
) -> dict[str, object]:
    # 剪映's clip transform is relative to half the canvas, with y pointing up.
    # Centre the planned caption band so exported text starts inside the band.
    centre_x = (caption_box.left + caption_box.right) / 2
    centre_y = (caption_box.top + caption_box.bottom) / 2
    return {
        "clip": _clip_transform(
            x=round((centre_x - 50) / 50, 4),
            y=round((50 - centre_y) / 50, 4),
        ),
        "common_keyframes": [],
        "enable_adjust": False,
        "extra_material_refs": [],
        "id": str(uuid.uuid5(_NAMESPACE, f"{draft_id}/segment/text/{segment.index}")),
        "material_id": text_material_id,
        "render_index": 1,
        "source_timerange": {"duration": segment.duration_us, "start": 0},
        "target_timerange": {
            "duration": segment.duration_us,
            "start": segment.timeline_start_us,
        },
        "track_attribute": 0,
        "track_render_index": 1,
        "visible": True,
        "volume": 1.0,
    }


def _clip_transform(*, x: float, y: float) -> dict[str, object]:
    return {
        "alpha": 1.0,
        "flip": {"horizontal": False, "vertical": False},
        "rotation": 0.0,
        "scale": {"x": 1.0, "y": 1.0},
        "transform": {"x": x, "y": y},
    }


def _text_material(text: str, material_id: str) -> dict[str, object]:
    return {
        "add_type": 0,
        "alignment": 1,
        "background_alpha": 1.0,
        "background_color": "",
        "bold_width": 0.0,
        "border_alpha": 1.0,
        "border_color": "#000000",
        "border_width": 0.08,
        "check_flag": 7,
        "content": text,
        "fixed_width": -1,
        "font_category_id": "",
        "font_category_name": "",
        "font_id": "",
        "font_name": "",
        "font_path": "",
        "font_resource_id": "",
        "font_size": 8.0,
        "font_source_platform": 0,
        "font_team_id": "",
        "font_title": "none",
        "font_url": "",
        "fonts": [],
        "force_apply_line_max_width": False,
        "global_alpha": 1.0,
        "has_shadow": False,
        "id": material_id,
        "initial_scale": 1.0,
        "inner_padding": -1.0,
        "is_rich_text": False,
        "italic_degree": 0,
        "keyword_highlight": [],
        "language": "",
        "layer_weight": 1,
        "letter_spacing": 0.0,
        "line_spacing": 0.02,
        "multi_language_current": "none",
        "name": "",
        "original_size": [],
        "preset_category": "",
        "preset_category_id": "",
        "preset_has_set_alignment": False,
        "preset_id": "",
        "preset_index": 0,
        "preset_name": "",
        "recognize_type": 0,
        "relevance_segment": [],
        "shadow_alpha": 0.9,
        "shadow_angle": -45.0,
        "shadow_color": "#000000",
        "shadow_distance": 8.0,
        "shadow_smoothing": 0.75,
        "stroke_color": "#000000",
        "stroke_width": 0.0,
        "style_name": "",
        "sub_type": 0,
        "text_alpha": 1.0,
        "text_color": "#FFFFFF",
        "text_size": 30,
        "type": "text",
        "typesetting": 0,
        "underline": False,
        "vertical": False,
    }


def _draft_meta_info(
    plan: DraftPlan,
    *,
    draft_id: str,
    draft_root: Path,
    project_dir: Path,
    material_path: str,
) -> dict[str, object]:
    return {
        "draft_cloud_capcut_purchase_info": "",
        "draft_cloud_last_action_download": False,
        "draft_cloud_materials": [],
        "draft_cloud_purchase_info": "",
        "draft_cloud_template_id": "",
        "draft_cloud_tutorial_info": "",
        "draft_cloud_videocut_purchase_info": "",
        "draft_cover": "draft_cover.jpg",
        "draft_fold_path": str(project_dir.resolve()),
        "draft_id": draft_id,
        "draft_is_ai_shorts": False,
        "draft_is_article_video_draft": False,
        "draft_is_invisible": False,
        "draft_is_web_article_video": False,
        "draft_json_file": "draft_content.json",
        "draft_materials": [
            {
                "type": 0,
                "value": [
                    {
                        "create_time": DRAFT_TIMESTAMP_US,
                        "duration": plan.total_us,
                        "file_Path": material_path,
                        "height": plan.canvas_height,
                        "id": str(uuid.uuid5(_NAMESPACE, f"{draft_id}/material/meta/video")),
                        "import_time": DRAFT_TIMESTAMP_US,
                        "import_time_ms": DRAFT_TIMESTAMP_US // 1000,
                        "item_source": 1,
                        "md5": "",
                        "metetype": "video",
                        "roughcut_time_range": {"duration": plan.total_us, "start": 0},
                        "sub_time_range": {"duration": -1, "start": -1},
                        "type": 0,
                        "width": plan.canvas_width,
                    }
                ],
            },
            *({"type": kind, "value": []} for kind in range(1, 6)),
        ],
        "draft_materials_copied_info": [],
        "draft_name": plan.draft_name,
        "draft_new_version": DRAFT_NEW_VERSION,
        "draft_removable_storage_space": 0,
        "draft_segment_extra_info": [],
        "draft_timeline_materials_size": 0,
        "draft_type": "",
        "tm_draft_create": DRAFT_TIMESTAMP_US,
        "tm_draft_modified": DRAFT_TIMESTAMP_US,
        "tm_draft_removed": 0,
        "tm_duration": plan.total_us,
    }


def _root_meta_info(
    plan: DraftPlan,
    *,
    draft_id: str,
    draft_root: Path,
    project_dir: Path,
) -> dict[str, object]:
    return {
        "all_draft_store": [
            {
                "draft_cloud_last_action_download": False,
                "draft_cloud_purchase_info": "",
                "draft_cloud_template_id": "",
                "draft_cloud_tutorial_info": "",
                "draft_cloud_videocut_purchase_info": "",
                "draft_cover": "draft_cover.jpg",
                "draft_fold_path": str(project_dir.resolve()),
                "draft_id": draft_id,
                "draft_is_ai_shorts": False,
                "draft_is_invisible": False,
                "draft_json_file": "draft_content.json",
                "draft_name": plan.draft_name,
                "draft_new_version": DRAFT_NEW_VERSION,
                "draft_root_path": str(draft_root.resolve()),
                "draft_timeline_materials_size": 0,
                "tm_draft_create": DRAFT_TIMESTAMP_US,
                "tm_draft_modified": DRAFT_TIMESTAMP_US,
                "tm_draft_removed": 0,
                "tm_duration": plan.total_us,
            }
        ],
        "draft_ids": [draft_id],
        "root_path": str(draft_root.resolve()),
    }


def _seconds_to_us(seconds: float) -> int:
    return int(round(seconds * 1_000_000))


def _format_us(value: int) -> str:
    seconds, micros = divmod(max(0, value), 1_000_000)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{micros // 1000:03d}"


def _guard_existing_draft(content_path: Path, draft_id: str, project_dir: Path) -> None:
    """Refuse to overwrite a draft folder that belongs to a different sheet."""
    if not content_path.is_file():
        return
    try:
        existing = json.loads(content_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"refusing to overwrite unreadable {content_path}: {exc}") from exc
    existing_id = existing.get("id") if isinstance(existing, dict) else None
    if existing_id != draft_id:
        raise ValueError(
            f"{project_dir} already contains a different draft; choose another --draft-name"
        )


def _register_root_meta(path: Path, payload: dict[str, object]) -> None:
    """Merge the new draft into an existing root_meta_info.json.

    ``--jianying-dir`` may point at a real 剪映 draft root. Replacing that file
    would delete every other project from the editor's draft list, so new
    entries are merged by draft id and unknown fields are preserved.
    """
    store = payload.get("all_draft_store")
    entry = store[0] if isinstance(store, list) and store else None
    if not isinstance(entry, dict):
        raise ValueError("draft plan is missing its root_meta_info entry")
    draft_id = entry.get("draft_id")
    if not isinstance(draft_id, str):
        raise ValueError("draft plan is missing its draft id")

    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"refusing to overwrite unreadable {path}: {exc}") from exc
        if not isinstance(existing, dict):
            raise ValueError(f"refusing to overwrite {path}: not a JSON object")
        entries = existing.get("all_draft_store")
        if not isinstance(entries, list):
            entries = []
        for index, item in enumerate(entries):
            if isinstance(item, dict) and item.get("draft_id") == draft_id:
                entries[index] = entry
                break
        else:
            entries.append(entry)
        existing["all_draft_store"] = entries
        ids = existing.get("draft_ids")
        if not isinstance(ids, list):
            ids = []
        if draft_id not in ids:
            ids.append(draft_id)
        existing["draft_ids"] = ids
        if "root_path" not in existing:
            existing["root_path"] = payload.get("root_path")
        payload = existing
    _write_json(path, payload)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
