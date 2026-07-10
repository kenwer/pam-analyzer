# Changelog

## [Unreleased]
### Added
- Add support importing audio from folders via drag&drop at the campaign's detail panel.
- Add context menu entries to sort the campaign list, and to open campaign folders in the file manage.
### Changed
- File listing and size checks now run in parallel instead of one at a time. This mainly helps on network shares (e.g. SMB), where each check is a network round trip. Goal is to speed up scanning the audio folder when opening a project and after ARU data imports. 

## [0.3.1] - 2026-07-09
### Changed
- [Dev] Upgrade dependencies.
### Added
- Add more logging and a "Help > Open Log Folder" menu action that opens the app's log directory directly in the file browser, so Windows users no longer need to navigate to the hidden `%LOCALAPPDATA%` folder manually.

## [0.3.0] - 2026-06-25
### Added
- Add support for importing audio from Wildlife Acoustics Song Meter Micro devices:
  - Song Meter cards are detected alongside AudioMoth cards: recordings live under `Data/` and the card carries a `<serial>_Summary.txt` deployment log at its root.
  - The default SD card volume name pattern now matches both AudioMoth (`MSD-`) and Song Meter (`2MM`) cards.
  - The `<serial>_Summary.txt` log is copied through untouched, the same way AudioMoth's `CONFIG.TXT` is.
### Fixed
- Editing the SD card name regex in project settings no longer loses keyboard focus after each character, so it can be typed in one go.

## [0.2.0] - 2026-06-23
### Added
- Add support for FLAC audio:
  - WAV recordings are transcoded to FLAC on import (lossless, 16-bit PCM) to save disk space.
  - FLAC files already on the SD card are imported unchanged.
  - Analysis, playback, spectrogram, and snippet export all work with FLAC.
  - GUANO metadata (timestamp, location, device) gets re-embedded into the FLAC as a 'GUANO' Vorbis comment.
### Changed
- [Dev] Release script also handles uv.lock.
### Fixed
- Perch analysis no longer contacts Kaggle on each run: bundled builds resolve the model version from the bundled cache, so Perch runs fully offline and an upstream version bump can no longer trigger a re-download.

## [0.1.2] - 2026-06-19
### Changed
- [Dev] Upgrade dependencies.
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
