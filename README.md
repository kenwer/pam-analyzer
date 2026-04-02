# PAM Analyzer: Passive Acoustic Monitoring for Bird Species Detection
<!--TOC-->

- [About](#about)
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
- [Installation](#installation)
- [Usage](#usage)
  - [Keyboard shortcuts](#keyboard-shortcuts)
- [Changelog](#changelog)
- [License](#license)

<!--TOC-->


## About
PAM Analyzer is a desktop application for **bird species detection** from field recordings from Autonomous Recording Units (ARUs). It covers the workflow from importing ARU-SD card contents through BirdNET species detection to annotation and export, organized around the concept of projects and campaigns.

## Core Concepts
### Project
The largest organisational unit. A project represents a study or monitoring programme, e.g. "Bird survey of Lake Constance wetlands 2026". It holds project-wide configuration (audio recordings root path, ARU SD card name pattern to identify matching ARU devices, the language you want the species names encoded in) and groups all campaigns belonging to that study. A project maps to one `.pamproj` file on disk.
Note: The BirdNET species filter settings (lat/lon location or species list) are campaign-scoped, not project-scoped.

### Campaign
A campaign is a time-bounded field deployment, a period during which a set of ARUs were active in the field. The campaign name is freely chosen by the researcher when importing ARU SD cards and might consist of several components like a start date, an end date, the study area, e.g. `Campaign-20260114-20260216-Federsee`. On the file system each campaign lives in its own subdirectory under the audio recordings root and carries a `campaign.toml` sidecar that stores its species filter configuration. Because the sidecar travels with the audio, campaigns are self-contained and can be moved, archived, or shared independently of the project file. Campaigns are discovered automatically. Individual ARUs within the same campaign may be spread across many specific spots within that area.

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
  - either location mode where you pick a lat/lon picked on a map (or enter manually) that BirdNET uses to create a species list from
  - or species list mode where you provide a species list `.txt` file that is copied into the campaign folder (keeping it self-contained alongside the audio).
- **Rename** a campaign by editing its name in the form and saving.
- **Edit** species filter settings at any time.
- **Delete** a campaign via the trash icon on its list card, with an inline confirmation step.

Campaigns are discovered automatically from the audio recordings root: any subdirectory containing a `campaign.toml` sidecar is treated as a campaign.

### Import Audio
Import audio files from SD cards into a campaign. Select the target campaign from the dropdown, then start watching for SD card volumes matching the configured name pattern. Files are copied into the `campaign/ARU/week` directory structure automatically when a card is inserted, with deduplication, conflict resolution, and real-time progress tracking.

### BirdNET
Run bird species detection using BirdNET-Analyzer. Configurable parameters include minimum confidence threshold, segment overlap, and additional language columns for species names. Each 3-second detection is assigned a within-segment `Rank` (1 = highest-confidence species in that window), useful for deprioritising detections that are consistently outcompeted by other species in the same clip. Analyses can be run per-campaign or across all campaigns, producing per-campaign detection CSVs (grouped by ARU and week), per-ARU and all-ARUs summary CSVs, and project-level rollups when running all campaigns at once.

### Examine Detections
Review and annotate results. Detection CSVs are loaded into an interactive grid with multi-column sorting and filtering, inline annotation editing (Verified, Corrected_Species, Comment), and one-click audio playback for individual detections. Annotations are written back to the source CSVs automatically. Filtered results can be exported to a new CSV, and audio snippets for selected detections can be extracted with configurable padding.

When exporting audio snippets, annotation values are reflected in the output filenames:
- **Verified**: appends `_confirmed`, `_incorrect`, or `_uncertain` depending on the value.
- **Corrected_Species**: replaces the original species name in the filename with the corrected one (scientific name looked up from the project language) and appends `_corrected`.

Both suffixes can appear together, e.g. `…_corrected_confirmed.wav`.


## Installation
TODO

## Usage
Start the application using `uv`:
```bash
uv run pam-analyzer
```

On first launch, click **New Project** to create an in-memory project and configure the audio root and output paths. Use **File → Save Project** (or `⌘S` / `Ctrl+S`) to persist it as a `.pamproj` file. Then create campaigns in the Campaigns panel, copy audio from SD cards in the Import panel, run analysis in the BirdNET panel, and review detections in the Examine panel.

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