# Changelog

## [Unreleased]
### Added
- Better date and time filters for the examine panel.
- New "Is one of" filter for Campaign, ARU, Species, Model, Verified, and Corrected_Species.

## [0.5.0] - 2026-07-17
### Added
- File > Open Legacy Project File... migrates a legacy `.pamproj`.
### Changed
- Projects are now self-contained folders. The `pam-analyzer.toml` file inside the project folder stores the settings. The audio root and detections output path settings are gone. New Project and Open Project pick a folder instead of a file.
- Detection CSVs now live inside each campaign folder as `detections-{model}.csv`, with File paths relative to the campaign folder.
- Location-mode species lists are written as `applied-species-list.txt` inside the campaign folder.
- Project settings save automatically on every change. The Save and Save As menu items have been removed.
- Opening a legacy `.pamproj` project offers a migration that moves the detection CSVs into their campaign folders and keeps the old file as `.bak`.
### Fixed
- SD card import no longer aborts when the card contains an inaccessible OS-generated directory, such as macOS's `.Spotlight-V100`.

## [0.4.1] - 2026-07-14
### Fixed
- [Dev] Pin the Perch v2 model to Kaggle version 1, so builds, tests, and the app always use the exact same checkpoint.

## [0.4.0] - 2026-07-14
### Added
- The Campaigns panel now shows an overview of all campaigns and their ARUs when no campaign is selected.
- Add support importing audio from folders via drag&drop at the campaign's detail panel.
- Add context menu entries to the campaign list to allow to
  - create a new campaign
  - sort the campaign list
  - open a campaign folder in the file manager
- [Dev] Add a pre-push git hook that runs the fast test suite, plus `poe install-hooks` to enable it.
### Changed
- Recent folder paths on the welcome screen now show `~` instead of the full home directory path.
- The recent projects list on the welcome screen now grows with the window instead of staying at a fixed size.
- File listing and size checks now run in parallel instead of one at a time. This mainly helps on network shares (e.g. SMB), where each check is a network round trip. Goal is to speed up scanning the audio folder when opening a project and after ARU data imports. 
- Faster WAV imports to network shares (e.g. SMB) as the transcode now runs local and sends only the finished FLAC.
- [Dev] Consolidate the detection schema (columns, CSV serialization, filename pattern) into `domain/detection_schema.py`. All readers and writers derive from it.
- [Dev] Analysis runners now build domain `Detection` objects and serialize them through the detection schema, removing the last hand-built CSV row writer. Freshly written CSVs use the same number formatting as annotation saves (e.g. `0.85` instead of `0.8500`).
- [Dev] Name the week `-1` sentinel `WEEK_YEAR_ROUND` in the domain and use it everywhere it appears (geo filter, CSV Week column, audio inventory). It mirrors the birdnet geo API, where week -1 requests the year-round species list.
- [Dev] Move `AppSettings` from `app/` to `ui/` and enforce the ARCHITECTURE.md layer import rules with a new test case.
- [Dev] Let CI run the full test suite, including the slow Perch tests.
- [Dev] Upgrade dependencies.
- Drop Intel Mac (macOS x86_64) support. Google stopped shipping macOS x86_64 TensorFlow wheels after 2.16, and Perch v2 requires 2.17+.
- [Dev] Upgrade Python from 3.12 to 3.13 (the latest that are supported by birdnet and TensorFlow).
- [Dev] Guard CI against hanging tests and skip the Perch cancellation test.
- [Dev] CI runs the fast tests and the slow model tests as separate pytest invocations.
- [Dev] Perch runner tests analyze a 60s audio like real AudioMoth recordings.
### Fixed
- [Dev] Re-enable the macOS (arm64) CI build.
- Detections CSV discovery no longer misbehaves for campaign names containing glob characters such as `[`, `]`, `*`, or `?`.
- Saving detection edits (Verified, Corrected Species, Comment) now writes the CSV atomically via a temp file. A crash or power loss during save can no longer truncate the file and lose annotations.

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
