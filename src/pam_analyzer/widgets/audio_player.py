from __future__ import annotations

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .spectrogram_widget import SpectrogramWidget

# Only statuses where seeking is guaranteed to be safe.
_LOADED_STATUSES = (
    QMediaPlayer.MediaStatus.LoadedMedia,
    QMediaPlayer.MediaStatus.EndOfMedia,
)


class AudioPlayerPanel(QWidget):
    """Persistent bottom panel for audio playback.

    prepare()        — load a detection and seek to its start without playing.
    play_detection() — play the detection window, auto-stop at end_time + pad_after.
    play_file()      — start free playback from the current position, no auto-stop.
    toggle()         — pause if playing, otherwise call play_file().

    Signals:
        playbackStarted(file_path)  - emitted when playback begins
        playbackStopped()           - emitted when playback stops (timer or user)
    """

    playbackStarted = Signal(str)
    playbackStopped = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._player = QMediaPlayer(self)
        self._audio_out = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_out)

        self._current_file: str | None = None
        self._start_time: float = 0.0
        self._end_time: float = 0.0
        self._pad_before_s: float = 0.0
        self._pad_after_s: float = 0.0
        self._info: str = ""

        self._stop_timer = QTimer(self)
        self._stop_timer.setSingleShot(True)
        self._stop_timer.timeout.connect(self._on_stop_timer)

        # Pending-load queue: set by prepare()/play_detection() before entering the
        # load chain.  setSource(QUrl()) triggers NoMedia -> _on_media_status_changed
        # calls _begin_pending_load() which issues the real setSource().
        # This NoMedia round-trip is required because Qt's AVFoundation backend
        # coalesces rapid setSource() calls and skips the load signal.
        self._pending_load_file: str | None = None
        self._pending_auto_play: bool = False
        self._load_in_progress = False  # True between load request and LoadedMedia
        self._bounded = False  # True when stop timer should fire at detection end
        self._needs_seek_before_play = False  # Guards AVFoundation async-seek race after prepare()

        self._build_ui()

        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.playbackStateChanged.connect(self._on_state_changed)
        self._player.mediaStatusChanged.connect(self._on_media_status_changed)

        self.setVisible(False)

    def prepare(
        self,
        file_path: str,
        start_time: float,
        end_time: float,
        info: str = "",
        *,
        context_detections: list[tuple[float, float, str]] | None = None,
    ) -> None:
        """Load a detection without playing.

        Shows the panel, seeks to start_time - pad_before, and stops any current
        playback. Call toggle() or play_file() to start free playback afterward.
        """
        was_playing = self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        self._load_detection(
            file_path,
            start_time,
            end_time,
            info,
            auto_play=False,
            context_detections=context_detections,
        )
        if was_playing:
            self.playbackStopped.emit()

    def play_detection(
        self,
        file_path: str,
        start_time: float,
        end_time: float,
        info: str = "",
        *,
        context_detections: list[tuple[float, float, str]] | None = None,
    ) -> None:
        """Play the detection window, auto-stopping at end_time + pad_after."""
        self._load_detection(
            file_path,
            start_time,
            end_time,
            info,
            auto_play=True,
            context_detections=context_detections,
        )

    def play_file(self) -> None:
        """Start free playback from the current position with no auto-stop boundary."""
        self._stop_timer.stop()
        self._bounded = False
        if self._load_in_progress:
            # File not yet ready; arm auto-play so seek+play happen together once
            # LoadedMedia fires.
            self._pending_auto_play = True
        elif self._needs_seek_before_play:
            # First play after prepare(): re-seek and play atomically so AVFoundation
            # applies the position before starting playback.
            self._needs_seek_before_play = False
            self._seek_to_start()
            self._player.play()
            if self._current_file:
                self.playbackStarted.emit(self._current_file)
        else:
            self._player.play()

    def toggle(self) -> None:
        """Pause if playing; start free playback otherwise."""
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._pending_auto_play = False
            self._player.pause()
        else:
            self.play_file()

    def update_context_detections(
        self,
        context_detections: list[tuple[float, float, str]],
    ) -> None:
        """Refresh the spectrogram's context-detection markers without reloading audio."""
        self._spectrogram.update_context_detections(context_detections)

    def stop(self) -> None:
        """Stop playback and hide the panel."""
        self._stop_timer.stop()
        self._pending_load_file = None
        self._pending_auto_play = False
        self._bounded = False
        self._needs_seek_before_play = False
        self._load_in_progress = False
        self._player.stop()
        self.setVisible(False)
        self.playbackStopped.emit()

    def setPadding(self, before: float, after: float) -> None:  # noqa: N802 (Qt-style)
        """Update the playback padding window.

        If a bounded clip is currently playing, re-arms the stop timer so the
        user immediately hears the new post-roll. If paused on a loaded clip,
        re-seeks to detection start so the new pre-roll takes effect.
        """
        self._pad_before_s = max(0.0, float(before))
        self._pad_after_s = max(0.0, float(after))
        if self._bounded and self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._arm_stop_timer()
        elif self._player.mediaStatus() in _LOADED_STATUSES and not self._load_in_progress:
            self._seek_to_start()

    def _load_detection(
        self,
        file_path: str,
        start_time: float,
        end_time: float,
        info: str,
        *,
        auto_play: bool,
        context_detections: list[tuple[float, float, str]] | None = None,
    ) -> None:
        """Shared loader funneled by both prepare() and play_detection()."""
        same_file_loaded = (
            file_path == self._current_file and self._current_file is not None and not self._load_in_progress
        )
        same_file_loading = file_path == self._current_file and self._load_in_progress

        self._current_file = file_path
        self._start_time = start_time
        self._end_time = end_time
        self._info = info
        self.setVisible(True)
        self._stop_timer.stop()
        self._bounded = auto_play

        if same_file_loaded:
            # File already decoded: update overlays without re-rendering the spectrogram.
            self._spectrogram.set_detection(
                start_time,
                end_time,
                detection_label=info,
                context_detections=context_detections,
            )
            self._needs_seek_before_play = not auto_play
            start_ms = self._seek_to_start()
            if auto_play:
                self._player.play()
                self._arm_stop_timer(from_ms=start_ms)
                self.playbackStarted.emit(file_path)
            elif self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self._player.stop()
            return

        if same_file_loading:
            # Same file is still loading: update intent so _on_media_status_changed
            # plays (or not) when LoadedMedia fires — no second setSource needed.
            self._pending_auto_play = auto_play
            self._bounded = auto_play
            self._needs_seek_before_play = not auto_play
            self._spectrogram.set_detection(
                start_time,
                end_time,
                detection_label=info,
                context_detections=context_detections,
            )
            if auto_play:
                self.playbackStarted.emit(file_path)
            return

        # Different file or not yet loaded: full reload.
        self._spectrogram.set_audio(
            file_path,
            start_time,
            end_time,
            detection_label=info,
            context_detections=context_detections,
        )

        self._pending_load_file = file_path
        self._pending_auto_play = auto_play
        self._load_in_progress = True
        self._needs_seek_before_play = not auto_play

        if self._player.mediaStatus() == QMediaPlayer.MediaStatus.NoMedia:
            self._begin_pending_load()
        else:
            # Trigger NoMedia; _on_media_status_changed issues the actual load.
            self._player.setSource(QUrl())

        if auto_play:
            self.playbackStarted.emit(file_path)

    def _begin_pending_load(self) -> None:
        file_path = self._pending_load_file
        if file_path is None:
            return
        self._pending_load_file = None
        self._player.setSource(QUrl.fromLocalFile(file_path))

    def _on_media_status_changed(self, status: QMediaPlayer.MediaStatus) -> None:
        # NoMedia -> queue the actual load
        if self._pending_load_file is not None and status == QMediaPlayer.MediaStatus.NoMedia:
            self._begin_pending_load()
            return

        # Loaded/EndOfMedia -> seek to detection start and optionally play
        # (only when we initiated the load ourselves)
        if self._load_in_progress and status in _LOADED_STATUSES:
            self._load_in_progress = False
            start_ms = self._seek_to_start()

            if self._pending_auto_play:
                self._pending_auto_play = False
                self._needs_seek_before_play = False
                self._player.play()
                if self._bounded:
                    self._arm_stop_timer(from_ms=start_ms)
            elif self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                # prepare(): stop any still-playing clip so the player is seeked but idle
                self._player.stop()

    def seek_to_file_start(self) -> None:
        """Seek to the very beginning of the audio file (position 0)."""
        if self._load_in_progress or self._current_file is None:
            return
        self._stop_timer.stop()
        self._player.setPosition(0)

    def jump_to_detection(self) -> None:
        """Seek to the detection start (same as the ↩ button)."""
        if self._load_in_progress or self._current_file is None:
            return
        self._stop_timer.stop()
        start_ms = self._seek_to_start()
        if self._bounded and self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._arm_stop_timer(from_ms=start_ms)

    def _on_stop_timer(self) -> None:
        self._player.pause()
        self.playbackStopped.emit()

    def _on_position_changed(self, pos_ms: int) -> None:
        duration = self._player.duration()
        self._time_label.setText(f"{_ms_to_str(pos_ms)} / {_ms_to_str(duration)}")
        self._spectrogram.set_position(pos_ms, duration)

    def _on_duration_changed(self, duration: int) -> None:
        self._time_label.setText(f"0:00 / {_ms_to_str(duration)}")

    def _on_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        self._play_btn.setText("⏸" if state == QMediaPlayer.PlaybackState.PlayingState else "▶")

    def _on_seeking(self, ratio: float) -> None:
        duration = self._player.duration()
        if duration > 0:
            self._time_label.setText(f"{_ms_to_str(int(ratio * duration))} / {_ms_to_str(duration)}")

    def _on_seek_to(self, ratio: float) -> None:
        self._stop_timer.stop()
        duration = self._player.duration()
        if duration > 0:
            self._player.setPosition(int(ratio * duration))
        if self._bounded and self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._arm_stop_timer()

    def _seek_to_start(self) -> int:
        start_ms = max(0, int((self._start_time - self._pad_before_s) * 1000))
        self._player.setPosition(start_ms)
        return start_ms

    def _arm_stop_timer(self, *, from_ms: int | None = None) -> None:
        # Pass from_ms right after a seek -- _player.position() may not yet
        # reflect the just-issued setPosition() on AVFoundation.
        end_ms = int((self._end_time + self._pad_after_s) * 1000)
        cursor_ms = self._player.position() if from_ms is None else from_ms
        remaining = max(0, end_ms - cursor_ms)
        if remaining > 0:
            self._stop_timer.start(remaining)

    def _build_ui(self) -> None:
        self._spectrogram = SpectrogramWidget()
        self._spectrogram.seekTo.connect(self._on_seek_to)
        self._spectrogram.seeking.connect(self._on_seeking)

        self._play_btn = QPushButton("▶")
        self._play_btn.setFixedWidth(32)
        self._play_btn.setToolTip("Play / Pause")
        self._play_btn.clicked.connect(self.toggle)

        self._jump_btn = QPushButton("↩")
        self._jump_btn.setFixedWidth(28)
        self._jump_btn.setToolTip("Jump to detection start")
        self._jump_btn.clicked.connect(self.jump_to_detection)

        self._time_label = QLabel("0:00 / 0:00")
        self._time_label.setStyleSheet("font-size: 10px; color: #666;")

        btns_row = QHBoxLayout()
        btns_row.setContentsMargins(0, 0, 0, 0)
        btns_row.setSpacing(4)
        btns_row.addWidget(self._play_btn)
        btns_row.addWidget(self._jump_btn)

        controls = QVBoxLayout()
        controls.setContentsMargins(6, 6, 6, 6)
        controls.setSpacing(4)
        controls.addLayout(btns_row)
        controls.addWidget(self._time_label)
        controls.addStretch()

        # Main layout: narrow controls column left, spectrogram right
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        main_layout.addLayout(controls)
        main_layout.addWidget(self._spectrogram, 1)

        self.setStyleSheet("background: #f5f5f5; border-top: 1px solid #ddd;")
        self.setMinimumHeight(60)


def _ms_to_str(ms: int) -> str:
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"
