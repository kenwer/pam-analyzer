"""Rich-text links for QLabel."""

import html


def link(href: str, text: str) -> str:
    """Return an escaped HTML anchor for a QLabel, without the default underline.

    A style sheet on the label does not reach the anchor inside its rich text,
    so the underline is switched off on the tag itself.
    """
    return f'<a href="{html.escape(href)}" style="text-decoration:none">{html.escape(text)}</a>'
