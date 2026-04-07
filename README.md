# PAM Analyzer
Passive Acoustic Monitoring for Bird Species Detection
<!--TOC-->

- [About](#about)
- [Download](#download)
- [Features](#features)
- [Core Concepts](#core-concepts)
  - [Project](#project)
  - [Campaign](#campaign)
  - [ARU (Autonomous Recording Unit)](#aru-autonomous-recording-unit)
- [Workflow](#workflow)
  - [Project Settings](#project-settings)
  - [Campaigns](#campaigns)
  - [Import Audio](#import-audio)
  - [BirdNET](#birdnet)
  - [Examine Detections](#examine-detections)
- [Usage](#usage)
  - [Keyboard shortcuts](#keyboard-shortcuts)
- [Changelog](#changelog)
- [License](#license)

<!--TOC-->

## About
PAM Analyzer is a desktop application designed for processing Autonomous Recording Unit (ARU) field recordings to **detect bird species**. It covers the full workflow: importing SD card contents, running BirdNET species detection, reviewing and annotating detections, and exporting results. Data is organized into hierarchical projects and campaigns.

## Download
Pre-built binaries are available for the following platforms:
* macOS (Apple Silicon): [PAM-Analyzer-macos-arm64.zip](https://github.com/kenwer/pam-analyzer/releases/latest/download/PAM-Analyzer-macos-arm64.zip)
* Windows (x86_64): [PAM-Analyzer-windows-x86_64.zip](https://github.com/kenwer/pam-analyzer/releases/latest/download/PAM-Analyzer-windows-x86_64.zip)
* Linux (x86_64): [PAM-Analyzer-linux-x86_64.tar.gz](https://github.com/kenwer/pam-analyzer/releases/latest/download/PAM-Analyzer-linux-x86_64.tar.gz)
* Linux (arm64): [PAM-Analyzer-linux-arm64.tar.gz](https://github.com/kenwer/pam-analyzer/releases/latest/download/PAM-Analyzer-linux-arm64.tar.gz)

Note: On any supported OS you can also easily run PAM Analyzer from source using `uv run pam-analyzer`

## Features
* **Project & campaign management**: organise monitoring deployments into projects (`.pamproj`) and campaigns, each with its own species filter (location-based or custom species list).
* **SD card import**: detects inserted ARU SD cards matching the configured volume name pattern and copies audio into a `campaign/ARU/week` directory layout, with deduplication and conflict resolution.
* **BirdNET analysis**: run BirdNET-Analyzer per campaign or across all campaigns, with configurable confidence threshold and overlap. Produces per-ARU and summary CSVs.
* **Detection review**: tabular view of detections with sorting, filtering, inline annotation (verified status, corrected species, comments), and audio playback per detection.
* **Export**: export filtered detections to CSV and extract audio snippets with annotation values embedded in output filenames.

## Core Concepts
### Project
The largest organisational unit. A project represents a study or monitoring programme, e.g. "Bird survey of Lake Constance wetlands 2026". It holds project-wide configuration (audio recordings root path, ARU SD card volume name pattern, preferred species name language) and groups all campaigns belonging to that study. A project maps to one `.pamproj` file on disk.

> **Note:** Species filter settings (lat/lon location or species list) are campaign-scoped, not project-scoped.

### Campaign
A campaign is a time-bounded field deployment during which a set of ARUs were active. The campaign name is chosen by the researcher and typically encodes start date, end date, and study area, e.g. `Campaign-20260114-20260216-Federsee`. On the file system each campaign lives in its own subdirectory under the audio recordings root and carries a `campaign.toml` sidecar that stores its species filter configuration. Because the sidecar travels with the audio, campaigns are self-contained and can be moved, archived, or shared independently of the project file. Campaigns are discovered automatically from the audio root. Individual ARUs within a campaign may be deployed at distinct locations within the study area.

```toml
species_filter_mode = "location"  # "location" or "list"
latitude = 47.94
longitude = 9.32
species_list_path = ""  # relative path to .txt, empty when using location mode
```

The combination of **campaign + ARU device ID** uniquely identifies a recording set within a project while the same physical ARU redeployed at a different time usually belongs to a different campaign.

### ARU (Autonomous Recording Unit)
An individual recording device, identified by its SD card volume name (e.g. `MSD-109`). Within a campaign folder, each ARU gets its own subfolder. Recordings are further organised into weekly subfolders (`week_08`) derived from the file timestamps.


After setting up a project and importing ARU SD cards, the resulting directory structure looks like this:
```
{audio_recordings_root_path}/
└── {campaign}/
    ├── campaign.toml
    └── {aru}/
```

Example:
```
~/Studies/2026-SW-Germany-PAM-Project/
├── Campaign-20260114-20260216-Federsee/
│   ├── campaign.toml
│   ├── MSD-109/
│   │   ├── week_02/
│   │   ├── week_03/
│   │   ├── week_04/
│   │   ├── week_05/
│   │   └── week_06/
│   └── MSD-110/
│       ├── week_02/
│       ├── week_03/
│       ├── week_04/
│       ├── week_05/
│       ├── week_06/
│       └── week_07/
└── Campaign-20260317-20260328-Lake-Constance/
    ├── campaign.toml
    ├── MSD-109/
    │   ├── week_11/
    │   └── week_12/
    └── MSD-110/
        ├── week_11/
        └── week_12/
```


## Workflow
The application is organized into five panels that map to the steps of a typical PAM analysis workflow.

### Project Settings
Configure a study. Set the audio recordings root directory, the SD card volume name pattern (regex), the detections output path, and the preferred species language for labels. Settings persist automatically to the `.pamproj` file.

### Campaigns
Create and manage the campaigns that belong to a project. The panel shows all discovered campaigns in a scrollable list; clicking a campaign opens its settings in an inline form on the right. From here you can:

- **Create** a new campaign using the `+` button. Each campaign must be configured with a species filter:
  - **Location mode**: specify a lat/lon on a map or enter coordinates manually; BirdNET derives the species list from this location.
  - **Species list mode**: provide a `.txt` species list file, which is copied into the campaign folder alongside the audio.
- **Rename** a campaign by editing its name in the form and saving.
- **Edit** species filter settings at any time.
- **Delete** a campaign via the trash icon on its list card, with an inline confirmation step.

Campaigns are discovered automatically from the audio recordings root: any subdirectory containing a `campaign.toml` sidecar is treated as a campaign.

### Import Audio
Import audio from SD cards into a campaign. Select the target campaign, then start monitoring for SD card volumes matching the configured name pattern. When a matching card is inserted, files are copied into the `campaign/ARU/week` directory structure with deduplication and conflict resolution.

### BirdNET
Run bird species detection using BirdNET-Analyzer. Configurable parameters include minimum confidence threshold, segment overlap, and additional language columns for species names. Each 3-second detection is assigned a within-segment `Rank` (1 = highest-confidence species in that window), useful for deprioritising detections that are consistently outcompeted by other species in the same clip. Analyses can be run per-campaign or across all campaigns, producing per-campaign detection CSVs (grouped by ARU and week), per-ARU and all-ARUs summary CSVs, and project-level rollups when running all campaigns at once.

### Examine Detections
Review and annotate results. Detection CSVs are loaded into a grid with multi-column sorting and filtering, inline annotation editing (Verified, Corrected_Species, Comment), and audio playback per detection. Annotations are written back to the source CSVs automatically. Filtered results can be exported to a new CSV, and audio snippets for selected detections can be extracted with configurable padding.

When exporting audio snippets, annotation values are reflected in the output filenames:
- **Verified**: appends `_confirmed`, `_incorrect`, or `_uncertain` depending on the value.
- **Corrected_Species**: replaces the original species name in the filename with the corrected one (scientific name looked up from the project language) and appends `_corrected`.

Both suffixes can appear together, e.g. `…_corrected_confirmed.wav`.


## Usage
Download and execute the binary for your platform from the [Download](#download) section. No installation is required.

Upon first launch, use `New Project` to initialize a project and configure the audio root and output paths. Use `File -> Save Project` (or `⌘S` / `Ctrl+S`) to persist this configuration as a `.pamproj` file. Then create at least one campaign in the `Campaigns` panel, import audio from SD cards in the `Import` panel, run analysis in the `BirdNET` panel, and review detections in the `Examine` panel.


### Keyboard shortcuts
| Windows/Linux | macOS | Action | Description |
| --- | --- | --- | --- |
| Ctrl+N        | ⌘N   | **New Project**         | Create a new empty in-memory project |
| Ctrl+O        | ⌘O   | **Open Project...**     | Open an existing `.pamproj` file |
| Ctrl+S        | ⌘S   | **Save Project**        | Save to the current file, or prompt if unsaved |
| Ctrl+Shift+S  | ⇧⌘S  | **Save Project As...**  | Save to a new location |
| Ctrl+W        | ⌘W   | **Close Project**       | Close the current project and return to the welcome screen |
| Ctrl+Q        | ⌘Q   | **Quit**                | Exit the application |

## Changelog
The changelog can be found at the [CHANGELOG page](CHANGELOG.md).

## License
This project is licensed under the AGPL-3.0 license. See the LICENSE file for the full text.