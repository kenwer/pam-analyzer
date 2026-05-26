"""Compound widget combining a multi-column sort table, per-column filter row,
play-button column, and persistent audio player panel.

This is the detection-specific compound widget with a virtual play column at index 0, audio-bearing
rows with File/Start_Time/End_Time/Species/ARU/Confidence headers.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QItemSelectionModel, QModelIndex, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QMenu,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from ..ui.models.detections_table_model import PLAY_COLUMN_INDEX
from .audio_player import AudioPlayerPanel
from .combo_delegate import ComboDelegate, fixed
from .filter_ops import FilterOp
from .header_filter_row import HeaderFilterRow
from .multi_column_sort_table import MultiColumnSortTable
from .no_hover_style import disable_item_hover

# Verified column accepts the same fixed set the original AG Grid did.
_VERIFIED_CHOICES = ("", "true", "false", "uncertain")

if TYPE_CHECKING:
    from ..domain import Detection
    from ..ui.models.detections_table_model import DetectionsTableModel


def _label_for(d: Detection) -> str:
    """Short label used for the panel info text and spectrogram tooltips."""
    species = d.species or d.scientific_name or ""
    return f"{species}  ·  {d.aru}  ·  conf {d.confidence:.2f}"


class _PlayDelegate(QStyledItemDelegate):
    """Paints ▶ / ⏸ in column 0 and forwards clicks to the table."""

    _PLAY = "♪"
    _PAUSE = "⏸"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._model: DetectionsTableModel | None = None
        self._playing_file: str = ""

    def set_model(self, model: DetectionsTableModel | None) -> None:
        self._model = model

    def set_playing_file(self, path: str) -> None:
        self._playing_file = path

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        # Draw selection / hover background
        self.initStyleOption(option, index)
        option.text = ""
        widget = option.widget
        style = widget.style() if widget else None
        if style:
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, option, painter, widget)

        # Determine icon based on whether this row's file is playing
        table = option.widget  # MultiColumnSortTable
        src_row = table.mapToSourceRow(index.row())
        detection = self._model.detection_at(src_row) if self._model else None
        playing = detection and self._playing_file and detection.file == self._playing_file
        icon = self._PAUSE if playing else self._PLAY

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setPen(QColor("#555"))
        painter.drawText(option.rect, Qt.AlignmentFlag.AlignCenter, icon)
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex):  # noqa: N802
        hint = super().sizeHint(option, index)
        hint.setWidth(28)
        return hint


class _PersistentCheckMenu(QMenu):
    """QMenu that stays open after toggling a checkable action.

    Clicking a checkable item triggers it (toggling the check state) without
    dismissing the menu, so users can toggle multiple columns in one sitting.
    Clicking outside or pressing Escape still closes it normally.
    """

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        action = self.activeAction()
        if action and action.isCheckable():
            action.trigger()
            return  # skip super(); the menu stays open
        super().mouseReleaseEvent(event)


class DetectionTable(QWidget):
    """Compound widget: multi-column sort table + per-column filter row
    + play-button column + persistent audio player panel.

    Signals
    -------
    columnVisibilityChanged(col, visible)
        Emitted whenever a user-driven action toggles a column's visibility,
        so hosts can persist the state.
    """

    columnVisibilityChanged = Signal(int, bool)
    statusChanged = Signal(int)

    def __init__(
        self,
        audio_root: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._audio_root = audio_root
        self._model: DetectionsTableModel | None = None
        # Snapshot of the column signature last seen by _refresh_layout_for_model,
        # used to skip the filter-row rebuild when a model reset (e.g. from
        # set_column_filter) didn't actually change the column layout. Without
        # this, every keystroke in a filter input would tear down and recreate
        # all the input widgets, wiping the text the user just typed.
        self._layout_signature: tuple | None = None
        self._playing_file: str = ""
        self._current_detection: Detection | None = None
        self._suppressing_row_change = False
        self._pending_prepare: Detection | None = None
        self._prepare_timer = QTimer(self)
        self._prepare_timer.setSingleShot(True)
        self._prepare_timer.setInterval(0)
        self._prepare_timer.timeout.connect(self._do_prepare)

        # sort table
        self._table = MultiColumnSortTable()
        disable_item_hover(self._table)
        self._table.setWordWrap(False)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setNonSortableColumns({PLAY_COLUMN_INDEX})
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.horizontalHeader().setStretchLastSection(True)
        # Cap the number of rows scanned for ResizeToContents / resizeColumnsToContents.
        # Keeps the auto-fit fast on very large datasets (500k+ rows).
        self._table.horizontalHeader().setResizeContentsPrecision(100)
        self._table.horizontalHeader().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.horizontalHeader().customContextMenuRequested.connect(self._show_column_menu)

        # play delegate
        self._play_delegate = _PlayDelegate(self._table)
        self._table.setItemDelegateForColumn(PLAY_COLUMN_INDEX, self._play_delegate)
        self._table.setColumnWidth(PLAY_COLUMN_INDEX, 28)
        self._table.horizontalHeader().setSectionResizeMode(
            PLAY_COLUMN_INDEX,
            self._table.horizontalHeader().ResizeMode.Fixed,
        )

        # Combo delegates for fixed-value annotation columns. Created here
        # but not yet attached to a column, because the column indices for
        # Verified / Corrected_Species depend on whether the loaded data
        # has Species_<locale> extras (which insert before them). The actual
        # setItemDelegateForColumn happens in _refresh_layout_for_model,
        # which runs on every model reset.
        self._species_choices: tuple[str, ...] = ("",)
        self._verified_delegate = ComboDelegate(fixed(_VERIFIED_CHOICES), self._table)
        self._species_delegate = ComboDelegate(lambda: self._species_choices, self._table)

        # filter row (embedded in the header)
        self._filter_row = HeaderFilterRow(self._table)
        self._filter_row.filterChanged.connect(self._on_filter_changed)

        # (status is emitted via statusChanged signal to the host panel)

        # audio player
        self._player = AudioPlayerPanel(self)
        self._player.playbackStarted.connect(self._on_playback_started)
        self._player.playbackStopped.connect(self._on_playback_stopped)

        # Intercept clicks for the play column
        self._table.clicked.connect(self._on_cell_clicked)

        # Layout (filter row lives inside the header, not in the layout)
        self._splitter = QSplitter(Qt.Orientation.Vertical)
        self._splitter.addWidget(self._table)
        self._splitter.addWidget(self._player)
        self._splitter.setCollapsible(0, False)
        self._splitter.setCollapsible(1, True)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 0)
        self._splitter.setSizes([1, 200])  # Default height for the spectrogram/audio player panel

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._splitter)

        self._setup_shortcuts()

    # public API
    def setModel(self, model: DetectionsTableModel) -> None:  # noqa: N802, Qt API
        self._model = model
        self._play_delegate.set_model(model)
        self._table.setSourceModel(model)
        self._table.selectionModel().currentRowChanged.connect(self._on_row_changed)

        self._refresh_layout_for_model()

        # When the model resets (filter, set_detections), refresh status and sync player.
        model.modelReset.connect(self._on_model_reset)
        model.rowsInserted.connect(self._refresh_status)
        model.rowsRemoved.connect(self._refresh_status)
        self._refresh_status()

    def _refresh_layout_for_model(self) -> None:
        """Re-anchor column-position-sensitive UI to the model's current layout.

        Called on initial setModel and after each modelReset. Skips the
        actual rebuild when the column signature is unchanged so a reset
        triggered by set_column_filter or sort_by_priority does not wipe
        the filter row's input widgets. Only set_detections with a new
        set of locale extras should trigger the rebuild work.
        """
        if self._model is None:
            return
        signature = (
            self._model.columnCount(),
            tuple(self._model.column_names(include_play=True)),
            frozenset(self._model.numeric_column_indices()),
        )
        if signature == self._layout_signature:
            return
        self._layout_signature = signature
        self._filter_row.rebuild(
            self._model.columnCount(),
            numeric_cols=self._model.numeric_column_indices(),
        )
        self._filter_row.set_column_visible(PLAY_COLUMN_INDEX, False)
        for name, delegate in (
            ("Verified", self._verified_delegate),
            ("Corrected_Species", self._species_delegate),
        ):
            idx = self._model.index_of(name)
            if idx >= 0:
                self._table.setItemDelegateForColumn(idx, delegate)

    def setSortPriority(  # noqa: N802 (Qt-style)
        self, priority: list[tuple[str, Qt.SortOrder]]
    ) -> None:
        """Apply a sort priority by column name.

        Names that don't exist in the current model are skipped, so a
        restored sort survives a column being renamed or a Species_<locale>
        extra that wasn't present in the previous data load.
        """
        if self._model is None:
            return
        idx_priority: list[tuple[int, Qt.SortOrder]] = []
        for name, order in priority:
            idx = self._model.index_of(name)
            if idx >= 0:
                idx_priority.append((idx, order))
        self._table.setSortPriority(idx_priority)

    def sortPriority(self) -> list[tuple[str, Qt.SortOrder]]:  # noqa: N802 (Qt-style)
        """Return the current sort priority as (column_name, order) pairs.

        Names survive set_detections inserting Species_<locale> extras
        (which shift the column indices the inner table uses internally);
        restoring via setSortPriority round-trips correctly.
        """
        if self._model is None:
            return []
        names = self._model.column_names(include_play=True)
        out: list[tuple[str, Qt.SortOrder]] = []
        for col, order in self._table.sortPriority():
            if 0 <= col < len(names):
                out.append((names[col], order))
        return out

    def setAudioRoot(self, path: Path | None) -> None:  # noqa: N802, Qt-style camelCase
        self._audio_root = path

    def setSpeciesChoices(self, choices: list[str]) -> None:  # noqa: N802, Qt-style camelCase
        """Set the option list backing the Corrected_Species combo editor.

        Always prepends an empty entry so the user can clear the field.
        """
        deduped = sorted({c for c in choices if c})
        self._species_choices = ("", *deduped)

    def fitColumnsToContents(self) -> None:  # noqa: N802, Qt-style camelCase
        """Resize all columns to fit visible-data widths (sampled per resizeContentsPrecision)."""
        self._table.resizeColumnsToContents()

    def table(self) -> MultiColumnSortTable:
        """Expose the inner table for callers that need to set delegates, edit triggers, etc."""
        return self._table

    # keyboard shortcuts
    def _setup_shortcuts(self) -> None:
        """Bind table-level shortcuts via QShortcut.

        QKeySequence does the modifier matching, so a bare "S" sequence
        won't fire on Cmd+S / Ctrl+S. WidgetShortcut context scopes them
        to the table, so an open editor delegate still receives raw keys.
        """
        bindings = (
            ("Space", self._toggle_player),
            ("T", lambda: self._set_verified("true")),
            ("F", lambda: self._set_verified("false")),
            ("U", lambda: self._set_verified("uncertain")),
            ("C", lambda: self._start_editing_column("Comment")),
            ("S", lambda: self._start_editing_column("Corrected_Species")),
            ("J", self._player_jump_to_detection),
            ("B", self._player_seek_to_file_start),
        )
        for sequence, handler in bindings:
            shortcut = QShortcut(QKeySequence(sequence), self._table)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(handler)

    def _toggle_player(self) -> None:
        if self._player.isVisible():
            self._player.toggle()

    def _player_jump_to_detection(self) -> None:
        if self._player.isVisible():
            self._player.jump_to_detection()

    def _player_seek_to_file_start(self) -> None:
        if self._player.isVisible():
            self._player.seek_to_file_start()

    def _set_verified(self, value: str) -> None:
        if self._model is None:
            return
        current = self._table.currentIndex()
        if not current.isValid():
            return
        src_row = self._table.mapToSourceRow(current.row())
        src_index = self._model.index(src_row, self._model.index_of("Verified"))
        self._model.setData(src_index, value, Qt.ItemDataRole.EditRole)

    def _start_editing_column(self, col_name: str) -> None:
        if self._model is None:
            return
        current = self._table.currentIndex()
        if not current.isValid():
            return
        col = self._model.index_of(col_name)
        if col < 0 or self._table.isColumnHidden(col):
            return
        proxy_index = self._table.model().index(current.row(), col)
        self._table.setCurrentIndex(proxy_index)
        self._table.edit(proxy_index)

    # handlers
    def _on_row_changed(self, current: QModelIndex, _: QModelIndex) -> None:
        if self._suppressing_row_change or not current.isValid() or self._model is None:
            return
        detection = self._model.detection_at(self._table.mapToSourceRow(current.row()))
        if detection is None or not detection.file:
            return
        # Defer by one event-loop tick so a ♪ click on the same row can cancel
        # this prepare before it runs (avoiding a double file-load).
        self._pending_prepare = detection
        self._prepare_timer.start()

    def _do_prepare(self) -> None:
        if self._pending_prepare is not None:
            self._present(self._pending_prepare, autoplay=False)
            self._pending_prepare = None

    def _on_cell_clicked(self, index: QModelIndex) -> None:
        if index.column() != PLAY_COLUMN_INDEX or self._model is None:
            return
        # Cancel any deferred prepare for this row — play_detection() subsumes it.
        self._prepare_timer.stop()
        self._pending_prepare = None
        detection = self._model.detection_at(self._table.mapToSourceRow(index.row()))
        if detection is None or not detection.file:
            return
        self._present(detection, autoplay=True)

    def _present(self, detection: Detection, *, autoplay: bool) -> None:
        self._current_detection = detection
        file_path = (
            str(self._audio_root / detection.file)
            if self._audio_root
            else detection.file
        )
        info = _label_for(detection)
        ctx = self._context_detections_for(detection)
        if autoplay:
            self._player.play_detection(
                file_path,
                detection.start_time,
                detection.end_time,
                info,
                context_detections=ctx,
            )
        else:
            self._player.prepare(
                file_path,
                detection.start_time,
                detection.end_time,
                info,
                context_detections=ctx,
            )

    def _context_detections_for(self, current: Detection) -> list[tuple[float, float, str]]:
        """Return (start_s, end_s, label) for visible detections in the same file."""
        return [
            (d.start_time, d.end_time, _label_for(d))
            for d in self._model.detections()
            if d.file == current.file and d is not current
        ]

    def _on_model_reset(self) -> None:
        self._refresh_layout_for_model()
        self._refresh_status()
        self._sync_player()

    def _sync_player(self) -> None:
        """Called (deferred) after every model reset to keep the player in sync."""
        proxy = self._table.model()
        if self._model is None or proxy.rowCount() == 0:
            self._player.stop()
            self._current_detection = None
            return

        select_flags = QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows

        # If the previous selection is still visible, reselect it (without
        # re-priming the player) and refresh the context markers.
        if self._current_detection is not None:
            for src_row, d in enumerate(self._model.detections()):
                if d is self._current_detection:
                    proxy_index = proxy.mapFromSource(self._model.index(src_row, 0))
                    self._suppressing_row_change = True
                    try:
                        self._table.selectionModel().setCurrentIndex(proxy_index, select_flags)
                    finally:
                        self._suppressing_row_change = False
                    self._player.update_context_detections(self._context_detections_for(self._current_detection))
                    return

        # Selection was filtered out (or none): select the first visible row.
        index = proxy.index(0, 0)
        self._table.selectionModel().setCurrentIndex(index, select_flags)
        self._table.scrollTo(index)
        self._table.setFocus()

    def _on_filter_changed(self, col: int, text: str, op: FilterOp) -> None:
        if self._model is None:
            return
        self._model.set_column_filter(col, text, op)
        self._refresh_status()

    def _on_playback_started(self, file_path: str) -> None:
        self._playing_file = file_path
        self._play_delegate.set_playing_file(file_path)
        self._table.viewport().update()

    def _on_playback_stopped(self) -> None:
        self._playing_file = ""
        self._play_delegate.set_playing_file("")
        self._table.viewport().update()

    def _refresh_status(self, *_: object) -> None:
        if self._model is None:
            return
        self.statusChanged.emit(self._model.rowCount())

    def makeColumnsMenu(  # noqa: N802 (Qt-style)
        self, parent: QWidget | None = None
    ) -> QMenu:
        """Create a persistent checkable menu pre-wired to this table's columns.

        The menu repopulates itself on ``aboutToShow`` so checkmarks always
        reflect current visibility. Pass the owning widget as *parent* so Qt
        manages lifetime correctly.
        """
        menu = _PersistentCheckMenu(parent)
        menu.aboutToShow.connect(lambda: self.populateColumnsMenu(menu))
        return menu

    def _show_column_menu(self, pos: QPoint) -> None:
        if self._model is None:
            return
        header = self._table.horizontalHeader()
        menu = _PersistentCheckMenu(self)
        self.populateColumnsMenu(menu)
        menu.exec(header.viewport().mapToGlobal(pos))

    def populateColumnsMenu(self, menu: QMenu) -> None:  # noqa: N802 (Qt-style)
        """Fill *menu* with one checkable action per data column.

        Reused by the toolbar columns button and the header's right-click
        context menu so both UIs stay in sync.
        """
        if self._model is None:
            return
        menu.clear()
        for logical in range(PLAY_COLUMN_INDEX + 1, self._model.columnCount()):
            name = self._model.headerData(logical, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole)
            action = QAction(str(name), menu)
            action.setCheckable(True)
            action.setChecked(not self._table.isColumnHidden(logical))
            action.toggled.connect(lambda checked, col=logical: self._toggle_column(col, checked))
            menu.addAction(action)

    def setHiddenColumnNames(  # noqa: N802 (Qt-style)
        self, hidden_names: list[str] | set[str]
    ) -> None:
        """Programmatically apply a saved hidden-column set without emitting
        :pyattr:`columnVisibilityChanged` (callers usually drive the persistence
        themselves and don't want an echo)."""
        if self._model is None:
            return
        hidden = set(hidden_names)
        for logical in range(PLAY_COLUMN_INDEX + 1, self._model.columnCount()):
            name = str(self._model.headerData(logical, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole))
            visible = name not in hidden
            self._table.setColumnHidden(logical, not visible)
            if not visible:
                # Hidden columns must not retain an active filter the user
                # can't see or clear.
                self._model.set_column_filter(logical, "")
                self._filter_row.clear_column(logical)
            self._filter_row.set_column_visible(logical, visible)

    def hiddenColumnNames(self) -> list[str]:  # noqa: N802 (Qt-style)
        """Return the header names of currently hidden columns."""
        if self._model is None:
            return []
        names: list[str] = []
        for logical in range(PLAY_COLUMN_INDEX + 1, self._model.columnCount()):
            if self._table.isColumnHidden(logical):
                name = self._model.headerData(logical, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole)
                names.append(str(name))
        return names

    def setPlaybackPadding(  # noqa: N802 (Qt-style)
        self, before: float, after: float
    ) -> None:
        """Forward to the embedded audio player so playback honors the new padding."""
        self._player.setPadding(before, after)

    def _toggle_column(self, col: int, visible: bool) -> None:
        self._table.setColumnHidden(col, not visible)
        if not visible and self._model is not None:
            # Clear the filter for hidden columns (invisible active filter = UX footgun)
            self._model.set_column_filter(col, "")
            self._filter_row.clear_column(col)
        self._filter_row.set_column_visible(col, visible)
        self.columnVisibilityChanged.emit(col, visible)
