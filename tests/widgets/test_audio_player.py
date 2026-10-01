"""Load sequencing of AudioPlayerPanel against Qt's FFmpeg backend.

The backend is replaced by a stand-in that replays the status sequence traced
on a real QMediaPlayer with QT_MEDIA_BACKEND=ffmpeg, the backend Windows uses.
A real player cannot hold a load open for the length of a mouse click, and
that window is where the panel went wrong.
"""

from PySide6.QtCore import QUrl
from PySide6.QtMultimedia import QMediaPlayer

from pam_analyzer.widgets.audio_player import AudioPlayerPanel

_Status = QMediaPlayer.MediaStatus
_State = QMediaPlayer.PlaybackState


class _FfmpegLikePlayer:
    def __init__(self, panel: AudioPlayerPanel) -> None:
        self._panel = panel
        self._status = _Status.NoMedia
        self._state = _State.StoppedState
        self._play_requested = False
        self.position_ms = 0

    def mediaStatus(self) -> QMediaPlayer.MediaStatus:  # noqa: N802 (Qt API)
        return self._status

    def playbackState(self) -> QMediaPlayer.PlaybackState:  # noqa: N802 (Qt API)
        return self._state

    def position(self) -> int:
        return self.position_ms

    def setSource(self, url: QUrl) -> None:  # noqa: N802 (Qt API)
        # setSource() stops the old media first, and that stop reports LoadedMedia.
        if self._status not in (_Status.NoMedia, _Status.LoadingMedia):
            self._state = _State.StoppedState
            self._set_status(_Status.LoadedMedia)
        self._set_status(_Status.NoMedia if url.isEmpty() else _Status.LoadingMedia)

    def setPosition(self, ms: int) -> None:  # noqa: N802 (Qt API)
        # Seeks issued while loading are dropped.
        if self._status != _Status.LoadingMedia:
            self.position_ms = ms

    def play(self) -> None:
        if self._status == _Status.LoadingMedia:
            self._play_requested = True
            return
        self._state = _State.PlayingState
        self._set_status(_Status.BufferedMedia)

    def pause(self) -> None:
        self._state = _State.PausedState

    def stop(self) -> None:
        self._state = _State.StoppedState
        self.position_ms = 0

    def finish_loading(self) -> None:
        self.position_ms = 0
        self._set_status(_Status.LoadedMedia)
        if self._play_requested:
            self._play_requested = False
            self.play()

    def _set_status(self, status: QMediaPlayer.MediaStatus) -> None:
        if status != self._status:
            self._status = status
            self._panel._on_media_status_changed(status)


def _panel_with_player(qtbot, monkeypatch) -> tuple[AudioPlayerPanel, _FfmpegLikePlayer]:
    panel = AudioPlayerPanel()
    qtbot.addWidget(panel)
    monkeypatch.setattr(panel._spectrogram, "set_audio", lambda *args, **kwargs: None)
    player = _FfmpegLikePlayer(panel)
    panel._player = player
    return panel, player


def test_note_click_on_new_file_plays_from_detection_start(qtbot, monkeypatch):
    panel, player = _panel_with_player(qtbot, monkeypatch)
    panel.play_detection("a.flac", 10.0, 13.0)
    player.finish_loading()
    assert player.position_ms == 10_000

    # Mouse press selects the row, release clicks the Note column, both before the load ends.
    panel.prepare("b.flac", 60.0, 63.0)
    panel.play_detection("b.flac", 60.0, 63.0)
    player.finish_loading()

    assert player.playbackState() == _State.PlayingState
    assert player.position_ms == 60_000


def test_prepare_on_new_file_seeks_to_detection_start(qtbot, monkeypatch):
    panel, player = _panel_with_player(qtbot, monkeypatch)
    panel.play_detection("a.flac", 10.0, 13.0)
    player.finish_loading()

    panel.prepare("b.flac", 60.0, 63.0)
    player.finish_loading()

    assert player.playbackState() == _State.StoppedState
    assert player.position_ms == 60_000
