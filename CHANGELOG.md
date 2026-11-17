# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-11-18

### m1 — 本地视频到 shots.json

- `shotsheet analyze` detects shot boundaries with `scenedetect`, converts
  frame numbers to non-drop-frame timecodes at the clip's real frame rate, and
  writes a validated `shots.json`.
- Optional local ASR via `--transcribe` (faster-whisper), with transcript text
  clamped to each shot and exposed as that shot's caption.
- Three-column printable sheet (`shots.html`, or `--html PATH`) with timeline,
  captions and rule findings; the analysis command applies platform rules
  before writing it.
- Clear errors for missing or unreadable video input, and a warning when a clip
  longer than three seconds produces only one shot.
- Timecode conversion verified at 29.97 fps: frame 180 is 6.006 s, not 6.000 s.

### m2 — 逐镜描述、平台规则与网页编辑

- `shotsheet describe` fills each shot's 景别/机位/运镜/道具/场景 through an
  OpenAI-compatible vision endpoint (豆包 by default, 通义千问 VL optional).
  Frames are sampled per shot; `--no-vision` keeps the fields empty.
  `describe` accepts a shots.json as its positional argument, reports vision
  coverage, and logs model values outside the allowed enums.
- `shotsheet rules` validates duration limits, the opening hook window, and the
  planned caption band from `rules/cn-platforms.yaml`. Findings carry a rule
  id, severity, and a per-shot or whole-sheet message.
- `shotsheet serve` starts a local single-page UI on 127.0.0.1. Uploads stay in
  a temporary directory; vision cells are editable and `PUT /sheet` validates
  the complete sheet, re-runs the rules, and writes it back immediately.
- `--caption-box top,bottom,left,right` overrides the planned subtitle band on
  `analyze`, `describe`, and `rules`; the web page exposes the same four values
  as inputs.
- Platform rule provenance is explicit: public policy pages for duration
  limits, `provenance: heuristic` for community-observed safe-area and hook
  values, with every threshold editable in the YAML.
- `pipeline/rules.py` never hardcodes a platform number; `cn-platforms.yaml` is
  the single source for all three profiles.

### m3 — 剪映 draft 导出

- `shotsheet export <sheet.json> --jianying-dir <out>` writes a Jianying draft
  root: `root_meta_info.json` plus `<draft-name>/draft_content.json` and
  `draft_meta_info.json`. `--video` embeds the source clip path.
- One video segment per shot, with `target_timerange` and `source_timerange`
  converted from `tc_in` / `tc_out` to microseconds. Captions become a text
  track positioned from the planned caption band.
- `--dry-run` prints the planned canvas, track and per-segment timeline/source
  tree without writing anything.
- An existing `root_meta_info.json` in the output root is merged by draft id,
  so pointing `--jianying-dir` at a real 剪映 draft folder does not erase other
  projects. A project folder that already holds a different draft is left
  untouched and the export asks for another `--draft-name`.
- Draft, track, material and segment ids are deterministic UUID5 values and the
  two timestamps are a fixed placeholder, so rerunning the same export produces
  byte-identical JSON.
- Targets the plaintext `draft_content.json` format (`version: 360000`,
  `new_version: "75.0.0"`) read by 剪映专业版 5.x/6.x. 剪映 10.1 uses an encrypted
  `draft_info.json`; the exporter neither writes nor decrypts it.
- `docs/demo-results.json` records a real 60-second synthetic run: 12 shots,
  12 video segments, 60,000,000 µs, one hook-window warning, and the target
  version limitations.
- `tests/test_jianying.py` covers segment counts, microsecond timing, caption
  tracks, schema keys, canvas sizing, the 29.97 fps path, dry-run behavior, and
  deterministic output.

### Fixed

- The default planned caption box is now `top 72 / bottom 80 / left 6 / right 88`
  percent, which stays inside the UI margins of all three shipped platform
  profiles. The previous default sat inside the bottom UI band and outside the
  side margins on every profile, so it always reported a violation. Its
  docstring now describes the actual rectangle.
- The subtitle check compares `source.caption_box` against each platform's UI
  margins instead of a band derived from the same margins (which could never
  fail). The rule is named `planned_caption_safe_area`; v0.1 cannot read text
  already burned into the source video.
- `av<19` is pinned because faster-whisper 1.2.x still passes `metadata_errors`
  to `av.open`, which av 19 removed; `--transcribe` no longer crashes.
- Non-video or unreadable input reports a clear error instead of a scenedetect
  `VideoOpenFailure` traceback.

### Added

- Project scaffold: packaging, package layout, CI, and release automation.
