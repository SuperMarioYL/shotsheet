"""Local clip analysis: shot boundaries, per-shot timecodes, and optional ASR text.

``scenedetect`` and ``faster-whisper`` are imported at call time so the package
stays importable (and ``shotsheet --version`` works) without either installed.
Each missing piece is reported only when the feature that needs it is requested.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shotsheet.schema import (
    Caption,
    Platform,
    Shot,
    ShotSheet,
    Source,
    format_timecode,
    parse_timecode,
)

DEFAULT_ASR_MODEL = "small"


class AnalyzeError(RuntimeError):
    """Analysis could not run; the message says what is missing or unreadable."""


@dataclass(frozen=True)
class CaptionSegment:
    """One ASR utterance, in source seconds."""

    start_s: float
    end_s: float
    text: str


@dataclass(frozen=True)
class _Scene:
    """A detected shot, in frames plus its formatted source timecodes."""

    start_frame: int
    end_frame: int
    tc_in: str
    tc_out: str


def analyze_video(
    video_path: str | Path,
    *,
    platform: Platform = Platform.DOUYIN,
    transcribe: bool = False,
    asr_model: str = DEFAULT_ASR_MODEL,
    language: str | None = None,
) -> ShotSheet:
    """Detect shots in a local clip and return a validated ``ShotSheet``.

    ASR is opt-in because it downloads a local model; when enabled, each shot's
    overlapping transcript becomes its caption.
    """
    path = Path(video_path)
    if not path.is_file():
        raise AnalyzeError(f"video file not found: {path}")

    scenes, fps = _detect_scenes(path)
    shots = [
        Shot(
            index=index,
            tc_in=scene.tc_in,
            tc_out=scene.tc_out,
            duration_ms=max(1, round((scene.end_frame - scene.start_frame) * 1000 / fps)),
        )
        for index, scene in enumerate(scenes)
    ]

    if transcribe:
        segments = transcribe_video(path, model_size=asr_model, language=language)
        shots = attach_captions(shots, segments, fps)

    return ShotSheet(
        source=Source(
            file=path.name,
            duration_ms=max(1, round(scenes[-1].end_frame * 1000 / fps)),
            fps=fps,
            platform=platform,
        ),
        shots=shots,
    )


def transcribe_video(
    video_path: str | Path,
    *,
    model_size: str = DEFAULT_ASR_MODEL,
    language: str | None = None,
) -> list[CaptionSegment]:
    """Transcribe the clip's audio track locally with faster-whisper."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise AnalyzeError(
            "ASR captions need the 'faster-whisper' package (pip install faster-whisper)"
        ) from exc

    model = WhisperModel(model_size, device="auto", compute_type="int8")
    segments, _info = model.transcribe(str(video_path), language=language)
    return [
        CaptionSegment(start_s=float(segment.start), end_s=float(segment.end), text=segment.text)
        for segment in segments
    ]


def attach_captions(
    shots: Sequence[Shot], segments: Sequence[CaptionSegment], fps: float
) -> list[Shot]:
    """Return the shots with any overlapping transcript text attached as one caption."""
    captioned: list[Shot] = []
    for shot in shots:
        shot_start_s = parse_timecode(shot.tc_in, fps)
        shot_end_s = parse_timecode(shot.tc_out, fps)
        overlap = [
            segment
            for segment in segments
            if segment.end_s > shot_start_s and segment.start_s < shot_end_s
        ]
        # faster-whisper keeps the leading space on Latin segments, so concatenate raw text.
        text = "".join(segment.text for segment in overlap).strip()
        if not text:
            captioned.append(shot)
            continue

        in_ms = max(
            round(shot_start_s * 1000),
            min(round(segment.start_s * 1000) for segment in overlap),
        )
        out_ms = min(
            round(shot_end_s * 1000),
            max(round(segment.end_s * 1000) for segment in overlap),
        )
        captioned.append(
            shot.model_copy(update={"caption": Caption(text=text, in_ms=in_ms, out_ms=out_ms)})
        )
    return captioned


def _detect_scenes(path: Path) -> tuple[list[_Scene], float]:
    try:
        from scenedetect import FrameTimecode, SceneManager, open_video
        from scenedetect.detectors import ContentDetector
    except ImportError as exc:
        raise AnalyzeError(
            "scene detection needs the 'scenedetect' package (pip install scenedetect)"
        ) from exc

    video = open_video(str(path))
    try:
        fps = _frame_rate(video)
        manager = SceneManager()
        manager.add_detector(ContentDetector())
        manager.detect_scenes(video, show_progress=False)
        scenes = manager.get_scene_list(start_in_scene=True)
        total_frames = _frame_num(video.duration)
    finally:
        video.close()

    if not scenes:
        if total_frames <= 0:
            raise AnalyzeError(f"could not read a video duration from {path}")
        scenes = [(FrameTimecode(0, fps), FrameTimecode(total_frames, fps))]

    detected: list[_Scene] = []
    for start, end in scenes:
        start_frame = _frame_num(start)
        end_frame = _frame_num(end)
        detected.append(
            _Scene(
                start_frame=start_frame,
                end_frame=end_frame,
                tc_in=format_timecode(start_frame, fps),
                tc_out=format_timecode(end_frame, fps),
            )
        )
    return detected, fps


def _frame_rate(video: Any) -> float:
    # scenedetect 0.7 renamed VideoStream.framerate to frame_rate; both read the same value.
    rate = getattr(video, "frame_rate", None)
    if rate is None:
        rate = getattr(video, "framerate", None)
    if not rate:
        raise AnalyzeError("could not read the video frame rate")
    return float(rate)


def _frame_num(timecode: Any) -> int:
    # scenedetect 0.7 exposes frame_num; earlier releases only expose get_frames().
    frame_num = getattr(timecode, "frame_num", None)
    if frame_num is None:
        frame_num = timecode.get_frames()
    return int(frame_num)
