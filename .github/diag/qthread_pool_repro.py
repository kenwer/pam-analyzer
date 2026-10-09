"""Standalone reproducer attempt for the Windows crash in the load_project fixture.

Mirrors tests/ui/conftest.py: a QObject worker moved to a QThread, started via
thread.started, quitting the thread through a DirectConnection, while the main
thread pumps events. No pytest, no widgets, no project code.

Modes:
    pool    the worker runs a ThreadPoolExecutor, as discover_audio_structure does
    nopool  the worker does the same work inline, so only the QThread is in play
"""

import sys
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread, Signal, Slot

ROUNDS = 300


class Worker(QObject):
    done = Signal(object)

    def __init__(self, use_pool: bool) -> None:
        super().__init__()
        self._use_pool = use_pool

    @Slot()
    def run(self) -> None:
        if self._use_pool:
            with ThreadPoolExecutor(max_workers=32) as pool:
                result = list(pool.map(abs, range(64)))
        else:
            result = [abs(i) for i in range(64)]
        self.done.emit(result)


def one_round(app: QCoreApplication, use_pool: bool) -> None:
    thread = QThread()
    worker = Worker(use_pool)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)

    outcome: list[object] = []
    worker.done.connect(outcome.append)
    worker.done.connect(thread.quit, Qt.ConnectionType.DirectConnection)

    thread.start()
    while thread.isRunning():
        app.processEvents()
    thread.wait()
    worker.deleteLater()
    thread.deleteLater()
    app.processEvents()
    assert outcome, "worker never reported"


def main() -> None:
    mode = sys.argv[1]
    app = QCoreApplication(sys.argv)
    for i in range(ROUNDS):
        one_round(app, use_pool=mode == "pool")
        if i % 50 == 0:
            print(f"{mode}: round {i} ok", flush=True)
    print(f"{mode}: all {ROUNDS} rounds ok", flush=True)


if __name__ == "__main__":
    main()
