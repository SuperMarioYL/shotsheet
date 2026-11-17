"""Platform rule validation for a shot sheet.

The rules live in ``rules/cn-platforms.yaml``; this module only loads and
applies them. Three checks run for the sheet's platform:

- total duration against the platform's published minimum/maximum;
- the tool's planned subtitle band against the platform's percentage-based
  safe area (bottom info bar, right interaction rail, top status area);
- the opening hook: the first shot must not run past the platform's hook
  window without a cut.

There is no caption bounding box in the v0.1 contract, so the safe-area
verdict is a property of the planned subtitle band, applied to every caption.
It is still reported per shot, and each caption's ``safe_area_ok`` is set, so
the exported sheet can be audited shot by shot.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from shotsheet.schema import Platform, Shot, ShotSheet

RULEBOOK_PATH = Path(__file__).resolve().parents[1] / "rules" / "cn-platforms.yaml"


class RulesError(RuntimeError):
    """The rule book is missing, unreadable, or does not describe the platform."""


@dataclass(frozen=True)
class SafeArea:
    """A rectangle in percent.

    Platform profiles store margins from each frame edge (top/bottom/left/
    right); the tool's planned subtitle band stores absolute positions. The
    parser validates each interpretation, and ``max_y``/``max_x`` convert the
    margin form to absolute bounds.
    """

    top: float
    bottom: float
    left: float
    right: float

    @property
    def max_y(self) -> float:
        return 100 - self.bottom

    @property
    def max_x(self) -> float:
        return 100 - self.right


@dataclass(frozen=True)
class PlatformProfile:
    """One platform's limits, safe area, and opening-hook window."""

    platform: Platform
    label: str
    source: str
    aspect_ratio: str
    min_s: float
    max_s: float
    duration_tiers_s: tuple[float, ...]
    safe_area: SafeArea
    hook_window_s: float


@dataclass(frozen=True)
class RuleBook:
    """The full rule file: shared subtitle band plus one profile per platform."""

    version: str
    subtitle_band: SafeArea
    platforms: dict[Platform, PlatformProfile]


@dataclass(frozen=True)
class Violation:
    """One rule finding, tied to a shot or to the sheet as a whole."""

    rule_id: str
    platform: Platform
    shot_index: int | None
    severity: Literal["error", "warning"]
    message: str

    def as_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "platform": self.platform.value,
            "shot_index": self.shot_index,
            "severity": self.severity,
            "message": self.message,
        }


@dataclass(frozen=True)
class RuleReport:
    """The sheet with safe-area verdicts filled in, plus every finding."""

    sheet: ShotSheet
    profile: PlatformProfile
    violations: tuple[Violation, ...]

    @property
    def errors(self) -> tuple[Violation, ...]:
        return tuple(v for v in self.violations if v.severity == "error")


def load_rulebook(path: str | Path | None = None) -> RuleBook:
    """Load and validate the YAML rule book (default: the packaged file)."""
    target = Path(path) if path is not None else RULEBOOK_PATH
    if not target.is_file():
        raise RulesError(f"rule book not found: {target}")

    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - pyyaml is a declared dependency
        raise RulesError("reading the rule book needs the 'PyYAML' package") from exc

    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RulesError(f"could not read {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise RulesError(f"{target} must contain a YAML mapping")

    return _parse_rulebook(data, source=target)


def _parse_rulebook(data: dict, *, source: Path) -> RuleBook:
    try:
        version = str(data["version"])
        band = _parse_safe_area(
            data["subtitle_band_pct"], f"{source}: subtitle_band_pct", absolute=True
        )
        raw_platforms = data["platforms"]
    except KeyError as exc:
        raise RulesError(f"{source} is missing {exc.args[0]!r}") from exc
    if not isinstance(raw_platforms, dict):
        raise RulesError(f"{source}: platforms must be a mapping")

    platforms: dict[Platform, PlatformProfile] = {}
    for name, raw in raw_platforms.items():
        try:
            platform = Platform(str(name))
        except ValueError as exc:
            raise RulesError(f"{source}: unknown platform {name!r}") from exc
        platforms[platform] = _parse_profile(platform, raw, f"{source}: platforms.{name}")

    missing = set(Platform) - set(platforms)
    if missing:
        names = ", ".join(sorted(p.value for p in missing))
        raise RulesError(f"{source} has no profile for: {names}")
    return RuleBook(version=version, subtitle_band=band, platforms=platforms)


def _parse_profile(platform: Platform, raw: object, where: str) -> PlatformProfile:
    if not isinstance(raw, dict):
        raise RulesError(f"{where} must be a mapping")
    try:
        duration = raw["duration_s"]
        tiers = tuple(float(value) for value in duration["tiers"])
        min_s = float(duration["min"])
        max_s = float(duration["max"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RulesError(f"{where}.duration_s is invalid: {exc}") from exc

    if min_s <= 0 or max_s < min_s:
        raise RulesError(f"{where}.duration_s must satisfy 0 < min <= max")
    if not tiers or list(tiers) != sorted(tiers):
        raise RulesError(f"{where}.duration_s.tiers must be a sorted, non-empty list")

    try:
        profile = PlatformProfile(
            platform=platform,
            label=str(raw["label"]),
            source=str(raw["source"]),
            aspect_ratio=str(raw["aspect_ratio"]),
            min_s=min_s,
            max_s=max_s,
            duration_tiers_s=tiers,
            safe_area=_parse_safe_area(raw["subtitle_safe_area_pct"], where),
            hook_window_s=float(raw["hook_window_s"]),
        )
    except KeyError as exc:
        raise RulesError(f"{where} is missing {exc.args[0]!r}") from exc
    if profile.hook_window_s <= 0:
        raise RulesError(f"{where}.hook_window_s must be positive")
    return profile


def _parse_safe_area(raw: object, where: str, *, absolute: bool = False) -> SafeArea:
    if not isinstance(raw, dict):
        raise RulesError(f"{where} must be a mapping")
    try:
        area = SafeArea(
            top=float(raw["top"]),
            bottom=float(raw["bottom"]),
            left=float(raw["left"]),
            right=float(raw["right"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RulesError(f"{where} must define top/bottom/left/right percentages: {exc}") from exc
    if not all(0 <= value <= 100 for value in (area.top, area.bottom, area.left, area.right)):
        raise RulesError(f"{where} percentages must stay within 0..100")
    if absolute:
        if area.top >= area.bottom or area.left >= area.right:
            raise RulesError(f"{where} must satisfy top < bottom and left < right")
    elif area.top >= area.max_y or area.left >= area.max_x:
        raise RulesError(f"{where} leaves no usable area")
    return area


def apply_platform_rules(sheet: ShotSheet, *, rulebook: RuleBook | None = None) -> RuleReport:
    """Validate SHEET against its platform profile and fill safe-area verdicts."""
    book = rulebook or load_rulebook()
    profile = book.platforms.get(sheet.source.platform)
    if profile is None:
        raise RulesError(f"no rules for platform {sheet.source.platform.value!r}")

    platform = profile.platform
    violations: list[Violation] = []

    total_s = sheet.source.duration_ms / 1000
    if total_s < profile.min_s:
        violations.append(
            Violation(
                rule_id=f"{platform.value}.duration",
                platform=platform,
                shot_index=None,
                severity="error",
                message=(f"整条视频 {total_s:.1f}s，短于{profile.label}最短 {profile.min_s:g}s。"),
            )
        )
    elif total_s > profile.max_s:
        violations.append(
            Violation(
                rule_id=f"{platform.value}.duration",
                platform=platform,
                shot_index=None,
                severity="error",
                message=(
                    f"整条视频 {total_s:.1f}s，超过{profile.label}上限 "
                    f"{profile.max_s:g}s（{profile.max_s / 60:g} 分钟）。"
                ),
            )
        )

    largest_tier = profile.duration_tiers_s[-1]
    if largest_tier < total_s <= profile.max_s:
        violations.append(
            Violation(
                rule_id=f"{platform.value}.duration_tier",
                platform=platform,
                shot_index=None,
                severity="warning",
                message=(
                    f"整条视频 {total_s:.1f}s，超过{profile.label}常用档位 "
                    f"{largest_tier:g}s（平台上限 {profile.max_s:g}s）；完播率可能下降。"
                ),
            )
        )

    first_shot = sheet.shots[0]
    first_s = first_shot.duration_ms / 1000
    if first_s > profile.hook_window_s:
        violations.append(
            Violation(
                rule_id=f"{platform.value}.hook_window",
                platform=platform,
                shot_index=first_shot.index,
                severity="warning",
                message=(
                    f"开场镜头 #{first_shot.index + 1} 持续 {first_s:.1f}s，超过"
                    f"{profile.label}前 {profile.hook_window_s:g}s 钩子窗口；"
                    f"建议在前 {profile.hook_window_s:g}s 内切镜、加动作或字幕。"
                ),
            )
        )

    band_problems = _band_problems(book.subtitle_band, profile.safe_area)
    caption_shots = [shot for shot in sheet.shots if shot.caption is not None]
    if band_problems:
        message = "字幕区越过{}安全区：{}".format(profile.label, "；".join(band_problems))
        if caption_shots:
            for shot in caption_shots:
                violations.append(
                    Violation(
                        rule_id=f"{platform.value}.subtitle_safe_area",
                        platform=platform,
                        shot_index=shot.index,
                        severity="error",
                        message=f"镜头 #{shot.index + 1}：{message}",
                    )
                )
        else:
            violations.append(
                Violation(
                    rule_id=f"{platform.value}.subtitle_safe_area",
                    platform=platform,
                    shot_index=None,
                    severity="warning",
                    message=message,
                )
            )

    # The safe-area verdict is the same for every caption: the planned band
    # either fits the platform's UI margins or it does not.
    safe_area_ok = not band_problems
    shots = [
        shot if shot.caption is None else _with_caption_verdict(shot, safe_area_ok=safe_area_ok)
        for shot in sheet.shots
    ]
    checked = sheet.model_copy(
        update={
            "rules": f"cn-platforms.yaml#{platform.value}",
            "shots": shots,
        }
    )
    return RuleReport(sheet=checked, profile=profile, violations=tuple(violations))


def _band_problems(band: SafeArea, safe: SafeArea) -> list[str]:
    problems: list[str] = []
    if band.top < safe.top:
        problems.append(f"顶部 {band.top:g}% 高于安全区 {safe.top:g}%")
    if band.bottom > safe.max_y:
        problems.append(
            f"底部 {band.bottom:g}% 低于安全区 {safe.max_y:g}%（底部 {safe.bottom:g}% 为 UI）"
        )
    if band.left < safe.left:
        problems.append(f"左侧 {band.left:g}% 小于安全区 {safe.left:g}%")
    if band.right > safe.max_x:
        problems.append(
            f"右侧 {band.right:g}% 超过安全区 {safe.max_x:g}%（右侧 {safe.right:g}% 为 UI）"
        )
    return problems


def _with_caption_verdict(shot: Shot, *, safe_area_ok: bool) -> Shot:
    """Return SHOT with its caption's safe-area verdict set."""
    if shot.caption is None:  # pragma: no cover - guarded by the caller
        return shot
    return shot.model_copy(
        update={"caption": shot.caption.model_copy(update={"safe_area_ok": safe_area_ok})}
    )
