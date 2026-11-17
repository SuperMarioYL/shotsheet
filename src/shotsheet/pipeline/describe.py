"""Per-shot vision description through an OpenAI-compatible endpoint.

Two Chinese vision providers are configured from the plan's ``model_targets``:

- ``doubao`` (default): Volcano Ark, model ``doubao-seed-2-1-pro-260915``,
  base URL ``https://ark.cn-beijing.volces.com/api/v3`` (append
  ``/chat/completions``), key from ``ARK_API_KEY``.
- ``qwen``: DashScope compatible mode, model ``qwen3-vl-plus``, base URL
  ``https://dashscope.aliyuncs.com/compatible-mode/v1``, key from
  ``DASHSCOPE_API_KEY``.

Both accept OpenAI-style ``image_url`` entries whose URL is a base64 data URL.
For each shot the pipeline extracts three frames (15%, 50%, 85% of the shot)
with ffmpeg, asks the model for the fields the sheet needs, and merges the
answer into the shot. ``httpx`` is imported at call time.

Without a configured key this module raises :class:`AnalyzeError`; it never
invents placeholder descriptions. Callers that do not want vision can skip
:func:`describe_sheet` entirely (the CLI's ``--no-vision`` path).

Model values outside the allowed enums are logged and left empty rather than
guessed; :func:`vision_coverage` reports how many shots ended up complete so a
run cannot quietly look successful with missing fields.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from shotsheet.pipeline.analyze import AnalyzeError
from shotsheet.schema import Camera, Movement, Shot, ShotSheet, ShotSize, parse_timecode

LOGGER = logging.getLogger(__name__)

MAX_CONCURRENCY = 3
FRAMES_PER_SHOT = 3
_FRAME_POSITIONS = (0.15, 0.5, 0.85)
_REQUEST_TIMEOUT_S = 90.0


@dataclass(frozen=True)
class Provider:
    """One OpenAI-compatible vision endpoint."""

    name: str
    label: str
    model: str
    base_url: str
    api_key_env: str
    docs_url: str


PROVIDERS: dict[str, Provider] = {
    "doubao": Provider(
        name="doubao",
        label="字节火山方舟（豆包视觉）",
        model="doubao-seed-2-1-pro-260915",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key_env="ARK_API_KEY",
        docs_url="https://console.volcengine.com/ark",
    ),
    "qwen": Provider(
        name="qwen",
        label="阿里云百炼（通义千问 VL）",
        model="qwen3-vl-plus",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY",
        docs_url="https://bailian.console.aliyun.com/",
    ),
}
DEFAULT_PROVIDER = "doubao"


@dataclass(frozen=True)
class ShotDescription:
    """The vision fields one shot can carry."""

    shot_size: ShotSize | None
    camera: Camera | None
    movement: Movement | None
    props: tuple[str, ...]
    scene: str | None


def get_provider(name: str = DEFAULT_PROVIDER) -> Provider:
    """Return the provider config, or raise with the available choices."""
    try:
        return PROVIDERS[name]
    except KeyError as exc:
        choices = ", ".join(PROVIDERS)
        raise AnalyzeError(f"未知的视觉 provider {name!r}；可选：{choices}") from exc


def has_api_key(name: str = DEFAULT_PROVIDER) -> bool:
    """Whether the provider's key is present in the environment."""
    return bool(os.environ.get(get_provider(name).api_key_env))


def require_api_key(name: str = DEFAULT_PROVIDER) -> tuple[Provider, str]:
    """Return ``(provider, key)`` or fail with the exact variable to set."""
    provider = get_provider(name)
    key = os.environ.get(provider.api_key_env)
    if not key:
        raise AnalyzeError(
            f"{provider.label} 视觉描述需要 API Key，但环境变量 "
            f"{provider.api_key_env} 未设置。请先 export {provider.api_key_env}=<key>"
            f"（豆包用 ARK_API_KEY，通义用 DASHSCOPE_API_KEY），或加 --no-vision "
            "跳过视觉描述。"
        )
    return provider, key


def describe_sheet(
    sheet: ShotSheet,
    video_path: str | Path,
    *,
    provider: str = DEFAULT_PROVIDER,
    api_key: str | None = None,
    concurrency: int = MAX_CONCURRENCY,
    progress: Callable[[int, int], None] | None = None,
) -> ShotSheet:
    """Fill each shot's vision fields from the clip, preserving all other data.

    Frames are extracted from ``video_path`` at three points inside every shot.
    The function is all-or-nothing: a missing key, unreadable video, failed
    request, or non-JSON model answer raises :class:`AnalyzeError` rather than
    leaving a half-described sheet. ``progress`` is called as ``(done, total)``
    after each shot resolves so a caller can show per-shot progress.
    """
    config = get_provider(provider)
    key = api_key or os.environ.get(config.api_key_env)
    if not key:
        # Reuse the message and validation from the single key entry point.
        config, key = require_api_key(provider)

    path = Path(video_path)
    if not path.is_file():
        raise AnalyzeError(f"video file not found: {path}")

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise AnalyzeError(
            "视觉描述需要对每个镜头抽帧，但没有找到 ffmpeg。请先安装 ffmpeg 并确保在 PATH 中。"
        )

    try:
        import httpx
    except ImportError as exc:
        raise AnalyzeError(
            "视觉描述需要 'httpx'（pip install httpx）；不需要时加 --no-vision。"
        ) from exc

    workers = max(1, min(concurrency, MAX_CONCURRENCY, len(sheet.shots)))
    descriptions: dict[int, ShotDescription] = {}
    with httpx.Client(timeout=_REQUEST_TIMEOUT_S) as client:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    _describe_shot,
                    client=client,
                    config=config,
                    key=key,
                    video=path,
                    shot=shot,
                    fps=sheet.source.fps,
                    ffmpeg=ffmpeg,
                ): shot
                for shot in sheet.shots
            }
            try:
                done = 0
                total = len(futures)
                for future in as_completed(futures):
                    shot = futures[future]
                    descriptions[shot.index] = future.result()
                    done += 1
                    if progress is not None:
                        progress(done, total)
            except BaseException:
                for future in futures:
                    future.cancel()
                raise

    shots = [_merge_description(shot, descriptions[shot.index]) for shot in sheet.shots]
    return sheet.model_copy(update={"shots": shots})


def _describe_shot(
    *,
    client,
    config: Provider,
    key: str,
    video: Path,
    shot: Shot,
    fps: float,
    ffmpeg: str,
) -> ShotDescription:
    frames = _extract_frames(ffmpeg, video, shot, fps)
    payload = {
        "model": config.model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": _prompt(shot.duration_ms / 1000, len(frames)),
                    },
                    *(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/jpeg;base64,"
                                + base64.b64encode(frame).decode("ascii")
                            },
                        }
                        for frame in frames
                    ),
                ],
            }
        ],
        "temperature": 0.1,
        "max_tokens": 400,
    }
    url = f"{config.base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {key}"}

    try:
        response = client.post(url, json=payload, headers=headers)
    except Exception as exc:  # httpx transport errors, timeouts, DNS, TLS
        raise AnalyzeError(
            f"镜头 #{shot.index + 1} 的视觉请求失败（{config.model}）：{exc}"
        ) from exc
    if response.status_code >= 400:
        detail = " ".join(response.text.split())[:300]
        raise AnalyzeError(
            f"镜头 #{shot.index + 1} 的视觉请求失败：HTTP {response.status_code} {detail}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise AnalyzeError(
            f"镜头 #{shot.index + 1} 的视觉接口没有返回 JSON：{response.text[:200]}"
        ) from exc

    text = _message_text(body, shot.index)
    data = _parse_json_object(text, shot.index)
    return _description_from_payload(data, shot.index)


def _extract_frames(ffmpeg: str, video: Path, shot: Shot, fps: float) -> list[bytes]:
    start_s = parse_timecode(shot.tc_in, fps)
    duration_s = shot.duration_ms / 1000
    frames: list[bytes] = []
    for position in _FRAME_POSITIONS[:FRAMES_PER_SHOT]:
        timestamp = start_s + duration_s * position
        command = [
            ffmpeg,
            "-nostdin",
            "-loglevel",
            "error",
            "-ss",
            f"{timestamp:.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            "scale='min(720,iw)':-2",
            "-q:v",
            "4",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "pipe:1",
        ]
        result = subprocess.run(command, capture_output=True, check=False)
        if result.returncode != 0 or not result.stdout:
            stderr = result.stderr.decode("utf-8", "replace").strip().splitlines()
            reason = stderr[-1] if stderr else f"exit code {result.returncode}"
            raise AnalyzeError(f"镜头 #{shot.index + 1} 抽帧失败（{timestamp:.2f}s）：{reason}")
        frames.append(result.stdout)
    return frames


def _prompt(duration_s: float, frame_count: int) -> str:
    return (
        "你是短视频分镜师。下面按时间顺序给出同一个镜头的 "
        f"{frame_count} 帧画面，镜头时长约 {duration_s:.2f} 秒。\n"
        "只输出一个 JSON 对象，不要解释，不要 Markdown 代码块。字段必须是：\n"
        "{\n"
        '  "shot_size": "特写|近景|中景|全景|远景",\n'
        '  "camera": "固定|手持|俯拍|仰拍|平视|过肩",\n'
        '  "movement": "推|拉|摇|移|跟|无",\n'
        '  "props": ["画面中可见的关键道具，最多 6 个"],\n'
        '  "scene": "一句话描述场景"\n'
        "}\n"
        "景别、机位、运镜只能从给定选项里选；多帧之间构图没有明显变化时 movement 用“无”。"
    )


def _message_text(body: object, shot_index: int) -> str:
    try:
        content = body["choices"][0]["message"]["content"]  # type: ignore[index]
    except (KeyError, IndexError, TypeError) as exc:
        snippet = json.dumps(body, ensure_ascii=False)[:300]
        raise AnalyzeError(f"镜头 #{shot_index + 1} 的视觉接口返回格式异常：{snippet}") from exc
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not isinstance(content, str) or not content.strip():
        raise AnalyzeError(f"镜头 #{shot_index + 1} 的视觉接口返回了空内容")
    return content


def _parse_json_object(text: str, shot_index: int) -> dict:
    body = text.strip()
    if body.startswith("```"):
        body = "\n".join(line for line in body.splitlines() if not line.startswith("```"))
    start = body.find("{")
    end = body.rfind("}")
    if start == -1 or end < start:
        raise AnalyzeError(f"镜头 #{shot_index + 1} 的视觉模型没有返回 JSON：{text.strip()[:200]}")
    try:
        data = json.loads(body[start : end + 1])
    except json.JSONDecodeError as exc:
        raise AnalyzeError(
            f"镜头 #{shot_index + 1} 的视觉模型返回的 JSON 无法解析：{text.strip()[:200]}"
        ) from exc
    if not isinstance(data, dict):
        raise AnalyzeError(f"镜头 #{shot_index + 1} 的视觉模型没有返回 JSON 对象")
    return data


def _description_from_payload(data: dict, shot_index: int) -> ShotDescription:
    raw_props = data.get("props", [])
    if isinstance(raw_props, str):
        raw_props = [raw_props]
    if not isinstance(raw_props, list):
        raw_props = []
    props = tuple(
        dict.fromkeys(
            prop.strip()[:40] for prop in raw_props if isinstance(prop, str) and prop.strip()
        )
    )[:8]

    raw_scene = data.get("scene")
    scene = raw_scene.strip()[:120] if isinstance(raw_scene, str) else None
    return ShotDescription(
        shot_size=_enum_value(ShotSize, data.get("shot_size"), "shot_size", shot_index),
        camera=_enum_value(Camera, data.get("camera"), "camera", shot_index),
        movement=_enum_value(Movement, data.get("movement"), "movement", shot_index),
        props=props,
        scene=scene or None,
    )


def _enum_value(enum_type, value: object, field: str, shot_index: int):
    """Normalise one model value, logging anything outside the allowed labels."""
    if value is None:
        return None
    if not isinstance(value, str):
        LOGGER.warning("镜头 #%d 的 %s 返回非字符串 %r，已留空", shot_index + 1, field, value)
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    for member in enum_type:
        if member.value == cleaned:
            return member
    allowed = "/".join(member.value for member in enum_type)
    LOGGER.warning(
        "镜头 #%d 的 %s 返回 %r，不在允许值 %s 内，已留空",
        shot_index + 1,
        field,
        cleaned,
        allowed,
    )
    return None


def vision_coverage(sheet: ShotSheet) -> tuple[int, int]:
    """Return ``(shots_with_all_fields, total_shots)`` for the vision pass.

    A shot counts as covered when the three enum fields and the scene sentence
    are present. ``props`` may legitimately be empty for a shot with no
    prominent object, so it is not part of the coverage test.
    """
    covered = sum(
        1
        for shot in sheet.shots
        if shot.shot_size is not None
        and shot.camera is not None
        and shot.movement is not None
        and bool(shot.scene)
    )
    return covered, len(sheet.shots)


def _merge_description(shot: Shot, description: ShotDescription) -> Shot:
    return shot.model_copy(
        update={
            "shot_size": description.shot_size,
            "camera": description.camera,
            "movement": description.movement,
            "props": list(description.props),
            "scene": description.scene,
        }
    )
