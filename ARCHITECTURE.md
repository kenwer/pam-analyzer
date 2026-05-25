# Architecture
PAM Analyzer is a PySide6 desktop application. The codebase is organised in layers
with explicit dependency rules. All concrete dependencies are wired together in a
single composition root; no panel constructs its own dependencies.

For domain concepts (Project, Campaign, ARU, Detection) see the [README](README.md).


## Package layout
```
src/pam_analyzer/
├── domain/          # Pure Python: entities, protocols, pure functions. No Qt, no I/O.
├── infrastructure/  # I/O adapters: TOML/CSV repos, BirdNET subprocess, audio I/O.
├── workers/         # Qt-aware background tasks: QThread workers + ImportOrchestrator.
├── widgets/         # Reusable Qt widgets with no domain knowledge.
├── ui/              # App-specific panels, dialogs, Qt models, generated .ui wrappers.
│   ├── panels/      # Top-level tab panels (CampaignsPanel, BirdNetPanel, ExaminePanel, ...).
│   ├── dialogs/     # Modal dialogs (ImportConflictDialog, AboutDialog).
│   └── models/      # QAbstractItemModel subclasses backing tree and table views.
├── app/             # Composition root and application entry point.
└── __main__.py      # Thin entry shim; delegates to app/__main__.py.
```


## Layer rules
| Layer | May import | Must not import |
|---|---|---|
| `domain` | stdlib only | Qt, infrastructure, workers, ui |
| `infrastructure` | domain, stdlib, third-party I/O | Qt, workers, ui |
| `workers` | domain, infrastructure, Qt | ui |
| `widgets` | Qt | domain, infrastructure, workers, ui/panels |
| `ui` | domain, infrastructure, workers, widgets, Qt | (composition root only) |
| `app` | everything | (no restrictions; this is the composition root) |

The `widgets/` layer is for generic, reusable Qt components that carry no domain
knowledge. App-specific components that know about `Campaign`, `Detection`, etc.
belong in `ui/`.


## Key patterns
### Composition root
`app/__main__.py:build_main_window()` constructs every concrete dependency and passes
them into the window and panel constructors. No panel creates its own dependencies.
Swapping a real repo or adapter for a test fake only requires a change in one place.

### Intent signals
Child panels emit typed intent signals (`createRequested`, `updateRequested`,
`deleteRequested`) rather than calling repositories or updating AppState directly.
The parent panel handles those signals by calling the appropriate repository or
AppState method. This keeps child panels free of repository knowledge and makes
them testable in isolation.

Example: `CampaignDetailWidget` emits
`createRequested(name, mode, location, species_text, must_have_text)`;
`CampaignsPanel` receives it and calls `campaign_repo.create(...)`.

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

### Protocol-based seams
`domain/analysis.py` defines `AnalysisRunner` and `AnalysisProgress` as structural
protocols. `BirdnetRunner` and `PerchRunner` both satisfy `AnalysisRunner` and are 
wired into the composition root as `{model_key: runner}` dict. The `BirdNetPanel` 
exposes them via a Model dropdown. Each runner declares a `model_key` string 
(`"birdnet"`, `"perch"`) that doubles as the CSV filename suffix written by that
runner, so multiple model runs coexist for one campaign. Tests use `FakeRunner`.
This is the main place where a concrete infrastructure adapter is substituted at
test time.


## Generated files
The `.ui` files under `ui/panels/` and `ui/dialogs/` are Qt Designer sources. The
matching `ui_*.py` files are produced by `uv run poe compile-ui` and should not be
edited by hand. `resources_rc.py` is produced by `uv run poe compile-qrc`.


## Tests
Tests mirror the source layout under `tests/`. Domain and infrastructure tests are
plain pytest with no Qt dependency. UI and widget tests use pytest-qt; a shared
`QApplication` is set up in `tests/conftest.py`. Workers are tested with fake
implementations of domain protocols (see `tests/workers/test_analysis_worker.py`).
