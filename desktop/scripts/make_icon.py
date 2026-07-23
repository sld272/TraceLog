"""Generate a desktop application icon from TraceLog's source PNG.

macOS app icons are a rounded-rect tile on a transparent 1024 canvas
(Apple's grid: an 824x824 tile centered, corner radius ~185). The brand
glyph is transparent-background artwork, so we composite it onto a white
rounded-rect tile here instead of shipping a transparent icon. The same
composition is exported as a multi-resolution ICO for Windows.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

CANVAS = 1024
TILE = 824
CORNER_RADIUS = 185
# Glyph size relative to the tile, applied after cropping the source to
# its alpha bounding box — matches the proportions of docs/images/logo.png
# (glyph fills ~3/4 of the frame).
GLYPH_RATIO = 0.75


def main(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)

    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    tile_origin = (CANVAS - TILE) // 2
    tile = Image.new("RGBA", (TILE, TILE), (0, 0, 0, 0))
    ImageDraw.Draw(tile).rounded_rectangle(
        (0, 0, TILE - 1, TILE - 1),
        radius=CORNER_RADIUS,
        fill=(255, 255, 255, 255),
    )
    canvas.alpha_composite(tile, (tile_origin, tile_origin))

    with Image.open(source) as image:
        glyph = image.convert("RGBA")
    bbox = glyph.getbbox()
    if bbox:
        glyph = glyph.crop(bbox)
    glyph_size = int(TILE * GLYPH_RATIO)
    glyph.thumbnail((glyph_size, glyph_size), Image.LANCZOS)
    glyph_origin = (
        (CANVAS - glyph.width) // 2,
        (CANVAS - glyph.height) // 2,
    )
    canvas.alpha_composite(glyph, glyph_origin)

    suffix = destination.suffix.lower()
    if suffix == ".icns":
        canvas.save(destination, format="ICNS")
        return
    if suffix == ".ico":
        canvas.save(
            destination,
            format="ICO",
            sizes=[
                (16, 16),
                (24, 24),
                (32, 32),
                (48, 48),
                (64, 64),
                (128, 128),
                (256, 256),
            ],
        )
        return
    raise ValueError(f"Unsupported desktop icon format: {destination.suffix}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
