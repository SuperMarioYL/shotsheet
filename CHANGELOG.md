# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `analyze` now writes a printable two-column storyboard sheet (`shots.html`, or
  `--html PATH`) beside the JSON, with a timeline/镜头 column and a 字幕 column.
- Optional local ASR via `--transcribe`, attaching each shot's transcript text.
- Timecode conversion that accounts for the clip's real frame rate.

### Fixed

- Pin `av<19`; faster-whisper 1.2.x still passes `metadata_errors` to
  `av.open`, which av 19 removed, so `--transcribe` no longer crashes with a
  `TypeError`. Transcription failures now raise an actionable `AnalyzeError`.
- Non-video or unreadable input reports a clear error instead of a scenedetect
  `VideoOpenFailure` traceback.
- Warn on stderr when a clip longer than three seconds yields only one shot.

## [0.1.0] - 2026-11-18

### Added

- Project scaffold: packaging, package layout, CI, and release automation.
