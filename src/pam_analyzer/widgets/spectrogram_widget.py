from __future__ import annotations

import dataclasses
import enum
import logging
import time
from collections.abc import Iterator

import numpy as np
import soundfile as sf
from PySide6.QtCore import QObject, QRect, Qt, QThread, Signal, Slot
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QToolTip, QWidget
from scipy.signal import ShortTimeFFT
from scipy.signal.windows import hann

log = logging.getLogger(__name__)

# Yellow overlay drawn over the primary detection window.
_CURRENT_REGION_COLOR = QColor(200, 200, 0, 50)
# Magenta overlay drawn over other detections from the same file.
_CONTEXT_REGION_COLOR = QColor(150, 0, 150, 50)
# White vertical line marking the current playback position.
_CURSOR_COLOR = QColor(255, 255, 255, 220)
# Greyed-out text shown when audio loading fails.
_ERROR_TEXT_COLOR = QColor("#888")

# Viridis colormap as a 256-entry RGBA lookup table.
# Generated via `matplotlib.cm.viridis(np.linspace(0, 1, 256))` so we don't pull a ~30MB matplotlib dependency for one constant.
_VIRIDIS_RGBA = np.frombuffer(
    bytes.fromhex(
        "440154ff440255ff440357ff450558ff45065aff45085bff46095cff460b5eff460c5fff460e61ff470f62ff471163ff471265ff471466ff471567ff471669ff47186aff48196bff481a6cff481c6eff481d6fff481e70ff482071ff482172ff482273ff482374ff472575ff472676ff472777ff472878ff472a79ff472b7aff472c7bff462d7cff462f7cff46307dff46317eff45327fff45347fff453580ff453681ff443781ff443982ff433a83ff433b83ff433c84ff423d84ff423e85ff424085ff414186ff414286ff404387ff404487ff3f4587ff3f4788ff3e4888ff3e4989ff3d4a89ff3d4b89ff3d4c89ff3c4d8aff3c4e8aff3b508aff3b518aff3a528bff3a538bff39548bff39558bff38568bff38578cff37588cff37598cff365a8cff365b8cff355c8cff355d8cff345e8dff345f8dff33608dff33618dff32628dff32638dff31648dff31658dff31668dff30678dff30688dff2f698dff2f6a8dff2e6b8eff2e6c8eff2e6d8eff2d6e8eff2d6f8eff2c708eff2c718eff2c728eff2b738eff2b748eff2a758eff2a768eff2a778eff29788eff29798eff287a8eff287a8eff287b8eff277c8eff277d8eff277e8eff267f8eff26808eff26818eff25828eff25838dff24848dff24858dff24868dff23878dff23888dff23898dff22898dff228a8dff228b8dff218c8dff218d8cff218e8cff208f8cff20908cff20918cff1f928cff1f938bff1f948bff1f958bff1f968bff1e978aff1e988aff1e998aff1e998aff1e9a89ff1e9b89ff1e9c89ff1e9d88ff1e9e88ff1e9f88ff1ea087ff1fa187ff1fa286ff1fa386ff20a485ff20a585ff21a685ff21a784ff22a784ff23a883ff23a982ff24aa82ff25ab81ff26ac81ff27ad80ff28ae7fff29af7fff2ab07eff2bb17dff2cb17dff2eb27cff2fb37bff30b47aff32b57aff33b679ff35b778ff36b877ff38b976ff39b976ff3bba75ff3dbb74ff3ebc73ff40bd72ff42be71ff44be70ff45bf6fff47c06eff49c16dff4bc26cff4dc26bff4fc369ff51c468ff53c567ff55c666ff57c665ff59c764ff5bc862ff5ec961ff60c960ff62ca5fff64cb5dff67cc5cff69cc5bff6bcd59ff6dce58ff70ce56ff72cf55ff74d054ff77d052ff79d151ff7cd24fff7ed24eff81d34cff83d34bff86d449ff88d547ff8bd546ff8dd644ff90d643ff92d741ff95d73fff97d83eff9ad83cff9dd93aff9fd938ffa2da37ffa5da35ffa7db33ffaadb32ffaddc30ffafdc2effb2dd2cffb5dd2bffb7dd29ffbade27ffbdde26ffbfdf24ffc2df22ffc5df21ffc7e01fffcae01effcde01dffcfe11cffd2e11bffd4e11affd7e219ffdae218ffdce218ffdfe318ffe1e318ffe4e318ffe7e419ffe9e419ffece41affeee51bfff1e51cfff3e51efff6e61ffff8e621fffae622fffde724ff"
    ),
    dtype=np.uint8,
).reshape(256, 4)

# Plasma colormap as a 256-entry RGBA lookup table.
# Generated via `matplotlib.cm.plasma(np.linspace(0, 1, 256))` so we don't pull a ~30MB matplotlib dependency for one constant.
_PLASMA_RGBA = np.frombuffer(
    bytes.fromhex(
        "0c0786ff100787ff130689ff15068aff18068bff1b068cff1d068dff1f058eff21058fff230590ff250591ff270592ff290593ff2b0594ff2d0494ff2f0495ff310496ff330497ff340498ff360498ff380499ff3a049aff3b039aff3d039bff3f039cff40039cff42039dff44039eff45039eff47029fff49029fff4a02a0ff4c02a1ff4e02a1ff4f02a2ff5101a2ff5201a3ff5401a3ff5601a3ff5701a4ff5901a4ff5a00a5ff5c00a5ff5e00a5ff5f00a6ff6100a6ff6200a6ff6400a7ff6500a7ff6700a7ff6800a7ff6a00a7ff6c00a8ff6d00a8ff6f00a8ff7000a8ff7200a8ff7300a8ff7500a8ff7601a8ff7801a8ff7901a8ff7b02a8ff7c02a7ff7e03a7ff7f03a7ff8104a7ff8204a7ff8405a6ff8506a6ff8607a6ff8807a5ff8908a5ff8b09a4ff8c0aa4ff8e0ca4ff8f0da3ff900ea3ff920fa2ff9310a1ff9511a1ff9612a0ff9713a0ff99149fff9a159eff9b179eff9d189dff9e199cff9f1a9bffa01b9bffa21c9affa31d99ffa41e98ffa51f97ffa72197ffa82296ffa92395ffaa2494ffac2593ffad2692ffae2791ffaf2890ffb02a8fffb12b8fffb22c8effb42d8dffb52e8cffb62f8bffb7308affb83289ffb93388ffba3487ffbb3586ffbc3685ffbd3784ffbe3883ffbf3982ffc03b81ffc13c80ffc23d80ffc33e7fffc43f7effc5407dffc6417cffc7427bffc8447affc94579ffca4678ffcb4777ffcc4876ffcd4975ffce4a75ffcf4b74ffd04d73ffd14e72ffd14f71ffd25070ffd3516fffd4526effd5536dffd6556dffd7566cffd7576bffd8586affd95969ffda5a68ffdb5b67ffdc5d66ffdc5e66ffdd5f65ffde6064ffdf6163ffdf6262ffe06461ffe16560ffe26660ffe3675fffe3685effe46a5dffe56b5cffe56c5bffe66d5affe76e5affe87059ffe87158ffe97257ffea7356ffea7455ffeb7654ffec7754ffec7853ffed7952ffed7b51ffee7c50ffef7d4fffef7e4efff0804dfff0814dfff1824cfff2844bfff2854afff38649fff38748fff48947fff48a47fff58b46fff58d45fff68e44fff68f43fff69142fff79241fff79341fff89540fff8963ffff8983efff9993dfff99a3cfffa9c3bfffa9d3afffa9f3afffaa039fffba238fffba337fffba436fffca635fffca735fffca934fffcaa33fffcac32fffcad31fffdaf31fffdb030fffdb22ffffdb32efffdb52dfffdb62dfffdb82cfffdb92bfffdbb2bfffdbc2afffdbe29fffdc029fffdc128fffdc328fffdc427fffdc626fffcc726fffcc926fffccb25fffccc25fffcce25fffbd024fffbd124fffbd324fffad524fffad624fffad824fff9d924fff9db24fff8dd24fff8df24fff7e024fff7e225fff6e425fff6e525fff5e726fff5e926fff4ea26fff3ec26fff3ee26fff2f026fff2f126fff1f326fff0f525fff0f623ffeff821ff"
    ),
    dtype=np.uint8,
).reshape(256, 4)

# Inferno colormap as a 256-entry RGBA lookup table.
# Generated via `matplotlib.cm.inferno(np.linspace(0, 1, 256))` so we don't pull a ~30MB matplotlib dependency for one constant.
_INFERNO_RGBA = np.frombuffer(
    bytes.fromhex(
        "000003ff000004ff000006ff010007ff010109ff01010bff02010eff020210ff030212ff040314ff040316ff050418ff06041bff07051dff08061fff090621ff0a0723ff0b0726ff0d0828ff0e082aff0f092dff10092fff120a32ff130a34ff140b36ff160b39ff170b3bff190b3eff1a0b40ff1c0c43ff1d0c45ff1f0c47ff200c4aff220b4cff240b4eff260b50ff270b52ff290b54ff2b0a56ff2d0a58ff2e0a5aff300a5cff32095dff34095fff350960ff370961ff390962ff3b0964ff3c0965ff3e0966ff400966ff410967ff430a68ff450a69ff460a69ff480b6aff4a0b6aff4b0c6bff4d0c6bff4f0d6cff500d6cff520e6cff530e6dff550f6dff570f6dff58106dff5a116dff5b116eff5d126eff5f126eff60136eff62146eff63146eff65156eff66156eff68166eff6a176eff6b176eff6d186eff6e186eff70196eff72196dff731a6dff751b6dff761b6dff781c6dff7a1c6dff7b1d6cff7d1d6cff7e1e6cff801f6bff811f6bff83206bff85206aff86216aff88216aff892269ff8b2269ff8d2369ff8e2468ff902468ff912567ff932567ff952666ff962666ff982765ff992864ff9b2864ff9c2963ff9e2963ffa02a62ffa12b61ffa32b61ffa42c60ffa62c5fffa72d5fffa92e5effab2e5dffac2f5cffae305bffaf315bffb1315affb23259ffb43358ffb53357ffb73456ffb83556ffba3655ffbb3754ffbd3753ffbe3852ffbf3951ffc13a50ffc23b4fffc43c4effc53d4dffc73e4cffc83e4bffc93f4affcb4049ffcc4148ffcd4247ffcf4446ffd04544ffd14643ffd24742ffd44841ffd54940ffd64a3fffd74b3effd94d3dffda4e3bffdb4f3affdc5039ffdd5238ffde5337ffdf5436ffe05634ffe25733ffe35832ffe45a31ffe55b30ffe65c2effe65e2dffe75f2cffe8612bffe9622affea6428ffeb6527ffec6726ffed6825ffed6a23ffee6c22ffef6d21fff06f1ffff0701efff1721dfff2741cfff2751afff37719fff37918fff47a16fff57c15fff57e14fff68012fff68111fff78310fff7850efff8870dfff8880cfff88a0bfff98c09fff98e08fff99008fffa9107fffa9306fffa9506fffa9706fffb9906fffb9b06fffb9d06fffb9e07fffba007fffba208fffba40afffba60bfffba80dfffbaa0efffbac10fffbae12fffbb014fffbb116fffbb318fffbb51afffbb71cfffbb91efffabb21fffabd23fffabf25fffac128fff9c32afff9c52cfff9c72ffff8c931fff8cb34fff8cd37fff7cf3afff7d13cfff6d33ffff6d542fff5d745fff5d948fff4db4bfff4dc4ffff3de52fff3e056fff3e259fff2e45dfff2e660fff1e864fff1e968fff1eb6cfff1ed70fff1ee74fff1f079fff1f27dfff2f381fff2f485fff3f689fff4f78dfff5f891fff6fa95fff7fb99fff9fc9dfffafda0fffcfea4ff"
    ),
    dtype=np.uint8,
).reshape(256, 4)


class Colormap(enum.Enum):
    VIRIDIS = "viridis"
    PLASMA = "plasma"
    INFERNO = "inferno"


# numpy arrays are unhashable, so the LUT can't live inside the enum as a value.
_COLORMAP_LUTS: dict[Colormap, np.ndarray] = {
    Colormap.VIRIDIS: _VIRIDIS_RGBA,
    Colormap.PLASMA: _PLASMA_RGBA,
    Colormap.INFERNO: _INFERNO_RGBA,
}


@dataclasses.dataclass
class _RenderSpectroRequest:
    path: str
    colormap: Colormap
    seq: int  # monotonic counter; stale results are dropped by the widget


class _RenderSpectroWorker(QObject):
    """Runs spectrogram computation on a background QThread.

    Emits ready() with the raw RGBA numpy array (not a QPixmap — Qt forbids
    creating QPixmap outside the GUI thread). The widget converts to QPixmap
    on receipt in the main thread.
    """

    ready = Signal(object, float, int)   # (rgba: np.ndarray, total_s, seq)
    failed = Signal(str, int)            # (error_msg, seq)

    def __init__(self) -> None:
        super().__init__()
        self._sft: ShortTimeFFT | None = None
        self._sft_sr: int = 0

    @Slot(object)
    def process(self, req: _RenderSpectroRequest) -> None:
        try:
            t0 = time.perf_counter()
            data, sr = sf.read(req.path, dtype="float32")
            if data.ndim > 1:
                data = data.mean(axis=1)
            total_s = len(data) / sr
            t1 = time.perf_counter()

            if self._sft is None or self._sft_sr != sr:
                self._sft = ShortTimeFFT(hann(1024), hop=512, fs=sr)
                self._sft_sr = sr
            Sx_db = 10.0 * np.log10(self._sft.spectrogram(data) + 1e-10)
            t2 = time.perf_counter()

            vmax = float(Sx_db.max())
            vmin = vmax - 80
            normalized = np.clip((Sx_db - vmin) / (vmax - vmin), 0.0, 1.0)
            # Flip frequency rows in indices before LUT lookup so low
            # frequencies sit at the bottom (QImage row 0 is at the top).
            # Fancy indexing always returns a fresh contiguous array, so no
            # ascontiguousarray copy is needed.
            indices = (normalized * 255).astype(np.uint8)
            rgba = _COLORMAP_LUTS[req.colormap][indices[::-1]]
            t3 = time.perf_counter()

            log.debug(
                "spectrogram stages ms — read: %.1f  fft: %.1f  lut: %.1f  total: %.1f",
                (t1 - t0) * 1e3,
                (t2 - t1) * 1e3,
                (t3 - t2) * 1e3,
                (t3 - t0) * 1e3,
            )
            self.ready.emit(rgba, total_s, req.seq)
        except Exception as e:
            log.exception("Failed to render spectrogram for %s", req.path)
            self.failed.emit(str(e), req.seq)


class SpectrogramWidget(QWidget):
    """Static spectrogram image with a draggable playback position marker.

    Renders the full audio file as a spectrogram (scipy ShortTimeFFT to viridis
    colormap to QPixmap). Overlays:
      - yellow region: the primary detection window
      - magenta regions: other detections from the same filtered dataset
      - white vertical line: current playback position (updated via set_position)

    Clicking or dragging moves the marker visually; seekTo is emitted on release
    so the caller can seek the media player.
    """

    seekTo = Signal(float)   # ratio 0..1, emitted on mouse release or click
    seeking = Signal(float)  # ratio 0..1, emitted continuously while dragging

    # Routed to _RenderSpectroWorker.process via a QueuedConnection (cross-thread).
    _request_render = Signal(object)

    def __init__(self, parent: QWidget | None = None, colormap: Colormap = Colormap.VIRIDIS) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._error: str | None = None
        self._position_ratio: float = 0.0
        self._detect_start_s: float = 0.0
        self._detect_end_s: float = 0.0
        self._dragging = False
        self._detect_label: str = ""
        self._total_s: float = 0.0
        # (start_s, end_s, label) for other detections in the same file
        self._context_detections: list[tuple[float, float, str]] = []
        self._colormap = colormap
        self._audio_path: str = ""
        self._render_seq: int = 0  # incremented per request; stale responses are discarded

        self._worker = _RenderSpectroWorker()
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._request_render.connect(self._worker.process)
        self._worker.ready.connect(self._on_render_ready)
        self._worker.failed.connect(self._on_render_failed)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._stop_thread)

        self.setMinimumHeight(30)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.setMouseTracking(True)

    def _stop_thread(self) -> None:
        self._thread.quit()
        self._thread.wait()

    def set_colormap(self, colormap: Colormap) -> None:
        """Change the colormap and re-render if audio is already loaded."""
        if self._colormap != colormap:
            self._colormap = colormap
            if self._audio_path:
                self._submit_render()

    def set_audio(
        self,
        file_path: str,
        detection_start_s: float,
        detection_end_s: float,
        *,
        detection_label: str = "",
        context_detections: list[tuple[float, float, str]] | None = None,
    ) -> None:
        self._audio_path = file_path
        self._position_ratio = 0.0
        self._pixmap = None
        self._error = None
        self._total_s = 0.0
        self._detect_label = detection_label
        self._detect_start_s = detection_start_s
        self._detect_end_s = detection_end_s
        self._context_detections = list(context_detections or [])
        self.update()  # immediately repaint as blank while computing
        self._submit_render()

    def _submit_render(self) -> None:
        self._render_seq += 1
        self._request_render.emit(_RenderSpectroRequest(self._audio_path, self._colormap, self._render_seq))

    @Slot(object, float, int)
    def _on_render_ready(self, rgba: np.ndarray, total_s: float, seq: int) -> None:
        if seq != self._render_seq:
            return  # superseded by a newer request
        h, w = rgba.shape[:2]
        self._pixmap = QPixmap.fromImage(QImage(rgba.data, w, h, QImage.Format_RGBA8888).copy())
        self._total_s = total_s
        self.update()

    @Slot(str, int)
    def _on_render_failed(self, error_msg: str, seq: int) -> None:
        if seq != self._render_seq:
            return
        self._error = error_msg
        self.update()

    def set_position(self, pos_ms: int, total_ms: int) -> None:
        if total_ms > 0 and not self._dragging:
            self._position_ratio = max(0.0, min(1.0, pos_ms / total_ms))
            self.update()

    def set_detection(
        self,
        detection_start_s: float,
        detection_end_s: float,
        *,
        detection_label: str = "",
        context_detections: list[tuple[float, float, str]] | None = None,
    ) -> None:
        """Update detection window and context overlays without reloading the spectrogram image."""
        self._detect_start_s = detection_start_s
        self._detect_end_s = detection_end_s
        self._detect_label = detection_label
        self._context_detections = list(context_detections or [])
        self._position_ratio = 0.0
        self.update()

    def update_context_detections(self, context_detections: list[tuple[float, float, str]]) -> None:
        self._context_detections = list(context_detections)
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._move_marker(event.position().x())

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            self._move_marker(event.position().x())
            return
        if self._pixmap is not None and self.width() > 0 and self._total_s > 0:
            seconds = (event.position().x() / self.width()) * self._total_s
            for start_s, end_s, label in self._regions_at_cursor_priority():
                if label and start_s <= seconds <= end_s:
                    QToolTip.showText(event.globalPosition().toPoint(), label, self)
                    return
        QToolTip.hideText()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self._move_marker(event.position().x())
            self.seekTo.emit(self._position_ratio)

    def _move_marker(self, x: float) -> None:
        self._position_ratio = max(0.0, min(1.0, x / self.width()))
        self.update()
        if self._dragging:
            self.seeking.emit(self._position_ratio)

    def _regions_at_cursor_priority(self) -> Iterator[tuple[float, float, str]]:
        """Yield detection regions in front-to-back order for hit-testing.

        The primary detection is drawn on top of context detections, so it
        wins ties when a context window overlaps it.
        """
        yield self._detect_start_s, self._detect_end_s, self._detect_label
        yield from self._context_detections

    def paintEvent(self, _event) -> None:  # noqa: N802
        if self._pixmap is None:
            painter = QPainter(self)
            painter.setPen(_ERROR_TEXT_COLOR)
            if self._error:
                text = f"Cannot load audio: {self._error}"
            elif self._audio_path:
                text = "Loading spectrogram…"
            else:
                text = ""
            if text:
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, text)
            painter.end()
            return
        painter = QPainter(self)
        painter.drawPixmap(self.rect(), self._pixmap)
        w, h = self.width(), self.height()

        def x_of(seconds: float) -> int:
            return int(seconds / self._total_s * w) if self._total_s > 0 else 0

        # Other detections in this file
        for start_s, end_s, _ in self._context_detections:
            x1, x2 = x_of(start_s), x_of(end_s)
            painter.fillRect(QRect(x1, 0, max(1, x2 - x1), h), _CONTEXT_REGION_COLOR)
        # Current detection region
        x1, x2 = x_of(self._detect_start_s), x_of(self._detect_end_s)
        painter.fillRect(QRect(x1, 0, max(1, x2 - x1), h), _CURRENT_REGION_COLOR)
        # Playback marker
        x = int(self._position_ratio * w)
        painter.setPen(QPen(_CURSOR_COLOR, 2))
        painter.drawLine(x, 0, x, h)
        painter.end()
