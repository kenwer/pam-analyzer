# Changelog

## unreleased
### Changed
- Disable macOS release as its size exceeds the GitHub release limit.

## [0.1.0] - 2026-05-27
### Added
- Initial release with MVP functionality.
- Project management: create, load, save, and reopen recent projects (`.pamproj` format).
- Campaign management panel to organize monitoring deployments, including location selection via an interactive map picker.
- SD import functionality at campaign level to load audio files from SD cards into local directories.
- Species list support: load custom species list files when creating campaigns.
- Audio analysis via BirdNET-2.4 and Perch-2.0 models with configurable min confidence and overlap.
- Examine panel with data table displaying detections, supporting filters, column visibility toggles, auto-sizing, and data exporting.
- Integrated audio player with spectrogram for quick playback of detected snippets.
