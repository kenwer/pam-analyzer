"""Non-modal toast notifications for short-running actions.

Wraps pyqt-toast-notification so callers get a one-line helper and the
version-specific link handling stays in a single place.
"""

import html
from collections.abc import Callable, Sequence
from pathlib import Path

from pyqttoast import Toast, ToastPreset
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import QApplication, QWidget

Links = Sequence[tuple[str, Callable[[], None]]]


def _show_toast(
    parent: QWidget,
    title: str,
    text: str,
    preset: ToastPreset,
    *,
    links: Links = (),
    duration: int | None = None,
) -> Toast:
    """Build and show a toast with the given preset.

    The default toast position is bottom-right (pyqttoast's own default), so it
    does not steal focus from the main window.

    When links are given, the text label is switched to rich text and the links
    are appended after the text. Clicking one invokes its callback. pyqttoast
    1.3.3 has no public rich-text API, so the internal text label is accessed via
    its name-mangled attribute. If a future version renames it, the toast falls
    back to plain text instead of raising.

    Args:
        parent: Widget the toast is parented to.
        title: Bold title line.
        text: Body text.
        preset: pyqttoast style preset (success, error, warning, ...).
        links: (visible text, callback) pairs, shown in order after the text.
        duration: Auto-dismiss time in milliseconds. None keeps pyqttoast's
            default (5000). 0 disables auto-dismiss.

    Returns:
        The shown Toast instance.
    """
    toast = Toast(parent)
    if duration is not None:
        toast.setDuration(duration)
    toast.setTitle(title)
    toast.applyPreset(preset)

    # pyqttoast defaults to Arial 9pt, which is small and non-native. Use the app
    # font family with a floor so the toast stays legible on every platform.
    base_font = QApplication.font()
    text_size = max(base_font.pointSize(), 13)
    text_font = QFont(base_font)
    text_font.setPointSize(text_size)
    toast.setTextFont(text_font)
    title_font = QFont(base_font)
    title_font.setPointSize(text_size + 1)
    title_font.setBold(True)
    toast.setTitleFont(title_font)

    label = getattr(toast, "_Toast__text_label", None)
    if links and label is not None:
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        # Keep openExternalLinks off so the click reaches us via linkActivated
        # instead of the label trying to open the href (the link's index) itself.
        callbacks = [callback for _text, callback in links]
        label.linkActivated.connect(lambda href: callbacks[int(href)]())
        # Rich text reads '&' and '<' in paths as markup and drops newlines.
        body = html.escape(text).replace("\n", "<br>")
        anchors = " · ".join(f'<a href="{i}">{html.escape(t)}</a>' for i, (t, _cb) in enumerate(links))
        toast.setText(f"{body} {anchors}")
    else:
        toast.setText(text)

    toast.show()
    return toast


def show_success_toast(
    parent: QWidget,
    title: str,
    text: str,
    *,
    links: Links = (),
    duration: int | None = None,
) -> Toast:
    """Show a success toast, optionally ending in clickable links."""
    return _show_toast(parent, title, text, ToastPreset.SUCCESS, links=links, duration=duration)


def show_info_toast(
    parent: QWidget,
    title: str,
    text: str,
    *,
    duration: int | None = None,
) -> Toast:
    """Show a non-blocking information toast."""
    return _show_toast(parent, title, text, ToastPreset.INFORMATION, duration=duration)


def show_error_toast(
    parent: QWidget,
    title: str,
    text: str,
    *,
    duration: int | None = None,
) -> Toast:
    """Show a non-blocking error toast."""
    return _show_toast(parent, title, text, ToastPreset.ERROR, duration=duration)


def show_warning_toast(
    parent: QWidget,
    title: str,
    text: str,
    *,
    links: Links = (),
    duration: int | None = None,
) -> Toast:
    """Show a non-blocking warning toast, optionally ending in clickable links."""
    return _show_toast(parent, title, text, ToastPreset.WARNING, links=links, duration=duration)


def open_in_file_manager(folder: Path) -> None:
    """Open *folder* in the platform file manager."""
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
