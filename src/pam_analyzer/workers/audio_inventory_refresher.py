from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot

from ..domain import AudioInventory
from ..infrastructure import discover_audio_structure, resolve_audio_sizes


class AudioInventoryRefresher(QObject):
    """Owns the background thread that builds or resolves a project's AudioInventory.

    Used two ways. After an import or a campaign create/rename/delete, the
    on-disk set of audio files may have changed, so refresh(folder) reruns both
    discovery phases from scratch: the structure walk (counts, tree, date
    ranges) followed by the per-file size stat. inventoryReady is emitted after
    each phase, so the tree updates quickly with sizes pending and then fills
    in. At project open the structure walk has already happened as part of
    load_project, so refresh(folder, inventory) skips straight to the
    size stat on that already-walked inventory and inventoryReady fires once.

    Every emission carries the folder it was computed for so a caller can drop a
    result for a project the user has since navigated away from. Only one run
    happens at a time: a new refresh() cancels the one in progress, and a late
    result from that abandoned run is ignored via the sender-identity check
    below.

    It keeps the whole QThread lifecycle (launch, cancel, bounded teardown) in
    one place behind a single high-level signal, so the AppState that uses it
    only connects to inventoryReady and calls refresh()/request_shutdown(). The
    AudioRefreshWorker it drives is a thin moveToThread shim defined below.
    """

    inventoryReady = Signal(object, object)  # (project_folder: Path, AudioInventory)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: AudioRefreshWorker | None = None

    def refresh(self, folder: Path, inventory: AudioInventory | None = None) -> None:
        """Build or resolve the inventory for folder on a worker thread.

        With no inventory, walks the folder from scratch (structure then sizes).
        With an already-walked inventory, skips straight to resolving its sizes,
        for the project-open path where the walk already happened.

        Any run already in progress is for a run this call supersedes, so it
        is cancelled first. Only one run happens at a time.
        """
        self._cancel_running()
        self._thread = QThread(self)
        self._worker = AudioRefreshWorker(folder, inventory)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.structureReady.connect(self._on_structure)
        self._worker.succeeded.connect(self._on_sized)
        # DirectConnection so quit() runs inline on the worker thread as run()
        # returns, after the final (sized) emit. The later queued teardown wait()
        # then returns immediately. A cancelled run emits no succeeded, so this
        # connects only to the success path.
        self._worker.succeeded.connect(self._thread.quit, Qt.ConnectionType.DirectConnection)
        self._thread.start()

    def request_shutdown(self) -> None:
        """Cancel any in-flight rebuild with a bounded wait, e.g. on app close."""
        self._cancel_running()

    def _on_structure(self, folder: Path, inventory: AudioInventory) -> None:
        """Publish the size-less tree mid-run. Runs while the worker keeps going on
        the size pass, so it does not tear the thread down."""
        if self.sender() is not self._worker:
            return  # a late emit from a run this one replaced; drop it
        self.inventoryReady.emit(folder, inventory)

    def _on_sized(self, folder: Path, inventory: AudioInventory) -> None:
        if self.sender() is not self._worker:
            return  # a late emit from a run this one replaced; drop it
        self._teardown()
        self.inventoryReady.emit(folder, inventory)

    def _teardown(self) -> None:
        """Tear down after a run finished (succeeded fired, quit already called via
        DirectConnection), so wait() returns immediately."""
        if self._thread is not None:
            self._thread.wait()
            self._thread.deleteLater()
            self._thread = None
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None

    def _cancel_running(self) -> None:
        """Abandon an in-flight run: signal the worker to stop, then wait a bounded
        time so a hung network mount cannot block. A stale result that still slips
        through is dropped by the sender-identity check in the slots, so this is
        best-effort by design."""
        if self._worker is not None:
            self._worker.cancel()
        if self._thread is not None:
            self._thread.quit()
            # deleteLater only once the thread has actually stopped: deleting a
            # still-running QThread would crash. On a timeout (hung mount) skip it.
            if self._thread.wait(5000):
                self._thread.deleteLater()
                if self._worker is not None:
                    self._worker.deleteLater()
            self._thread = None
            self._worker = None


class AudioRefreshWorker(QObject):
    """Builds or resolves a project's AudioInventory from disk.

    Given no inventory, walks the folder first (counts, tree, date ranges) and
    publishes that size-less result via structureReady (total_bytes is None),
    then stats every file and publishes the fully sized inventory via succeeded.
    Given an already-walked inventory, skips the walk and structureReady, and
    only stats its files. Either way, both signals carry the folder they were
    computed for, so the caller can drop a result once the user has switched
    projects.

    Cancellable: cancel() sets a flag checked between and within the phases. A
    cancelled run emits no succeeded (and no structureReady once the flag is set).
    The flag is a plain threading.Event, not a Qt type, so the infrastructure
    functions it drives stay Qt-free.

    A thin shim over discover_audio_structure and resolve_audio_sizes whose only
    job is to be moveToThread'd, so it lives with its sole owner,
    AudioInventoryRefresher, rather than in its own module.
    """

    structureReady = Signal(object, object)  # (project_folder: Path, AudioInventory) sizes pending
    succeeded = Signal(object, object)       # (project_folder: Path, AudioInventory) fully sized

    def __init__(self, project_folder: Path, inventory: AudioInventory | None = None) -> None:
        super().__init__()
        self._folder = project_folder
        self._inventory = inventory
        self._cancel = Event()

    def cancel(self) -> None:
        self._cancel.set()

    @Slot()
    def run(self) -> None:
        structure = self._inventory
        if structure is None:
            structure = discover_audio_structure(self._folder)
            if self._cancel.is_set():
                return
            self.structureReady.emit(self._folder, structure)
        sized = resolve_audio_sizes(structure, self._cancel)
        if self._cancel.is_set():
            return
        self.succeeded.emit(self._folder, sized)
