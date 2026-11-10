from pam_analyzer.ui.panels.model_status_widget import ModelStatusWidget

_MB = 1024 * 1024


def _widget(qtbot) -> ModelStatusWidget:
    w = ModelStatusWidget()
    qtbot.addWidget(w)
    return w


def test_shows_not_downloaded_with_size(qtbot) -> None:
    w = _widget(qtbot)
    w.show_model(430_000_000)
    assert w.status_label.text() == "Model not downloaded (411 MB)"
    assert w.progress_bar.isHidden()


def test_shows_downloaded(qtbot) -> None:
    w = _widget(qtbot)
    w.show_model(0)
    assert w.status_label.text() == ""


def test_show_download_reveals_progress(qtbot) -> None:
    w = _widget(qtbot)
    w.show_model(430_000_000)
    w.show_download(120 * _MB, 410 * _MB)
    assert not w.progress_bar.isHidden()
    assert (w.progress_bar.value(), w.progress_bar.maximum()) == (120, 410)
    assert w.status_label.text() == "Downloading model 120 / 410 MB"


def test_show_model_hides_progress_again(qtbot) -> None:
    w = _widget(qtbot)
    w.show_download(1, 2)
    w.show_model(0)
    assert w.progress_bar.isHidden()


def test_not_downloaded_offers_a_download_link(qtbot) -> None:
    w = _widget(qtbot)
    w.show_model(430_000_000)
    assert not w.action_label.isHidden()
    assert ">Download now</a>" in w.action_label.text()
    with qtbot.waitSignal(w.downloadRequested, timeout=1000):
        w.action_label.linkActivated.emit("download")


def test_downloaded_offers_no_link(qtbot) -> None:
    w = _widget(qtbot)
    w.show_model(0)
    assert w.action_label.isHidden()


def test_background_download_offers_cancel(qtbot) -> None:
    w = _widget(qtbot)
    w.show_download(1, 2)
    assert ">Cancel</a>" in w.action_label.text()
    with qtbot.waitSignal(w.cancelRequested, timeout=1000):
        w.action_label.linkActivated.emit("cancel")


def test_download_without_a_total_is_waiting_its_turn(qtbot) -> None:
    """Installs run one at a time, so a second engine waits for the first."""
    w = _widget(qtbot)
    w.show_download(0, 0)
    assert w.status_label.text() == "Waiting to download"
    assert ">Cancel</a>" in w.action_label.text()
