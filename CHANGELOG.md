# Changelog

## [0.1.1] - 2026-04-02
### Fixed
- Fix windows build

## [0.1.0] - 2026-04-02
### Added
- Initial release with MVP functionality.
- Project management: create, load, save, and reopen recent projects (`.pamproj` format).
- Campaign management panel to organise monitoring deployments, including location selection via an interactive map picker.
- Species list support: load custom species list files when creating campaigns, with optional localized species names (multiple languages).
- Import panel to load audio files from SD cards and local directories, with import progress log.
- Audio analysis powered by BirdNET-Analyzer with configurable minimum confidence threshold and per-ARU detection limit.
- Examine panel with an AG Grid table displaying detections, supporting filters, column visibility toggles, auto-sizing, and exporting visible columns and rows.
- Integrated audio player in the file column for quick playback of detected snippets.
- SD card watch hints to guide correct card mounting.
