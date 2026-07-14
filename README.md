# PAM Analyzer
Automated bird species detection from acoustic recordings.

<!--TOC-->

- [About](#about)
- [Download](#download)
- [Features](#features)
- [Usage](#usage)
- [Workflow](#workflow)
  - [Project Settings](#project-settings)
  - [Campaigns](#campaigns)
  - [Run bird species detection using BirdNET-2.4 or Perch-2.0](#run-bird-species-detection-using-birdnet-24-or-perch-20)
  - [Output files](#output-files)
  - [Examine Detections](#examine-detections)
- [Keyboard shortcuts](#keyboard-shortcuts)
  - [Global](#global)
  - [Examine panel: detection row selected](#examine-panel-detection-row-selected)
- [Core Concepts](#core-concepts)
  - [Project](#project)
  - [Campaign](#campaign)
  - [ARU (Autonomous Recording Unit)](#aru-autonomous-recording-unit)
- [Models](#models)
  - [BirdNET v2.4](#birdnet-v24)
  - [Perch v2](#perch-v2)
    - [Logit calibration](#logit-calibration)
  - [Choosing a model](#choosing-a-model)
- [Troubleshooting](#troubleshooting)
- [Changelog](#changelog)
- [Acknowledgements](#acknowledgements)
- [Citation](#citation)
- [License](#license)

<!--TOC-->


## About
PAM Analyzer is a cross-platform desktop application designed to help researchers performing Passive Acoustic Monitoring (PAM). It provides a complete workflow for processing Autonomous Recording Unit (ARU) field recordings: from importing SD card contents and running automated species detection (using BirdNET v2.4 or Google Perch v2), to reviewing, annotating, and exporting detections. The application organizes data into a hierarchical structure of projects and campaigns, making it easy to manage large-scale monitoring studies.

![Examine panel of the application interface](https://github.com/user-attachments/assets/613c7c67-abaf-4425-b2dc-15d194037eee)

## Download
Pre-built binaries are available for the following platforms:
* macOS (Apple Silicon): [PAM-Analyzer-macos-arm64.zip](https://github.com/kenwer/pam-analyzer/releases/latest/download/PAM-Analyzer-macos-arm64.zip)
* Windows (x86_64): [PAM-Analyzer-windows-x86_64.zip](https://github.com/kenwer/pam-analyzer/releases/latest/download/PAM-Analyzer-windows-x86_64.zip)
* Linux (x86_64): [PAM-Analyzer-linux-x86_64.tar.gz](https://github.com/kenwer/pam-analyzer/releases/latest/download/PAM-Analyzer-linux-x86_64.tar.gz)
* Linux (arm64): [PAM-Analyzer-linux-arm64.tar.gz](https://github.com/kenwer/pam-analyzer/releases/latest/download/PAM-Analyzer-linux-arm64.tar.gz)

Note: On any supported OS you can also easily run PAM Analyzer from source using `uv run poe run`


## Features
* **Project & campaign management**: Organizes monitoring deployments into projects (`.pamproj`) and campaigns, each supporting independent species filters (via geographic coordinates or custom species lists).
* **SD card import**: Automatically detects ARU SD cards matching a configured volume name pattern and imports audio into a structured `campaign/ARU/week` directory layout. Both AudioMoth and Wildlife Acoustics Song Meter Micro cards are supported, including Song Meter's `Data/` subfolder layout. WAV recordings are transcoded to FLAC (lossless, 16-bit PCM) on import to save disk space.
* **Multi-model analysis**: Run BirdNET-2.4 or Google's Perch-2.0 from the same panel via a model selector. Both support per-campaign or batch-across-campaigns runs with a configurable confidence threshold and segment overlap. Each model writes its own CSV per campaign so multiple model runs can coexist (see [Output files](#output-files)).
* **Detection review**: Provides a tabular interface for detections with multi-column sorting, filtering, inline annotation (verification status, species correction, comments), and integrated audio playback.
* **Data export**: Supports exporting filtered detections to CSV format and extracting annotated audio snippets with metadata embedded in filenames.


## Usage
Download and execute the binary for your platform from the [Download](#download) section.

Upon first launch, use `New Project` to initialize a project and configure the audio root and output paths. Then create at least one campaign in the `Campaigns` panel (audio import from SD cards is also handled there), run analysis in the `BirdNET` panel, and review detections in the `Examine` panel. More details are in the workflow section below.


## Workflow
The application is organized into four panels that map to the steps of a typical PAM analysis workflow.

### Project Settings
Configure a study in the project settings: 

- Set the **audio recordings root directory**: this is where the audio data from the ARUs gets imported to.
- the detections output path
- If needed adjust the **SD card volume name pattern**: A regular expression to match SD card volume names for your ARUs. The default matches both AudioMoth (`MSD-`) and Song Meter (`2MM`) cards; widen or narrow it to suit your devices.
- You can also set the **preferred species language** that is used when exporting audio snippets and for the species column in the examine data table.

After making project config changes you may want to save your project using `File -> Save Project` (or `⌘S` / `Ctrl+S`) to save this configuration as a `.pamproj` file.

### Campaigns
Create and manage the campaigns that belong to this project. The panel shows all discovered campaigns in a scrollable list. Clicking a campaign opens its settings in an inline form on the right. From here you can:

- **Create** a new campaign using the `+` button. Each campaign must be configured with a species filter:
  - **Location mode**: specify a lat/lon on a map or enter coordinates manually; BirdNET derives the species list from this location. Here you can also add species you want to have always included when feeding the detection models.
  - **Species list mode**: provide a `.txt` species list file, which is copied into the campaign folder alongside the audio.
- **Edit** species filter settings at any time.
- **Delete** a campaign via the trash icon on its list card, with an inline confirmation step.
- **Import audio** from SD cards directly within a campaign's detail view. Click the import button to start monitoring for SD card volumes matching the configured name pattern. When a matching card is inserted, files are imported into the `campaign/ARU/week` directory structure with deduplication and conflict resolution. WAV recordings are transcoded to FLAC (lossless, 16-bit PCM) to save disk space, and any GUANO metadata (timestamp, location, device) is carried across into the FLAC. The encode is verified against the source before a card is cleared, so a recording is never lost to a bad transcode; FLAC sources and the device's provenance file are copied through untouched. The device family is recognised from the card layout: AudioMoth keeps recordings and a `CONFIG.TXT` at the card root, while Song Meter keeps recordings under `Data/` and a `<serial>_Summary.txt` log at the root.

Campaigns are discovered automatically from the audio recordings root: any subdirectory containing a `campaign.toml` sidecar is treated as a campaign.

### Run bird species detection using BirdNET-2.4 or Perch-2.0
Pick a model from the dropdown and configure its parameters in the panel. See [Models](#models) for a side-by-side comparison of BirdNET-2.4 and Perch-2.0 and guidance on when to use each.

Common parameters include minimum confidence threshold and additional language columns for species names. Each detection is assigned a within-segment `Rank` (1 = highest-confidence species in that window), useful for deprioritising detections that are consistently outcompeted by other species in the same clip. Analyses can be run per-campaign or across all campaigns. See [Output files](#output-files) for what is written to disk.

### Output files
Analysis results are written to the **detections output path** set in Project Settings. When that path is left empty it defaults to `{audio_recordings_root}/{project}-detections/`. Each campaign gets its own subfolder there, with one detections CSV **per model run**:

```
{detections_output_path}/
└── {campaign}/
    ├── {campaign}-detections-BirdNET-2.4.csv  # one row per BirdNET detection
    ├── {campaign}-detections-Perch-2.0.csv    # one row per Perch v2 detection (only if Perch was run)
    ├── {campaign}-species-list.txt            # location mode only: the geographic species list BirdNET used
    └── {aru}/.../week_NN/
        └── *.BirdNET.results.csv              # BirdNET's own raw output, one file per recording
```

- **`{campaign}-detections-{model_key}.csv`** is the file you work with. The `{model_key}` suffix is the runner's identifier (`BirdNET-2.4` or `Perch-2.0`), so multiple model runs coexist for the same campaign. Every row carries a `Model` column identifying its source, plus the annotation columns (`Verified`, `Corrected_Species`, `Comment`). The Examine panel loads every model file it finds for the campaign and concatenates them; annotations are written back to the file the row came from.
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
An individual recording device, identified by its SD card volume name (e.g. `MSD-109` for AudioMoth, `2MM30692` for a Song Meter serial). Within a campaign folder, each ARU gets its own subfolder. Recordings are further organised into weekly subfolders (`week_08`) derived from the file timestamps.


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


## Models
PAM Analyzer ships two bird-detection models. Both run locally on CPU, write to the same per-detection CSV schema, and honor the campaign's species filter (location-mode or species-list mode). They can be run on the same campaign and their outputs coexist in separate files.

| | **BirdNET v2.4** | **Perch v2** |
|---|---|---|
| Backend | TFLite via the [`birdnet`](https://github.com/birdnet-team/birdnet) library | TensorFlow SavedModel via the same library |
| Audio window | 3 s | 5 s |
| Sample rate | 48 kHz | 32 kHz |
| Segment overlap | Configurable (0 to 2.9 s) | Configurable (0 to 4.9 s) |
| Classes | ~6500 species | 14,795 classes |
| Speed (Apple M4 Pro, CPU, ~4 h audio) | ~1050x real-time | ~77x real-time |
| Confidence units in CSV | Sigmoid probability (0-1) | Calibrated probability (0-1), see [Logit calibration](#logit-calibration) |

### BirdNET v2.4
A compact CNN for global birdsong classification. The runner uses the campaign's coordinates to derive a per-week regional species list, so the model only emits species that are plausible at that location and time of year. BirdNET is the fast first-pass model: a four-hour campaign runs in under a minute on a modern laptop. Its confidence scores are sigmoid probabilities and need no calibration.

### Perch v2
A conformer-based open-world bird vocalization classifier from Google. Perch analyzes 5 s windows at 32 kHz with configurable overlap (0 to 4.9 s), emits the top-5 species per window, and recognizes ~14,795 classes globally. It is more sensitive than BirdNET at the cost of being roughly 13x slower (on my CPU). Perch's added value lies in low-amplitude calls (distant, partially-occluded, or under-modeled species) that BirdNET misses.

In location mode the runner post-filters Perch's open-world output against the campaign's regional species list (derived from BirdNET's geographic filter), so Perch and BirdNET runs on the same campaign return comparably-scoped species sets.

#### Logit calibration
Perch's classification head emits raw logits, not probabilities. Pure silence sits around +4.5 and ambient noise (wind, distant traffic) sits higher still, so a naive sigmoid would mark every 5 s window as ~99% confident in something. The runner therefore applies a hardcoded offset before the sigmoid (`_PERCH_LOGIT_OFFSET`) that is currently set to 11.2. The that the probabilities written to the CSV are somewhat comparable (to BirdNET's units in the 0-1 range). This is not ideal and might change in the future.

The offset was tuned (empirically) by cross-comparison against BirdNET (also not ideal, because we're missing ground truth). `scripts/calibrate_perch_offset.py` analyzues pairs of BirdNET/Perch detection CSVs and generates per-offset statistics and graphs (raw-logit histogram, per-species histograms, BN-agreement curves).

### Choosing a model
- Run **BirdNET** as the default first pass over every campaign. It is fast and has a low false-positive rate.
- Add **Perch v2** when you suspect BirdNET is missing quiet or distant calls (e.g. for corvids and other low-pitched or sparse vocalizers), or when you want a second opinion on borderline detections. Perch's added detections live mostly in the 0.25 to 0.5 calibrated-confidence range, exactly where manual review is most useful.
- Run **both** on the same campaign when you have the time budget. The Examine panel concatenates per-model CSVs and exposes the `Model` column for sorting and filtering, so each detection is traceable to its source.


## Troubleshooting
The application writes a rotating debug log (`pam-analyzer.log`, capped at 1 MB with one backup) to the platform's standard log directory:

- **Windows**: `%LOCALAPPDATA%\PAM Analyzer\Logs\pam-analyzer.log`
- **macOS**: `~/Library/Logs/PAM Analyzer/pam-analyzer.log`
- **Linux**: `~/.local/state/PAM Analyzer/log/pam-analyzer.log`

The easiest way to find it is **Help > Open Log Folder** in the app, which opens the folder directly in your file browser.

On Windows, `%LOCALAPPDATA%` lives under a hidden `AppData` folder that File Explorer doesn't show by default, so browsing there manually is not straightforward. If you don't have access to the app's menu, paste the path above into File Explorer's address bar (not the search box) and press Enter; Explorer will expand `%LOCALAPPDATA%` and navigate straight there.


## Changelog
The changelog can be found at the [CHANGELOG page](CHANGELOG.md).


## Acknowledgements
The author would like to thank the following projects:

* [BirdNET](https://github.com/birdnet-team/birdnet)
* [Perch 2.0](https://arxiv.org/pdf/2508.04665)
* [Qt](https://www.qt.io/) / [PySide6](https://doc.qt.io/qtforpython)
* [Python](https://www.python.org)
* [Polars](https://pola.rs)
* [SciPy](https://scipy.org)
* [GUANO](https://github.com/riggsd/guano-py)
* [Mutagen](https://github.com/quodlibet/mutagen)
* [NumPy](https://numpy.org)
* [platformdirs](https://github.com/tox-dev/platformdirs)
* [soundfile](https://github.com/bastibe/python-soundfile)
* [psutil](https://github.com/giampaolo/psutil)
     

## Citation

If you use PAM Analyzer in your work, you can [cite](CITATION.cff) it:

```bibtex
@software{Werner_PAM_Analyzer_2026,
  author  = {Werner, Ken},
  title   = {PAM Analyzer},
  url     = {https://github.com/kenwer/pam-analyzer},
  version = {0.4.0},
  year    = {2026}
}
```

## License
This project is licensed under the AGPL-3.0 license. See the LICENSE file for the full text.
