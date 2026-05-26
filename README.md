# PAM Analyzer Tech info

Native PySide6 desktop application for reviewing **Passive Acoustic Monitoring** field recordings: load a project, run BirdNET or Perch v2 detection, and review/annotate species detections in a sortable table.

Clean rewrite of the original NiceGUI-based PAM Analyzer with a layered, testable architecture.

## Quick start

```sh
uv sync
uv run poe run
```

Sub-commands:

- `uv run poe compile-ui` — regenerate `ui_*.py` from `.ui` files
- `uv run poe compile-qrc` — regenerate `*_rc.py` from `.qrc` files
- `uv run poe test` — run the test suite
- `uv run poe lint` / `uv run poe fmt` — ruff lint / format

The implementation plan lives in [`plan.md`](./plan.md).

## Layout

```
src/pam_analyzer/
├── domain/          # Pure entities + repository protocols (no Qt)
├── services/        # Application use-cases (no Qt)
├── infrastructure/  # TOML/CSV repos, BirdNET adapter, audio I/O
├── widgets/         # Reusable Qt widgets (multi-column-sort table, map)
├── ui/              # App-specific panels, dialogs, models, .ui files
├── workers/         # QThread-hosted background tasks
└── app/             # Composition root + entry point
```


# PAM Analyzer
Passive Acoustic Monitoring for Bird Species Detection
<!--TOC-->

- [PAM Analyzer Tech info](#pam-analyzer-tech-info)
  - [Quick start](#quick-start)
  - [Layout](#layout)
- [PAM Analyzer](#pam-analyzer)
  - [About](#about)
  - [Download](#download)
  - [Features](#features)
  - [Core Concepts](#core-concepts)
    - [Project](#project)
    - [Campaign](#campaign)
    - [ARU (Autonomous Recording Unit)](#aru-autonomous-recording-unit)
  - [Usage](#usage)
  - [Workflow](#workflow)
    - [Project Settings](#project-settings)
    - [Campaigns](#campaigns)
    - [Run bird species detection using BirdNET or Perch v2](#run-bird-species-detection-using-birdnet-or-perch-v2)
    - [Output files](#output-files)
    - [Examine Detections](#examine-detections)
  - [Keyboard shortcuts](#keyboard-shortcuts)
    - [Global](#global)
    - [Examine panel: detection row selected](#examine-panel-detection-row-selected)
  - [Changelog](#changelog)
  - [Acknowledgements](#acknowledgements)
  - [License](#license)

<!--TOC-->


## About
PAM Analyzer is a desktop application designed for processing Autonomous Recording Unit (ARU) field recordings to **detect bird species**. It covers the full workflow: importing SD card contents, running BirdNET species detection, reviewing and annotating detections, and exporting results. Data is organized into hierarchical projects and campaigns.

![Image](https://github.com/user-attachments/assets/93e71617-445c-47ed-ba3d-a3c279d3468c)


## Download
Pre-built binaries are available for the following platforms:
* macOS (Apple Silicon): [PAM-Analyzer-macos-arm64.zip](https://github.com/kenwer/pam-analyzer/releases/latest/download/PAM-Analyzer-macos-arm64.zip)
* Windows (x86_64): [PAM-Analyzer-windows-x86_64.zip](https://github.com/kenwer/pam-analyzer/releases/latest/download/PAM-Analyzer-windows-x86_64.zip)
* Linux (x86_64): [PAM-Analyzer-linux-x86_64.tar.gz](https://github.com/kenwer/pam-analyzer/releases/latest/download/PAM-Analyzer-linux-x86_64.tar.gz)
* Linux (arm64): [PAM-Analyzer-linux-arm64.tar.gz](https://github.com/kenwer/pam-analyzer/releases/latest/download/PAM-Analyzer-linux-arm64.tar.gz)

Note: On any supported OS you can also easily run PAM Analyzer from source using `uv run pam-analyzer`


## Features
* **Project & campaign management**: Organizes monitoring deployments into projects (`.pamproj`) and campaigns, each supporting independent species filters (via geographic coordinates or custom species lists).
* **SD card import**: Automatically detects ARU SD cards matching a configured volume name pattern and imports audio into a structured `campaign/ARU/week` directory layout with built-in deduplication and conflict resolution.
* **Multi-model analysis**: Run BirdNET or Google's Perch v2 from the same panel via a model selector. Both support per-campaign or batch-across-campaigns runs with a configurable confidence threshold; BirdNET adds segment overlap. Each model writes its own CSV per campaign so multiple model runs can coexist (see [Output files](#output-files)).
* **Detection review**: Provides a tabular interface for detections with multi-column sorting, filtering, inline annotation (verification status, species correction, comments), and integrated audio playback.
* **Data export**: Supports exporting filtered detections to CSV format and extracting annotated audio snippets with metadata embedded in filenames.


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
    ├── campaign.toml             # species filter configuration sidecar
    ├── species_list.txt          # species-list mode only: the campaign's species filter list
    ├── must_have_species.txt     # optional: extra species forced into a location-mode run
    └── {aru}/
```

`campaign.toml`, `species_list.txt`, and `must_have_species.txt` live in the campaign folder, beside the audio, so a campaign stays self-contained and can be moved, archived, or shared independently of the project file. The species-list files are present only when the corresponding filter option is used.

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


## Usage
Download and execute the binary for your platform from the [Download](#download) section. No installation is required.

Upon first launch, use `New Project` to initialize a project and configure the audio root and output paths. Use `File -> Save Project` (or `⌘S` / `Ctrl+S`) to persist this configuration as a `.pamproj` file. Then create at least one campaign in the `Campaigns` panel (audio import from SD cards is also handled there), run analysis in the `BirdNET` panel, and review detections in the `Examine` panel.


## Workflow
The application is organized into four panels that map to the steps of a typical PAM analysis workflow.

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
- **Import audio** from SD cards directly within a campaign's detail view. Click the import button to start monitoring for SD card volumes matching the configured name pattern. When a matching card is inserted, files are copied into the `campaign/ARU/week` directory structure with deduplication and conflict resolution.

Campaigns are discovered automatically from the audio recordings root: any subdirectory containing a `campaign.toml` sidecar is treated as a campaign.

### Run bird species detection using BirdNET or Perch v2
The app bundles offers two models to detect birds in the specified campaigns:

- **BirdNET**: TFLite model from BirdNET-Analyzer. Analyses 3-second segments at 48 kHz. Supports configurable segment overlap (0 - 2.9 s) and a per-week geographic species filter built from the campaign's coordinates.
- **Perch v2**: Google's SavedModel for bird vocalization classification. Analyses 5-second windows at 32 kHz with no overlap. Honors the campaign's location filter by post-filtering its open-world output against BirdNET's regional whitelist.

Common parameters include minimum confidence threshold and additional language columns for species names. Each detection is assigned a within-segment `Rank` (1 = highest-confidence species in that window), useful for deprioritising detections that are consistently outcompeted by other species in the same clip. Analyses can be run per-campaign or across all campaigns. See [Output files](#output-files) for what is written to disk.

### Output files
Analysis results are written to the **detections output path** set in Project Settings. When that path is left empty it defaults to `{audio_recordings_root}/{project}-detections/`. Each campaign gets its own subfolder there, with one detections CSV **per model run**:

```
{detections_output_path}/
└── {campaign}/
    ├── {campaign}-detections-birdnet.csv   # one row per BirdNET detection
    ├── {campaign}-detections-perch.csv     # one row per Perch v2 detection (only if Perch was run)
    ├── {campaign}-species-list.txt         # location mode only: the geographic species list BirdNET used
    └── {aru}/.../week_NN/
        └── *.BirdNET.results.csv           # BirdNET's own raw output, one file per recording
```

- **`{campaign}-detections-{model}.csv`** is the file you work with. Each model run writes its own file so BirdNET and Perch v2 outputs coexist for the same campaign. Every row carries a `Model` column identifying its source, plus the annotation columns (`Verified`, `Corrected_Species`, `Comment`). The Examine panel loads every model file it finds for the campaign and concatenates them; annotations are written back to the file the row came from. Legacy unsuffixed `{campaign}-detections.csv` files from earlier app versions are still loaded as a fallback.
- **`*.BirdNET.results.csv`** are BirdNET-Analyzer's raw per-recording outputs. The app parses them to build the BirdNET detections CSV and then leaves them on disk, so a re-run can reuse them. (Perch v2 doesn't produce intermediate per-recording files.)

Alongside the CSVs the app saves the species lists involved in the run as plain `.txt` files:

- **`{campaign}-species-list-input.txt`** (and, per week, `{campaign}-species-list-week-NN-input.txt`) is the list handed *to* BirdNET as input. It is written in species-list mode (a copy of your list) and in location mode when a must-have species list is merged on top of the geographic list. Plain location mode writes no input file, because BirdNET filters directly from the coordinates.
- **`{campaign}-species-list.txt`** (and, per week, `{campaign}-species-list-week-NN.txt`) is the geographic species list BirdNET *derived* from the campaign's coordinates, exported in location mode for reference. Perch v2 reuses these lists as its post-filter when run in location mode.

No combined, summary, or per-week CSVs are produced: the "All campaigns" view in the Examine panel concatenates the per-campaign CSVs in memory, so it always reflects the current per-campaign files.

### Examine Detections
Review and annotate results. Detection CSVs are loaded into a grid with multi-column sorting and filtering, inline annotation editing (Verified, Corrected_Species, Comment), and audio playback per detection. When more than one model has been run for a campaign, all detections appear in the same grid; sort or filter on the `Model` column to slice by source. Annotations are written back to the source CSV (the one the row was loaded from) automatically. Filtered results can be exported to a new CSV, and audio snippets for selected detections can be extracted with configurable padding.

When exporting audio snippets, annotation values are reflected in the output filenames:
- **Verified**: appends `_confirmed`, `_incorrect`, or `_uncertain` depending on the value.
- **Corrected_Species**: replaces the original species name in the filename with the corrected one (scientific name looked up from the project language) and appends `_corrected`.

Both suffixes can appear together, e.g. `…_corrected_confirmed.wav`.


## Keyboard shortcuts

### Global
| Windows/Linux | macOS | Action | Description |
| --- | --- | --- | --- |
| Ctrl+N        | ⌘N   | **New Project**        | Create a new empty in-memory project |
| Ctrl+O        | ⌘O   | **Open Project...**    | Open an existing `.pamproj` file |
| Ctrl+S        | ⌘S   | **Save Project**       | Save to the current file, or prompt if unsaved |
| Ctrl+Shift+S  | ⇧⌘S  | **Save Project As...** | Save to a new location |
| Ctrl+W        | ⌘W   | **Close Project**      | Close and return to the welcome screen |
| Ctrl+Q        | ⌘Q   | **Quit**               | Exit the application |

### Examine panel: detection row selected
These shortcuts work whenever a row is selected in the Examine panel and no cell editor is open.

| Key | Action |
| --- | --- |
| `Space` | Play / pause the current detection's audio |
| `J` | Jump to the detection start marker in the audio player |
| `B` | Seek to the beginning of the audio file |
| `T` | Set **Verified** to `true` |
| `F` | Set **Verified** to `false` |
| `U` | Set **Verified** to `uncertain` |
| `C` | Open the **Comment** field for text editing |
| `S` | Open the **Corrected Species** dropdown |

> **Tip:** While the Comment field or the Corrected Species dropdown is open, all single-key shortcuts are automatically suspended so you can type freely. Press `Escape` or `Enter` / `Return` to confirm and return to normal navigation.


## Changelog
The changelog can be found at the [CHANGELOG page](CHANGELOG.md).


## Acknowledgements

The author would like to thank the following projects:

* [BirdNET](https://github.com/birdnet-team/birdnet)
* [Perch 2.0](https://arxiv.org/pdf/2508.04665)
* [Qt](https://www.qt.io/) / [PySide6](https://doc.qt.io/qtforpython/)


## License
This project is licensed under the AGPL-3.0 license. See the LICENSE file for the full text.