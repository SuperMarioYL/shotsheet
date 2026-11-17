"""Pydantic contract for a ShotSheet.

A sheet is the diffable, exportable record of one analysed clip: source metadata,
the platform whose caption rules apply, and one entry per shot with a source
timecode. Fields that only a vision pass can fill (shot size, camera, movement,
props, scene) stay ``None`` until that pass runs.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator

TIMECODE_PATTERN = r"^\d{2,}:[0-5]\d:[0-5]\d:\d{2,3}$"
_TIMECODE_RE = re.compile(r"^(\d{2,}):([0-5]\d):([0-5]\d):(\d{2,3})$")


class Platform(StrEnum):
    """Platform whose duration and caption-safe-area rules apply to a sheet."""

    DOUYIN = "douyin"
    XIAOHONGSHU = "xiaohongshu"
    SHIPINHAO = "shipinhao"


class ShotSize(StrEnum):
    """Shot size (景别)."""

    CLOSE_UP = "特写"
    MEDIUM_CLOSE_UP = "近景"
    MEDIUM = "中景"
    FULL = "全景"
    LONG = "远景"


class Camera(StrEnum):
    """Camera placement (机位)."""

    STATIC = "固定"
    HANDHELD = "手持"
    HIGH_ANGLE = "俯拍"
    LOW_ANGLE = "仰拍"
    EYE_LEVEL = "平视"
    OVER_SHOULDER = "过肩"


class Movement(StrEnum):
    """Camera movement (运镜)."""

    PUSH_IN = "推"
    PULL_OUT = "拉"
    PAN = "摇"
    TRACK = "移"
    FOLLOW = "跟"
    NONE = "无"


def format_timecode(frame_num: int, fps: float) -> str:
    """Format a frame number as ``HH:MM:SS:FF``.

    The frame field counts against the rounded frame rate, which keeps the string
    readable for both 25/30 and 29.97/59.94 sources.
    """
    frames_per_second = max(1, round(fps))
    whole_seconds, frames = divmod(frame_num, frames_per_second)
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"


def parse_timecode(timecode: str, fps: float) -> float:
    """Return ``HH:MM:SS:FF`` as seconds, interpreting the frame field with ``fps``."""
    match = _TIMECODE_RE.match(timecode)
    if match is None:
        raise ValueError(f"invalid timecode: {timecode!r}")
    hours, minutes, seconds, frames = (int(part) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds + frames / fps


class Source(BaseModel):
    """The clip a sheet was derived from."""

    file: str = Field(description="Original file name (basename), not a local path.")
    duration_ms: int = Field(gt=0, description="Clip duration in milliseconds.")
    fps: float = Field(gt=0, description="Frame rate used for the timecode frame field.")
    platform: Platform


class Caption(BaseModel):
    """One subtitle line, clamped to the shot it belongs to."""

    text: str
    in_ms: int = Field(ge=0)
    out_ms: int = Field(ge=0)
    safe_area_ok: bool | None = Field(
        default=None,
        description="Whether the caption stays inside the platform safe area; None until checked.",
    )

    @model_validator(mode="after")
    def _check_span(self) -> Caption:
        if self.out_ms < self.in_ms:
            raise ValueError("out_ms must not be earlier than in_ms")
        return self


class Shot(BaseModel):
    """One shot: where it starts in the source, how long it holds, and what is in it."""

    index: int = Field(ge=0, description="0-based position in the sheet.")
    tc_in: str = Field(pattern=TIMECODE_PATTERN, description="In point as HH:MM:SS:FF.")
    tc_out: str = Field(pattern=TIMECODE_PATTERN, description="Out point as HH:MM:SS:FF.")
    duration_ms: int = Field(gt=0)
    shot_size: ShotSize | None = Field(default=None, description="Shot size (景别).")
    camera: Camera | None = Field(default=None, description="Camera placement (机位).")
    movement: Movement | None = Field(default=None, description="Camera movement (运镜).")
    props: list[str] = Field(default_factory=list)
    scene: str | None = None
    caption: Caption | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def subtitle_safe(self) -> bool | None:
        """Safe-area verdict for this shot's caption; None until platform rules run."""
        return None if self.caption is None else self.caption.safe_area_ok


class ShotSheet(BaseModel):
    """The exportable unit: source metadata, the rule set in play, and the shots."""

    schema_version: Literal[1] = 1
    source: Source
    rules: str | None = Field(
        default=None, description="Rule set applied, e.g. cn-platforms.yaml#douyin."
    )
    template: str | None = Field(default=None, description="Saved sheet id this sheet extends.")
    shots: list[Shot] = Field(min_length=1)

    @field_validator("shots")
    @classmethod
    def _check_indices(cls, shots: list[Shot]) -> list[Shot]:
        if any(shot.index != position for position, shot in enumerate(shots)):
            raise ValueError("shots must be indexed from 0 in order")
        return shots
