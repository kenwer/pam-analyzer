# Changelog

## unreleased
### Added
- Add support for `.flac`, `.mp3`, `.ogg`, `.m4a`, `.wma`, `.aiff`, `.aif` audio file formats.
### Fixed
- BirdNET result CSVs containing non-UTF-8 file paths (e.g. from older runs on Windows with cp1252 encoding) are now parsed correctly via a latin-1 fallback.
### Changed
- BirdNET progress bar switches to an indeterminate "Finalizing..." state once all per-file result CSVs are written.
- [Dev] Upgrade dependencies.


## [0.1.2] - 2026-04-07
### Added
- Linux arm64 build.
### Changed
- Release artifact names use upper case naming.
- [Dev] Upgrade dependencies.

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
