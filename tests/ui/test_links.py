"""Unit tests for the QLabel link helper."""

from pam_analyzer.ui.links import link


def test_link_escapes_href_and_text():
    html = link('a"b', "~/R&D <old>")
    assert 'href="a&quot;b"' in html
    assert ">~/R&amp;D &lt;old&gt;</a>" in html


def test_link_has_no_underline():
    assert 'style="text-decoration:none"' in link("#", "x")
