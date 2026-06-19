# Changelog

## development
### Fixed
- Windows: model files are now bundled, so analysis runs offline instead of downloading the models on first run.
- Windows: BirdNET and Perch analysis no longer crashes with "'NoneType' object has no attribute 'write'".

## [0.1.1] - 2026-05-28
### Changed
- Disable macOS release as its size exceeds the GitHub release limit.
- [dev] Switch CI to run test-fast and omit the slow tests.

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
