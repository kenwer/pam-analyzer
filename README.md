# PAM Analyzer
Automated bird species detection from acoustic recordings.

<!--TOC-->

- [About](#about)
- [Download](#download)
- [Features](#features)
- [Usage](#usage)
  - [Migrating legacy projects](#migrating-legacy-projects)
- [Workflow](#workflow)
  - [Project Settings](#project-settings)
  - [Campaigns](#campaigns)
  - [Run bird species detection](#run-bird-species-detection)
  - [Output files](#output-files)
  - [Examine Detections](#examine-detections)
- [Keyboard shortcuts](#keyboard-shortcuts)
- [Core Concepts](#core-concepts)
  - [Project](#project)
  - [Campaign](#campaign)
  - [ARU (Autonomous Recording Unit)](#aru-autonomous-recording-unit)
- [Models](#models)
  - [Species names](#species-names)
  - [Perch confidence](#perch-confidence)
  - [Region filtering](#region-filtering)
- [Troubleshooting](#troubleshooting)
  - [Changing the log level](#changing-the-log-level)
    - [Overriding the level from the environment](#overriding-the-level-from-the-environment)
- [Changelog](#changelog)
- [Acknowledgements](#acknowledgements)
- [Citation](#citation)
- [License](#license)

<!--TOC-->


## About
PAM Analyzer is a cross-platform desktop application designed to help researchers performing Passive Acoustic Monitoring (PAM). It provides a complete workflow for processing Autonomous Recording Unit (ARU) field recordings: from importing SD card contents and running automated species detection (using BirdNET or Perch), to reviewing, annotating, and exporting detections. The application organizes recordings and detections into projects, each containing one or more campaigns.

![Examine panel of the application interface](https://github.com/user-attachments/assets/613c7c67-abaf-4425-b2dc-15d194037eee)

## Download
Pre-built binaries are available for the following platforms:
* macOS (Apple Silicon): [PAM-Analyzer-macos-arm64.zip](https://github.com/kenwer/pam-analyzer/releases/latest/download/PAM-Analyzer-macos-arm64.zip)
* Windows (x86_64): [PAM-Analyzer-windows-x86_64.zip](https://github.com/kenwer/pam-analyzer/releases/latest/download/PAM-Analyzer-windows-x86_64.zip)
* Linux (x86_64): [PAM-Analyzer-linux-x86_64.tar.gz](https://github.com/kenwer/pam-analyzer/releases/latest/download/PAM-Analyzer-linux-x86_64.tar.gz)
* Linux (arm64): [PAM-Analyzer-linux-arm64.tar.gz](https://github.com/kenwer/pam-analyzer/releases/latest/download/PAM-Analyzer-linux-arm64.tar.gz)

Note: On any supported OS you can also easily run PAM Analyzer from source using `uv run poe run`


## Features
* **Project & campaign management**: Organizes monitoring deployments into self-contained project folders. A project folder contains one or more campaigns, each supporting independent species filters (via geographic coordinates and/or custom species lists). Projects and campaigns store no absolute paths, so they are relocatable.
* **SD card import**: As soon as the import mode for a Campaign is activated, the application detects ARU SD cards matching a configured volume name pattern and imports audio into the Project. Both AudioMoth and Wildlife Acoustics Song Meter Micro cards are supported. On import, WAV recordings are transcoded on-the-fly to FLAC to save disk space. Once the contents of an SD card have been imported, the card gets unmounted automatically.
* **Species detection**: Run BirdNET-2.4, BirdNET-3.0-preview3.1 or Perch-2.0 on the data of a Campaign or the whole Project. Each model writes its own CSV per campaign, so output from various models can coexist and be reviewed together.
* **Detection review**: You can examine all detections in one large data table with multi-column sorting, filtering, inline annotation (verification status, species correction, comments), and integrated audio playback.
* **Data export**: Allows to export CSV and creating annotated audio snippets for the filtered detections.


## Usage
Download the archive for your platform from the [Download](#download) section and unpack it.

Upon first launch, use `New Project` and pick (or create) the folder that will hold your data such as recordings and detection CSVs. The app marks it as a project by writing a `pam-analyzer.toml` settings file into it. Then create at least one campaign in the `Campaigns` panel (audio import from SD cards is also handled there), run species detection in the `BirdNET` panel, and review detections in the `Examine` panel. More details are in the workflow section below.

### Migrating legacy projects
If you used an older version of PAM Analyzer that stored projects as `.pamproj` files, use **File > Open Legacy Project File…** to select the `.pamproj` file. The app will offer to migrate it: detection CSVs are moved into their campaign folders, and the old file is kept as `.bak`. If the audio recordings folder moved since the project was created, a folder picker lets you relocate it. When opening a project folder that contains a `.pamproj` file, migration is offered automatically.


## Workflow
The application is organized into four panels that map to the steps of a typical PAM analysis workflow.

### Project Settings
Configure a study in the project settings.

- **SD card volume name pattern**: A regular expression to match SD card volume names for your ARUs during import. The default matches both AudioMoth (`MSD-`) and Song Meter (`2MM`) cards. If needed widen or narrow it to suit your devices/naming convention.
- Model settings:
  - **Min confidence**: the minimum detection score (0.10 to 1.00) a species prediction must reach to appear in the output CSV. Lower values result in more detections but increasingly more false positives.
  - **Overlap**: how much consecutive analysis windows overlap, in seconds (0 to 2.5 s). Overlap might help to catch vocalizations that would otherwise be split across a window boundary, at the cost of longer analysis time and/or duplicate detections.
- Species languages:
  - **Main** sets the preferred language for the Species column in all CSV outputs and for exported audio snippets
  - **Extra** adds one additional common-name column per checked language to the examine data table.
  - Both controls offer the 18 languages every shipped model can produce: `cs`, `da`, `de`, `en_us`, `es`, `fi`, `fr`, `ja`, `nl`, `no`, `pl`, `pt`, `ru`, `sk`, `sv`, `tr`, `uk`, `zh`.

All settings are saved automatically to the `pam-analyzer.toml` file inside the project folder.

### Campaigns
Create and manage the Campaigns that belong to this project. The panel on the left shows a list of all Campaigns for this Project. For newly created Projects it's empty.

- **Create a new campaign** using the `+` button. Each campaign must be configured with a species filter:
  - **Location mode**: specify a lat/lon on a map or enter coordinates manually. BirdNET derives the species list from this location. Here you can also add species you want to have always included when feeding the detection model. Names written under the older BirdNET v2.4 taxonomy are accepted, so a must-have entry saying `Accipiter gentilis` still matches the `Astur gentilis` the model emits. See [Species names](#species-names).
  - **Species list mode**: provide a `.txt` species list file, which is copied into the campaign folder alongside the audio.
- **Specifying species** (species list mode and the location-mode must-have list use the same input and format): type or paste species names directly into the text box, one per line, or drag-and-drop a `.txt` file onto it (or use the import button to browse for one). Either way, the file's contents are loaded into the box.
  - Example, one entry per line:

    ```text
    # This is comment
    Turdus merula
    Parus major_Great Tit # another comment
    Fringilla coelebs_Buchfink
    Corvus corax
    ```

    Each line is a scientific (Latin binomial) name, e.g. `Turdus merula`. BirdNET-style species list in `Scientific name_Common name` form also work, since everything from the underscore onward is ignored, regardless of which language the common name is in, so `Parus major_Great Tit` (English) and `Fringilla coelebs_Buchfink` (German) are parsed the same way as their bare scientific names. A `#` starts a comment that runs to the end of the line, whether on its own line or trailing a species name. The app uses this to mark must-have entries when it writes `applied-species-list*.txt`, so that file can be pasted straight back into the species list or must-have box. Blank lines are ignored.
- **When no Campaign is selected**, a project-wide overview is shown on the right, showing the total campaigns, ARUs, recordings, disk usage, and date range.
- Import audio from SD cards from the campaign's detail view using the **Start SD import** button. This starts monitoring for SD card volumes matching the configured name pattern. When a matching card is inserted, files are imported into the project. WAV recordings are transcoded on-the-fly to FLAC (lossless, 16-bit PCM) to save disk space, and any GUANO metadata (timestamp, location, device) stored in the FLAC as well. The device family is recognised from the card layout: AudioMoth keeps recordings and a `CONFIG.TXT` at the card root, while Song Meter keeps recordings under `Data/` and a `<serial>_Summary.txt` log at the root.

When the application loads a Project, Campaigns are discovered automatically from the project folder: any subdirectory containing a `campaign.toml` sidecar is treated as a campaign. This is especially handy as Campaign folders are relocatable.

### Run bird species detection
Analyses can be run per-campaign or across all campaigns. Choose the [Model](#models) and which campaign(s) to run against. The min confidence, overlap, and species language settings are taken from the [Project Settings](#project-settings). The output is one or more CSV files that contain the (bird) detections. One CSV per model and Campaign. Keep that in mind (especially when you already annotated your data) that subsequent runs of the very same model will create new ouptus CSV(s) and there overwrite the output.

Each detection is assigned a within-segment `Rank` (1 = highest-confidence species in that window), useful for deprioritising detections that are consistently outcompeted by other species in the same clip. See [Output files](#output-files) for what is written to disk.

Recordings are analysed in fixed windows, 3 s for BirdNET and 5 s for Perch, and every window is scored on its own. Inside each window a detection has to survive four steps:
1. The model scores every species in its label set.
2. Species below the **Min confidence** setting drop out.
3. Of those left, only the 50 highest-scoring are kept.
   - The `top_k=50` cap is deliberately generous. Even at 0.10, the lowest selectable confidence, no window held more than 10 species in testing, so it never applies in practice.
4. The campaign's species filter drops anything not expected. Each campaign uses one of two modes (see [Campaigns](#campaigns)):
   - **Species list mode**: only the species on the list you provide.
   - **Location mode**: only the species the geographic model expects at the campaign's coordinates for that week, plus any optional must-have species names you added. See [Region filtering](#region-filtering).

Step 3 runs before step 4, so a species crowded out by the cap is gone before the species filter can ask for it.

### Output files
Analysis results are written directly into each campaign folder, next to the audio, with one detections CSV **per model run**:

```
{project}/
└── {campaign}/
    ├── detections-BirdNET-2.4.csv             # BirdNET-2.4 detections
    ├── detections-BirdNET-3.0-preview3.1.csv  # BirdNET-3.0 preview detections
    ├── detections-Perch-2.0.csv               # Perch-2.0 detections
    ├── applied-species-list-week-NN.txt       # location mode: geo model species list for that week plus the must-have species
    ├── must_have_species.txt                  # location mode input: the configured must-have species (if any)
    └── campaign.toml                          # holds the configuration of this campaign
```

- For each campaign **`detections-{model_key}.csv`** is the file where the species detections are stored. The `{model_key}` suffix identifies the model (`BirdNET-2.4`, `BirdNET-3.0-preview3.1`, `Perch-2.0`). Every row carries a `Model` column identifying its source, plus the annotation columns (`Verified`, `Corrected_Species`, `Comment`). The Examine panel loads every model file it finds for the campaign and concatenates them. Annotations are written back to the file the row came from. The `File` column is stored relative to the campaign folder, so renaming or moving a campaign never breaks its CSVs.
- **`applied-species-list*.txt`** is the merged list (geographic list plus an optional must-have species list, the latter tagged `# must-have`) the run actually filtered against, exported in location mode for reference. One file is written per `week_NN` folder, since the geographic list differs from week to week.

### Examine Detections
Review and annotate results. The detections are shown on the Examine panel. The data table allows to play and filter the detections. This is also the place where you annotate and export your (filtered) detections. When a campaign has CSVs from more than one model, all detections appear in the same grid but you can sort or filter on the `Model` column to slice by source.

- **Column filters**: Click a column header to open the filter menu. Text columns support `contains`, `starts with`, and `ends with` operators. The `Campaign`, `ARU`, `Species`, `Model`, `Verified`, and `Corrected_Species` columns also support an "Is one of" operator for multi-value selection. Date and time columns have dedicated date range and time range filters. Pressing `Enter` in a filter input applies the filter immediately and moves focus to the table.
- **Max per ARU/Species**: This control caps how many detections to keep for each ARU and species pair, keeping the highest-confidence ones (set it to `All` to disable). The cap is applied *after* the per-column filters, so it thins only the rows that already passed those filters. For example, setting the cap to 1 shows the single best detection per ARU and species.
- **Playback padding**: The `⚙` button lets you configure how many seconds of audio to play before and after each detection, helpful for hearing context around the vocalization. These values are saved per-project.
- **Annotations**: Verified, Corrected_Species, and Comment edits are written back to the source CSV automatically.
- **Export**: The `⬇` button offers CSV export of the currently filtered rows and audio snippet extraction with configurable padding.

Exported audio snippets use the following file name scheme:
```
Campaign__ARU__Species__YYYYMMDD_HHMMSS__start-end__confN.NN[__status][__comment_Text].flac
```

| Field | Content | Example |
|---|---|---|
| Campaign | Campaign name | `CmpRot2_Zollhauser-Bach-Riedlingen` |
| ARU | Recorder name | `ID_106` |
| Species | Species name in the project's main language | `Long-eared_Owl` |
| Timestamp | Start of the recording | `20260501_053000` |
| Range | Start and end of the snippet within the recording in seconds, padding included | `12.0-15.0` |
| Confidence | The model's score for the species it detected | `conf0.87` |
| Status | Only present on annotated detections, see below | `confirmed` |
| Comment | Only present when a comment is set: the first 20 characters of it | `comment_two_birds` |

The annotations are reflected in the file name as follows:
- **Verified**: adds the status `confirmed`, `incorrect`, or `uncertain`.
- **Corrected_Species**: replaces the detected species name with the corrected one and sets the status to `corrected`. This status takes the place of `confirmed` and `incorrect`, because the status always describes the species shown in the file name. If Verified is `uncertain`, the status is `corrected_uncertain`. The confidence remains the model's score for the species it originally detected.
- **Comment**: adds `comment_` followed by the first 20 characters of the comment.

For example, a confirmed `Long-eared Owl` detection with the comment `two birds` is exported as:
```
CmpRot2_Zollhauser-Bach-Riedlingen__ID_106__Long-eared_Owl__20260501_053000__12.0-15.0__conf0.87__confirmed__comment_two_birds.flac
```

A `Tawny Owl` detection that was marked as wrong and corrected to `Long-eared Owl` is exported as:
```
CmpRot2_Zollhauser-Bach-Riedlingen__ID_106__Long-eared_Owl__20260501_053000__12.0-15.0__conf0.87__corrected.flac
```


## Keyboard shortcuts
The keyboard shortcuts are listed on the [Keyboard shortcuts page](SHORTCUTS.md). They are also available from the app's `Help -> Keyboard Shortcuts` menu.


## Core Concepts
### Project
The largest organisational unit. A project represents a study or monitoring programme, e.g. "Lake-Constance-2026". A project is a folder: it holds a `pam-analyzer.toml` settings file and one subfolder per campaign. The settings file stores no paths, so the whole project can be moved, backed up, or shared as one folder. The project name is simply the folder name.

> **Note:** Species filter settings (lat/lon location or species list) are campaign-scoped, not project-scoped.

### Campaign
A campaign is a time-bounded field deployment during which a set of ARUs were active. The campaign name is chosen by the researcher and typically encodes the study area, e.g. `Cmp_Bad-Urach`. On the file system each campaign lives in its own subdirectory under the project folder and carries a `campaign.toml` sidecar that stores its species filter configuration. Detection CSVs are written into the campaign folder too, with audio paths stored relative to it, so a campaign is fully self-contained and can be moved, archived, or shared, including its analysis results and annotations. Campaigns are discovered automatically from the project folder. Individual ARUs within a campaign may be deployed at distinct locations within the study area.

```toml
species_filter_mode = "location"  # "location" or "list"
latitude = 47.94                  # location mode only
longitude = 9.32                  # location mode only
```

The combination of **campaign + ARU device ID** uniquely identifies a recording set within a project while the same physical ARU redeployed at a different time usually belongs to a different campaign.

### ARU (Autonomous Recording Unit)
An individual recording device, identified by its SD card volume name (e.g. `MSD-109` for AudioMoth, `2MM30692` for a Song Meter serial). Within a campaign folder, each ARU gets its own subfolder of that name, which also receives the device's own log file (`CONFIG.TXT` for AudioMoth, `<serial>_Summary.txt` for Song Meter).

Recordings are sorted into weekly subfolders (`week_01` to `week_48`). These are BirdNET weeks, four per month, which is what the geographic model uses for its per-week species list. The week is taken from the recording's GUANO timestamp or the audio file name.

After setting up a project and importing ARU SD cards, the resulting directory structure looks like this:
```
{project}/
├── pam-analyzer.toml             # project settings, written automatically
└── {campaign}/
    ├── campaign.toml             # species filter configuration sidecar
    ├── species_list.txt          # species-list mode only: the campaign's species filter list
    ├── must_have_species.txt     # optional: extra species forced into a location-mode run
    └── {aru}/
        ├── CONFIG.TXT            # device log file, or <serial>_Summary.txt for Song Meter
        └── week_NN/
            └── *.flac            # recordings, transcoded from WAV on import
```

`campaign.toml`, `species_list.txt`, `must_have_species.txt`, and (after a run) the detection CSVs live in the campaign folder, beside the audio, so a campaign stays self-contained and can be moved, archived, or shared independently of the project. The species-list files are present only when the corresponding filter option is used.

Example:
```
~/Studies/MyPAM-Project-2026/
├── Cmp_Bad-Urach/
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
└── Cmp_Ammerbuch/
    ├── campaign.toml
    ├── MSD-109/
    │   ├── week_11/
    │   └── week_12/
    └── MSD-110/
        ├── week_11/
        └── week_12/
```


## Models
PAM Analyzer runs the following models on the CPU through the [`birdnet`](https://github.com/birdnet-team/birdnet) library. All three run on ONNX Runtime, write the same per-detection CSV schema, and honour the campaign's species filter, so one campaign can be analyzed with several of them and the results reviewed side by side.

| | **BirdNET-2.4** | **BirdNET-3.0-preview3.1** | **Perch-2.0** |
|---|---|---|---|
| About | The default. An older but established release. | A preview, not a final v3.0 release, so scores and label set may still change. A final v3.0 will get its own model key and its own CSV (separates preview and release detections). | An open-world classifier from Google, more sensitive to quiet and distant calls than BirdNET-2.4. It also detects insects and amphibians, but in location mode the region filter drops those. To keep them, run in species-list mode or add the names to the must-have list. |
| Release | `BirdNET_GLOBAL_6K_V2.4` ([Zenodo](https://zenodo.org/records/15050749)) | `BirdNET+_V3.0-preview3.1_Global_11K` ([Zenodo](https://zenodo.org/records/20703646)) | Google's Perch v2, as `perch_v2_no_dft.onnx` from [justinchuby/Perch-onnx](https://huggingface.co/justinchuby/Perch-onnx) |
| Audio window | 3 s | 3 s | 5 s |
| Sample rate | 48 kHz (the library resamples other rates) | 32 kHz (the library resamples other rates) | 32 kHz (the library resamples other rates) |
| Classes | [6,522](https://birdnet.cornell.edu/models/birdnet/labels/en_us.txt) (birds, plus a few amphibians and insects) | [11,560](https://zenodo.org/records/20703646/preview/BirdNET%2B_V3.0-preview3.1_Global_11K_Labels.csv) (birds, plus amphibians, insects and mammals) | [14,795](https://www.kaggle.com/api/v1/models/google/bird-vocalization-classifier/tensorFlow2/perch_v2_cpu/1/download/assets/labels.csv) (birds, other animals, and general sound events) |
| Taxonomy | older eBird-based axis | shared with the v3.0 geographic model | shares 10,916 names with v3.0's axis |
| Region filtering | v2.4 geographic model | v3.0 geographic model | v3.0 geographic model |
| Size | ~49 MB acoustic + ~28 MB geographic | ~542 MB acoustic + ~16 MB geographic | ~413 MB acoustic, no geographic model of its own |
| Speed | fastest but least accurate | about 4.5x slower than BirdNET-2.4 | about 2x slower than BirdNET-2.4 |

BirdNET v2.4 uses the geographic model of its own generation for [region filtering](#region-filtering), while BirdNET v3.0 and Perch share BirdNET v3.0's geo model.

### Species names
BirdNET v3.0 names species under a newer taxonomy than v2.4 did, so a handful of birds changed genus: the Northern Goshawk is `Astur gentilis` where v2.4 called it `Accipiter gentilis`, and the same applies to `Charadrius`/`Anarhynchus` plovers, `Ciccaba`/`Strix` owls, `Ixobrychus`/`Botaurus` bitterns, and others.

**Detections are written under one set of scientific names, whichever model produced them.** Each engine's labels are mapped through a bundled table (`infrastructure/data/species_aliases.tsv`, 175 pairs) as they leave the model, so the same bird appears under one spelling across all three engines and sorts together in the Examine grid. The `Model` column still records which engine produced each row.

Species lists work the same way. A name you type is mapped through the same table before it is matched, so `Accipiter gentilis` and `Astur gentilis` both match whichever model runs.

The bundled BirdNET v3.0 build is a developer preview, and its own release notes list "Species list needs cleanup" as a known limitation. It carries 27 birds as two separate classes, one under a current scientific name and one under a superseded one. Where both fire on the same segment the app keeps the stronger detection and discards the weaker duplicate, so a segment yields one row per species. The app also corrects the German name of `Tyto alba`, which upstream's taxonomy gives the name belonging to `Tyto furcata`. Both are worth knowing if you compare this app's CSVs against raw BirdNET output.

### Perch confidence
Perch's classification head emits raw logits, not probabilities, and they do not sit around zero the way BirdNET v2.4's do. Silence alone lands near +4.5 and real ambient noise higher still, so a plain sigmoid would report every window's top classes at about 0.99. The runner subtracts a fixed offset of 11.2 before the sigmoid, so the `Confidence` column means roughly what it means for the BirdNET runners. The **Min confidence** setting is translated into logit space the same way, so a threshold you set applies as you would expect.

### Region filtering
In location mode the runner filters detections against a per-week species list from the geographic model of the same generation as the acoustic model, so both sides speak the same taxonomy. For v3.0 that model carries 14,082 classes to the acoustic model's 11,560. The overlap is 10,653 names, so **907 acoustic classes have no geographic entry and are always dropped in location mode**: mostly narrowly-distributed birds, genus-level entries such as `Acris`, and insects and frogs.

To keep those detections, run in species-list mode, which applies no regional filter, or add the specific names to the must-have box in location mode. The debug log reports the split per campaign, for example `birdnet: per-week species filter dropped 412 row(s): 402 out-of-region, 10 absent from the model's axis entirely. 1391 kept`.


## Troubleshooting
The application writes a rotating debug log (`pam-analyzer.log`, capped at 1 MB with one backup) to the platform's standard log directory:

- **Windows**: `%LOCALAPPDATA%\PAM Analyzer\Logs\pam-analyzer.log`
- **macOS**: `~/Library/Logs/PAM Analyzer/pam-analyzer.log`
- **Linux**: `~/.local/state/PAM Analyzer/log/pam-analyzer.log`

The easiest way to locate it is **Help > Open Log Folder** in the app.

### Changing the log level
**Help > Log Level** sets how much detail the app records: `Debug`, `Info`,
`Warning`, `Error` or `Critical`. New installs start at `Warning`. The change takes effect immediately and is remembered for the next launch.

Log output goes to `pam-analyzer.log` and, when the app is started from a
terminal, to that terminal as well.

#### Overriding the level from the environment
Setting the `PAM_LOG_LEVEL` environment variable to a level name overrides the
menu for that run.

**macOS**, from the folder you unpacked the app bundle into:
```sh
PAM_LOG_LEVEL=DEBUG "pam-analyzer-<version>-macos-arm64.app/Contents/MacOS/pam-analyzer"
```
Double-clicking the app or starting it with `open` will not pass the
variable through, because those go via LaunchServices rather than your shell.

**Windows**, in PowerShell:
```powershell
$env:PAM_LOG_LEVEL = "DEBUG"
.\pam-analyzer-<version>-windows-x86_64.exe
```
The `.\` prefix is required. PowerShell does not run programs from the current directory
without it.

**Linux**:
```sh
PAM_LOG_LEVEL=DEBUG ./pam-analyzer-<version>-linux-x86_64
```


## Changelog
The changelog can be found at the [CHANGELOG page](CHANGELOG.md).


## Acknowledgements
Many thanks to the people behind the projects PAM Analyzer builds on:

* [BirdNET](https://github.com/birdnet-team/birdnet)
* [Perch 2.0](https://arxiv.org/pdf/2508.04665) / [Perch-onnx](https://huggingface.co/justinchuby/Perch-onnx)
* [ONNX Runtime](https://onnxruntime.ai)
* [TensorFlow](https://www.tensorflow.org) / [tf2onnx](https://github.com/onnx/tensorflow-onnx)
* [Qt](https://www.qt.io/) / [PySide6](https://doc.qt.io/qtforpython)
* [Python](https://www.python.org)
* [Polars](https://pola.rs)
* [SciPy](https://scipy.org)
* [GUANO](https://github.com/riggsd/guano-py)
* [Mutagen](https://github.com/quodlibet/mutagen)
* [NumPy](https://numpy.org)
* [platformdirs](https://github.com/tox-dev/platformdirs)
* [soundfile](https://github.com/bastibe/python-soundfile)
* [libsndfile](https://libsndfile.github.io/libsndfile/) / [FLAC](https://xiph.org/flac/)
* [psutil](https://github.com/giampaolo/psutil)
* [pyqt-toast-notification](https://github.com/niklashenning/pyqt-toast-notification)
* [tomli-w](https://github.com/hukkin/tomli-w)
* [OpenStreetMap](https://www.openstreetmap.org)
* [Nuitka](https://nuitka.net)
* [uv](https://docs.astral.sh/uv/)


## Citation

If you use PAM Analyzer in your work, you can [cite](CITATION.cff) it:

```bibtex
@software{Werner_PAM_Analyzer_2026,
  author  = {Werner, Ken},
  title   = {PAM Analyzer},
  url     = {https://github.com/kenwer/pam-analyzer},
  version = {0.7.4},
  year    = {2026}
}
```

## License
This project is licensed under the AGPL-3.0 license. See the LICENSE file for the full text.
