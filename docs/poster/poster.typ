// PAM Analyzer office-door poster.
// Build with scripts/build_poster.py (renders qr.png, then compiles this).
// Paper: A3 portrait. To switch to A4, change the `paper` below; the layout
// uses relative sizing so it reflows, but re-check the screenshot scale.

#let accent = rgb("#20c4b4")      // teal sampled from the app icon
#let accent-dark = rgb("#0f8f83")
#let ink = rgb("#1c2b2a")
#let muted = rgb("#5c6b6a")
#let panel = rgb("#f2f8f7")
#let hairline = rgb("#d5e2e0")

#set page(
  paper: "a3",
  margin: (x: 22mm, y: 20mm),
)
#set text(
  font: ("Helvetica Neue", "Helvetica", "Arial"),
  fill: ink,
  size: 11pt,
)
#set par(leading: 0.62em)

// A labelled feature block; hero: true renders the wider, bolder variant
// used for the single headline capability.
#let feature(title, body, hero: false) = block(
  fill: panel,
  inset: if hero { 14pt } else { 11pt },
  radius: 6pt,
  width: 100%,
  stroke: if hero { (left: 3pt + accent) } else { none },
  stack(
    spacing: if hero { 6pt } else { 4pt },
    text(weight: "bold", size: if hero { 14pt } else { 12pt }, fill: accent-dark, title),
    text(size: if hero { 11pt } else { 10.5pt }, fill: muted, body),
  ),
)

// Header
#grid(
  columns: (34mm, 1fr),
  column-gutter: 8mm,
  align: horizon,
  image("/assets/icon.png", width: 34mm),
  stack(
    spacing: 23pt,
    text(size: 40pt, weight: "extralight", fill: muted)[PAM Analyzer],
    text(size: 18pt, weight: "bold")[Manage automated bird species detection from acoustic recordings],
  ),
)

#v(4pt)
#line(length: 100%, stroke: 1.5pt + accent)
#v(6pt)

// Intro
#text(size: 12.5pt)[
  An open source desktop application designed to help researchers performing Passive Acoustic
  Monitoring (PAM). It covers the whole workflow for processing Autonomous Recording Unit (ARU)
  field recordings. The application organizes data into a hierarchical structure of projects and
  campaigns, making it easy to manage large-scale monitoring studies.
]

#v(6pt)

// Screenshot
#block(
  radius: 6pt,
  clip: true,
  image("screenshot.jpg", width: 100%),
)
#align(center, text(size: 9pt, fill: muted)[
  The Examine panel: detections with scientific, German, and English names, confidence, source
  model, and per-detection spectrogram playback.
])

#v(6pt)

// Feature grid
#feature("Project & campaign management", hero: true)[
  Organizes acoustic monitoring deployments into projects and campaigns. Campaigns hold your audio
  data for a specific location and can have as many as you want in your project. Each with its own
  species filter set by location and/or a custom species list. This keeps large, multi-site studies
  structured as they grow.
]
#v(3pt)

#grid(
  columns: (1fr, 1fr),
  gutter: 7pt,
  feature("Species detection using local AI")[
    Run the built-in BirdNET-2.4 or Google Perch-2.0 models to detect species, no cloud, no API
    keys. Run per campaign or in batch, with a configurable confidence threshold and segment
    overlap. Each model writes its own CSV per campaign, so multiple runs coexist.
  ],
  feature("Batch import from SD cards")[
    Auto-detects AudioMoth (Open Acoustic Devices) and Song Meter (Wildlife Acoustics) SD-cards
    and files them into a campaign / ARU / week layout. WAV is transcoded to lossless FLAC,
    verified against the source, with GUANO metadata carried across.
  ],
  feature("Review and annotate")[
    A sortable, filterable table of species detections with integrated spectrogram playback.
    Mark detections verified, correct a species, with keyboard shortcuts for fast passes.
  ],
  feature("Export")[
    Export filtered species detections to CSV, or extract annotated audio snippets with
    verification status, and timing embedded in the filenames.
  ],
)

#v(1fr)

// Footer: how to get it
#line(length: 100%, stroke: 0.75pt + hairline)
#v(6pt)
#grid(
  columns: (1fr, 30mm),
  column-gutter: 8mm,
  align: horizon,
  [
    #text(weight: "bold", size: 13pt)[Get it]
    #v(3pt)
    Pre-built binaries for macOS, Windows, and Linux, as well as additional information, can
    be found at: \
    #text(weight: "bold", fill: accent-dark)[github.com/kenwer/pam-analyzer]
    #v(8pt)
    #text(size: 9.5pt, fill: muted)[
      Ken Werner · ken.werner\@uni-tuebingen.de · AGPL-3.0 licensed
    ]
  ],
  align(center, stack(
    spacing: 3pt,
    image("qr.png", width: 28mm),
    text(size: 8pt, fill: muted)[scan for more info],
  )),
)
