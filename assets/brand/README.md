# Brand assets: a0-plugin-device-sync

PROPOSAL: the owner signs off logos. Nothing here is final until then.

Part of the Patchbay family (see `DESIGN.md`). Hand-written SVG, no raster source,
no stock icons, no generated imagery. PNG files are previews rendered from the SVGs.

## Construction (64 x 64 grid)

- Frame: rounded square, outer edge 4..60, outer corner radius 8, stroke 6 (6 units), no fill.
- Corner pin: a 14 x 14 filled block in the signal colour at the bottom-right
  (x 46..60, y 46..60), continuing the frame outline. Every family mark has it.
- Glyph box: 28 x 28 at x 16..44, y 16..44. Strokes are ink, 4 units wide, butt caps,
  mitre joins, centrelines inside 19..41. Straight segments only.

## This plugin's glyph

Two opposing arrows: right on the upper line, left on the lower line. Sync here is
symmetric (push, pull, bidirectional), so the mark shows traffic both ways. Two strokes
and two open arrowheads keep it readable at 16 px.

## Palette

| Token | Light | Dark |
|---|---|---|
| canvas | #F4F6F7 | #0C1116 |
| ink | #0F1720 | #E8EEF1 |
| ink-muted | #4A5663 | #9AA7B2 |
| signal (pin) | #00766E | #3FD3C4 |

## Files

`logo.svg`, `logo-dark.svg` (mark), `wordmark.svg`, `wordmark-dark.svg` (mark plus
name as outlines), `social-card.svg`, `social-card-dark.svg` (1280 x 640),
`family-sheet.png` (all five marks at 16, 32, 128 px), PNG previews of each SVG.

## Rebuild

```bash
python3 assets/brand/family-build.py device-sync assets/brand
resvg assets/brand/wordmark.svg assets/brand/wordmark.png   # repeat per SVG
oxipng -o 4 assets/brand/*.png
```
