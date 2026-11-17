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
    """Format a frame number as non-drop-frame ``HH:MM:SS:FF``.

    The minutes and seconds count nominally (``round(fps)`` frames per second)
    and the frame field is the remainder, which is how NDF timecode is written.
    A 29.97 fps clip therefore reads ``00:00:06:00`` at frame 180 even though
    that frame happens 6.006 s into the source; ``parse_timecode`` converts the
    frame count back to real seconds.
    """
    whole_seconds, frames = divmod(frame_num, max(1, round(fps)))
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"


def parse_timecode(timecode: str, fps: float) -> float:
    """Return ``HH:MM:SS:FF`` as real seconds.

    The fields count nominal frames, so at 29.97 fps ``00:00:06:00`` is frame
    180 at 6.006 s, not 6.000 s. Dividing the frame count by the real rate keeps
    the timecode consistent with ``source.duration_ms`` instead of drifting a
    few milliseconds per nominal second.
    """
    match = _TIMECODE_RE.match(timecode)
    if match is None:
        raise ValueError(f"invalid timecode: {timecode!r}")
    hours, minutes, seconds, frames = (int(part) for part in match.groups())
    nominal_fps = max(1, round(fps))
    total_frames = (hours * 3600 + minutes * 60 + seconds) * nominal_fps + frames
    return total_frames / fps


class CaptionBox(BaseModel):
    """The subtitle band this tool plans to place, in absolute frame percentages.

    This is the rectangle the exporter would use, not a box read from the source
    video: v0.1 cannot locate text already burned into the picture. The default
    is a conservative product choice, not a platform specification -- an 8%-tall
    band from 72% to 80% of frame height, with 6% left and 12% right margins.
    That rectangle stays inside the UI margins of all three shipped profiles
    (top 8-10%, bottom 20%, left 5%, right 10-12%), so it is a usable starting
    point rather than a guaranteed universal band.
    """

    top: float = Field(default=72.0, ge=0, le=100)
    bottom: float = Field(default=80.0, ge=0, le=100)
    left: float = Field(default=6.0, ge=0, le=100)
    right: float = Field(default=88.0, ge=0, le=100)

    @model_validator(mode="after")
    def _check_rect(self) -> CaptionBox:
        if self.top >= self.bottom:
            raise ValueError("caption box must satisfy top < bottom")
        if self.left >= self.right:
            raise ValueError("caption box must satisfy left < right")
        return self

    @classmethod
    def from_csv(cls, value: str) -> CaptionBox:
        """Parse ``top,bottom,left,right`` percentages into a caption box."""
        parts = [part.strip() for part in value.split(",")]
        if len(parts) != 4:
            raise ValueError("expected four comma-separated percentages: top,bottom,left,right")
        try:
            top, bottom, left, right = (float(part) for part in parts)
        except ValueError as exc:
            raise ValueError(f"caption box values must be numbers: {value!r}") from exc
        if not all(0 <= number <= 100 for number in (top, bottom, left, right)):
            raise ValueError("caption box percentages must stay within 0..100")
        if top >= bottom or left >= right:
            raise ValueError("caption box must satisfy top < bottom and left < right")
        return cls(top=top, bottom=bottom, left=left, right=right)


class Source(BaseModel):
    """The clip a sheet was derived from."""

    file: str = Field(description="Original file name (basename), not a local path.")
    duration_ms: int = Field(gt=0, description="Clip duration in milliseconds.")
    fps: float = Field(gt=0, description="Frame rate used for the timecode frame field.")
    platform: Platform
    caption_box: CaptionBox = Field(
        default_factory=CaptionBox,
        description=(
            "Planned subtitle band as absolute frame percentages, checked against the "
            "platform's UI margins. Not a box read from the source video."
        ),
    )


class Caption(BaseModel):
    """One subtitle line, clamped to the shot it belongs to."""

    text: str
    in_ms: int = Field(ge=0)
    out_ms: int = Field(ge=0)
    safe_area_ok: bool | None = Field(
        default=None,
        description=(
            "Whether the planned caption band (source.caption_box) stays inside the platform "
            "margins; None until checked. It says nothing about text burned into the source."
        ),
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
    shots: list[Shot] = Field(min_length=1)

    @field_validator("shots")
    @classmethod
    def _check_indices(cls, shots: list[Shot]) -> list[Shot]:
        if any(shot.index != position for position, shot in enumerate(shots)):
            raise ValueError("shots must be indexed from 0 in order")
        return shots

    def with_caption_box(self, box: CaptionBox) -> ShotSheet:
        """Return a copy of this sheet with a different planned caption band."""
        return self.model_copy(
            update={"source": self.source.model_copy(update={"caption_box": box})}
        )
