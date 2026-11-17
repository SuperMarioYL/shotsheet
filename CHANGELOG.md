# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `describe` fills each shot's 景别/机位/运镜/道具/场景 through an
  OpenAI-compatible vision endpoint (豆包 by default, 通义千问 VL optional).
  Frames are sampled per shot; `--no-vision` keeps the fields empty.
  `describe` now accepts a shots.json as its positional argument, reports
  vision coverage, and logs any model value outside the allowed enums.
- Inline editing in the local web UI: vision cells are editable, `PUT /sheet`
  validates the complete sheet with the pydantic contract, re-runs the rules,
  and writes it back to `shots.json` immediately. The page also has a provider
  selector, an in-memory API-key field, and per-shot progress text.
- `--caption-box top,bottom,left,right` overrides the planned subtitle band on
  `analyze`, `describe`, and `rules`.
- `rules` validates duration limits, the opening hook window, and the planned
  caption band against `rules/cn-platforms.yaml`. Findings carry a rule id,
  severity, and a per-shot or whole-sheet message.
- `serve` starts a local single-page UI on 127.0.0.1; drag a clip in to see the
  shot table and rule flags. Uploads stay in a temporary directory.
- `analyze` now applies platform rules and writes a three-column sheet
  (timeline/镜头, 字幕, 规则) with vision fields under each timecode.
- `analyze` now writes a printable storyboard sheet (`shots.html`, or
  `--html PATH`) beside the JSON.
- Optional local ASR via `--transcribe`, attaching each shot's transcript text.
- Timecode conversion that accounts for the clip's real frame rate.

### Fixed

- The subtitle check compares the planned caption band from
  `source.caption_box` against each platform's UI margins, instead of checking
  a band derived from the same margins (which could never fail). The rule is
  named `planned_caption_safe_area` and its docs state that v0.1 cannot read
  text already burned into the source video.
- Platform rule provenance is explicit: public policy-page citations for
  duration limits, `provenance: heuristic` for community-observed safe-area and
  hook-window values, with every threshold still editable in the YAML.
- Pin `av<19`; faster-whisper 1.2.x still passes `metadata_errors` to
  `av.open`, which av 19 removed, so `--transcribe` no longer crashes with a
  `TypeError`. Transcription failures now raise an actionable `AnalyzeError`.
- Non-video or unreadable input reports a clear error instead of a scenedetect
  `VideoOpenFailure` traceback.
- Warn on stderr when a clip longer than three seconds yields only one shot.

## [0.1.0] - 2026-11-18

### Added

- Project scaffold: packaging, package layout, CI, and release automation.
