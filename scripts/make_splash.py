#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pillow"]
# ///
"""Generate the PyInstaller splash screen PNG for PAM Analyzer from app_icon.svg.

Requires Inkscape on PATH for SVG→PNG rendering.

Usage:
    uv run --script scripts/make_splash.py [output_path]
"""

import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SCRIPT_DIR = Path(__file__).parent
ICON_SVG = SCRIPT_DIR.parent / 'src' / 'pam_analyzer' / 'static' / 'app_icon.svg'
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else SCRIPT_DIR / 'splash.png'

W, H = 480, 280
BG = (15, 17, 26)  # near-black navy
TEAL = (45, 212, 191)  # Tailwind teal-400
FG = (220, 225, 235)  # off-white title
SUBTEXT = (120, 130, 150)  # muted subtitle


def find_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        '/System/Library/Fonts/Helvetica.ttc',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
        'arial.ttf',
        'Arial.ttf',
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


icon_size = 120

# Use pre-built app.png if available (required on Windows where svg2png.sh
# cannot run). Generate it on macOS/Linux with:
#   scripts/svg2png.sh src/pam_analyzer/static/app_icon.svg scripts/app.png 256 256
prebuilt_png = SCRIPT_DIR / 'app.png'
if prebuilt_png.exists():
    icon_img = Image.open(prebuilt_png).convert('RGBA').resize((icon_size, icon_size))
else:
    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
        tmp_png = Path(tmp.name)
    try:
        subprocess.run(
            [
                str(SCRIPT_DIR / 'svg2png.sh'),
                str(ICON_SVG),
                str(tmp_png),
                str(icon_size),
                str(icon_size),
            ],
            check=True,
            capture_output=True,
        )
        icon_img = Image.open(tmp_png).convert('RGBA')
    finally:
        tmp_png.unlink(missing_ok=True)

img = Image.new('RGB', (W, H), BG)
draw = ImageDraw.Draw(img)

icon_x = (W - icon_size) // 2
icon_y = 28
img.paste(icon_img, (icon_x, icon_y), mask=icon_img)

title_y = icon_y + icon_size + 28
draw.text((W // 2, title_y), 'PAM Analyzer', fill=FG, font=find_font(26), anchor='mm')
draw.text(
    (W // 2, title_y + 26),
    'Passive Acoustic Monitoring for Bird Species Detection',
    fill=SUBTEXT,
    font=find_font(12),
    anchor='mm',
)

# Thin teal line at bottom (leaves room for pyi_splash text overlay)
draw.rectangle([0, H - 3, W, H], fill=TEAL)

OUT.parent.mkdir(parents=True, exist_ok=True)
img.save(OUT)
print(f'Splash written to {OUT}  ({W}x{H}px)')
