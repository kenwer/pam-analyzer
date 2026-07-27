# Architecture
PAM Analyzer is a PySide6 desktop application. The codebase is organised into
packages by responsibility, with a rich domain model at its core: the entities
carry their own behaviour and persistence. Qt is confined to the UI-facing
packages, and the concrete adapters (analysis runners, audio I/O, sd-card
scanner) are wired together in a single composition root.

For domain concepts (Project, Campaign, ARU, Detection) see the [README](README.md).

## Quick start
```sh
uv sync
uv run poe run-dbg
```

Sub-commands:
- `uv run poe compile-ui` — regenerate `ui_*.py` from `.ui` files
- `uv run poe compile-qrc` — regenerate `*_rc.py` from `.qrc` files
- `uv run poe lint` - ruff lint
- `uv run poe test` — run the test suite

## Package layout
```
src/pam_analyzer/
├── domain/          # Rich entities that persist themselves (Campaign, Project, DetectionSet), protocols, pure functions, path conventions.
├── infrastructure/  # I/O adapters that are not entity persistence: in-process BirdNET + Perch runners (via the birdnet lib), audio I/O, discovery, sd-card scanning, .pamproj migration.
├── workers/         # Qt-aware background tasks: QThread workers + ImportOrchestrator.
├── widgets/         # Reusable Qt widgets with no domain knowledge.
├── ui/              # App-specific panels, dialogs, Qt models, generated .ui wrappers.
│   ├── panels/      # Top-level tab panels (CampaignsPanel, BirdNetPanel, ExaminePanel, ...).
│   ├── dialogs/     # Modal dialogs (ImportConflictDialog, AboutDialog).
│   ├── models/      # QAbstractItemModel subclasses backing tree and table views.
│   └── settings.py  # AppSettings: persistent UI/host state (QSettings wrapper).
├── app/             # Composition root and application entry point.
└── __main__.py      # Thin entry shim; delegates to app/__main__.py.
```


## Package responsibilities
These are conventions, not a mechanically enforced contract (there is no
import-boundary test). Keep them in mind when deciding where code belongs:

- `domain` owns the entities and their persistence. `Campaign`, `Project`, and
  the `DetectionSet` aggregate read and write their own files (TOML sidecars,
  detection CSVs) through `domain.paths`. Also holds pure logic (species
  filtering, audio-import discovery, the Detection schema). It stays Qt-free so
  it can be exercised in plain pytest, but it is no longer stdlib-only: it
  depends on `tomli_w`/`tomllib` and `platformdirs` for that persistence.
- `infrastructure` holds the remaining I/O adapters that are not entity
  persistence: the BirdNET/Perch analysis runners, audio extraction, on-disk
  discovery, the sd-card scanner, and the one-time .pamproj migration. Qt-free.
- `workers` are the Qt-aware background tasks (QThread workers, the import
  orchestrator). They may call domain and infrastructure.
- `widgets` are reusable Qt components below the panel level. They may use
  domain vocabulary (enums, value objects, pure functions such as
  `domain.filter_ops`) but should not touch I/O or panels.
- `ui` holds the app-specific panels, dialogs, and Qt models, plus `AppState`.
- `app` is the composition root: it constructs the adapters and wires them in.

Entities persist themselves, so a mutation now happens by calling a method on
the entity (e.g. `campaign.save()`). Panels must not do this directly: every
mutation goes through `AppState`, which rebuilds derived state (the campaign
list, the audio inventory) after each write. Panels may call an entity's
read-only methods (`campaign.read_species_list()`, `count_audio_files()`).


## Key patterns
### Composition root
`app/__main__.py:build_main_window()` constructs every concrete adapter (analysis
runners, audio extractor, sd-card scanner, import orchestrator) and passes them
into the window and panel constructors. No panel creates its own dependencies.
Swapping a real adapter for a test fake only requires a change in one place.
Entity persistence needs no injection: `AppState` calls `Campaign`/`Project`
methods directly.

### Intent signals
Child panels emit typed intent signals (`createRequested`, `updateRequested`,
`deleteRequested`) rather than mutating entities or updating AppState directly.
The parent panel handles those signals by calling the appropriate AppState
method, which persists the entity and rebuilds derived state. This keeps child
panels free of persistence knowledge and makes them testable in isolation.

Example: `CampaignDetailWidget` emits
`createRequested(name, mode, location, species_text, must_have_text)`;
`CampaignsPanel` receives it and calls `app_state.create_campaign(...)`, which
calls `campaign.create()` and rewrites the species file.

### AppState
`ui/app_state.py:AppState` is a `QObject` that holds the live project, campaigns,
audio inventory, and analysis results. It emits a named Qt signal for each state
change. Every panel receives an `AppState` at construction and connects only to the
signals it needs. Writes to AppState (refresh, save, append result) are performed by
the panel that owns the action, not by the child that triggered it.

### Worker pattern
Background work runs on a `QThread`. Each worker (`AnalysisWorker`,
`AudioImportWorker`) is a `QObject` with `progress`, `finished`, and `failed` signals.
A `_SignalProgress` adapter bridges the domain-level plain-callable progress protocol
to Qt signal emission, keeping the domain layer unaware of Qt.

### ImportOrchestrator
`workers/import_orchestrator.py:ImportOrchestrator` owns the full SD card import
lifecycle: polling for inserted cards (`QTimer`), dedup queue (`CardQueue`), conflict
detection, and `AudioImportWorker` lifecycle. It holds the state machine
(IDLE / WATCHING / AWAITING_CONFLICT / COPYING) and emits signals that
`CampaignDetailWidget` connects to.

When a conflict is found, the orchestrator moves to AWAITING_CONFLICT and emits
`conflict_detected`. The panel shows `ImportConflictDialog` and calls either
`resolve_conflict(resolutions)` or `skip_card()` to resume. The orchestrator has no
knowledge of `AppState`; the panel relays relevant signals (`watching_started`,
`watching_stopped`, `result_ready`) to it.

### Detection schema
`domain/detection_schema.py` is the single definition of the Detection record's
shape: column names and canonical order, per-column access and CSV conversion
(`ColumnSpec`), CSV row serialization derived from the column table, and the
`detections-{model_key}.csv` filename pattern (plus the `campaign_csvs` and
`campaign_csv_for_model` path helpers that depend on it).
The `DetectionSet` aggregate, `BaseAnalysisRunner`, `analysis_discovery`, and
`DetectionsTableModel` all derive from it, so a schema change (new column, renamed
column, filename convention) lands in one file. The Examine panel's compound table
widget lives in `ui/detection_table.py`: it is Detection-specific, so it belongs in
`ui/`, while the generic pieces it composes (`MultiColumnSortTable`,
`HeaderFilterRow`, `AudioPlayerPanel`) stay in `widgets/`.

### Protocol-based seams
`domain/analysis.py` defines `AnalysisRunner` and `AnalysisProgress` as structural
protocols. `BirdnetRunner` and `PerchRunner` both satisfy `AnalysisRunner` and are
wired into the composition root as a `{model_key: runner}` dict. The `BirdNetPanel`
exposes them via a Model dropdown. Each runner declares a `model_key` string
(`"BirdNET-2.4"`, `"Perch-2.0"`) that doubles as the CSV filename suffix written
by that runner, so multiple model runs coexist for one campaign. Tests use
`FakeRunner`. This is the main place where a concrete infrastructure adapter is
substituted at test time.

Both concrete runners extend `BaseAnalysisRunner` (`infrastructure/base_analysis_runner.py`),
which centralises the per-campaign loop, file iteration, species-filter resolution,
ARU/rank computation, and CSV writing. Subclasses only fill three hooks: `_load_model`,
`_open_predict_session`, and `_parse_row`. Adding a third model means writing a class
with those three methods and a `model_key`, then registering it in the composition
root, with no changes to the panel or the worker.


## Generated files
The `.ui` files under `ui/panels/` and `ui/dialogs/` are Qt Designer sources. The
matching `ui_*.py` files are produced by `uv run poe compile-ui` and should not be
edited by hand. `resources_rc.py` is produced by `uv run poe compile-qrc`.


## Tests
Tests mirror the source layout under `tests/`. Domain and infrastructure tests are
plain pytest with no Qt dependency. Because entities persist themselves, their
tests exercise real files under `tmp_path` (see
`tests/domain/test_campaign_persistence.py`, `test_project_persistence.py`,
`test_detection_set.py`) rather than a fake repository. UI and widget tests use
pytest-qt; a shared `QApplication` is set up in `tests/conftest.py`. The analysis
runner is still substituted with a `FakeRunner` at the worker seam (see
`tests/workers/test_analysis_worker.py`).
