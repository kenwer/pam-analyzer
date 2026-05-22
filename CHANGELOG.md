# Changelog

## unreleased
### Added
- Initial release with MVP functionality.
- Project management: create, load, save, and reopen recent projects (`.pamproj` format).
- Campaign management panel to organise monitoring deployments, including location selection via an interactive map picker.
- TODO: Species list support: load custom species list files when creating campaigns, with optional localized species names (multiple languages).
- TODO: Import panel to load audio files from SD cards and local directories, with import progress log.
- TODO: Audio analysis powered by BirdNET-Analyzer with configurable minimum confidence threshold and per-ARU detection limit.
- Examine panel with data table displaying detections, supporting filters, column visibility toggles, auto-sizing, and exporting visible columns and rows.
- Integrated audio player in the file column for quick playback of detected snippets.
- TODO: SD card watch hints to guide correct card mounting.

### Changed
- Analysis now writes only two kinds of CSV: BirdNET's own per-recording
  `*.BirdNET.results.csv` and one `<campaign>-detections.csv` per campaign.
  The project-wide combined CSV, the per-aru / all-aru / per-campaign / all-campaign
  summary CSVs, and the per-week detection and summary CSVs are no longer
  produced. The "All campaigns" view in the Examine panel concatenates the
  per-campaign CSVs in memory, so it can no longer disagree with its sources.
  Existing CSVs from earlier runs are left untouched on disk.
