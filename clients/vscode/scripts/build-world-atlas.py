#!/usr/bin/env python3
"""Pack the workplace's world art out of the Kenney sheets in media/world/.

The webview is a self-contained document (no asWebviewUri, no network), so the art it uses is
carried INSIDE it as one data URI. Shipping the full packs that way would triple the document for
tiles nobody draws — this script packs ONLY the cells the view names, into media/world/atlas.png,
and writes webview/workplace/atlas.ts with the URI plus a name→cell index.

Deterministic: same sheets + same manifest → byte-identical atlas.ts, so the generated file is
committed and CI never needs Pillow. Re-run after changing the manifest:

    python3 scripts/build-world-atlas.py

Every cell is 16x16. Coordinates are (column, row) on the source sheet; `indoor` and `chars`
sheets have a 1px gutter between cells, `urban` is packed tight.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image

EXT = Path(__file__).resolve().parent.parent
MEDIA = EXT / "media" / "world"
OUT_TS = EXT / "webview" / "workplace" / "atlas.ts"
CELL = 16

SHEETS = {
    "u": ("urban.png", 0),  # RPG Urban Pack — packed, no gutter
    "i": ("indoor.png", 1),  # Roguelike Indoors — 1px gutter
    "c": ("chars.png", 1),  # Roguelike Characters — 1px gutter
}

# name: (sheet, col, row). Grouped by what the view uses them AS; the tile compositions
# (which cells make a desk, a tree, a piano) live in webview/workplace/tiles.ts.
MANIFEST: dict[str, tuple[str, int, int]] = {
    # ── ground ──────────────────────────────────────────────────────────────
    "grass": ("u", 1, 1),
    "meadow": ("u", 5, 0),
    "plazaTan": ("u", 1, 4),
    "plazaGrey": ("u", 9, 4),
    "plateGrey": ("u", 9, 1),
    "plateLight": ("u", 13, 1),
    "kerbTan": ("u", 13, 4),
    "water": ("u", 13, 6),
    "road": ("u", 7, 16),
    "roadDash": ("u", 1, 16),
    "roadCross": ("u", 2, 15),
    "roadP": ("u", 10, 16),
    "rugG": ("i", 24, 5),
    "rugO": ("i", 24, 1),
    # ── walls ───────────────────────────────────────────────────────────────
    "roofO": ("u", 16, 6),
    "roofOEdge": ("u", 16, 7),
    "asphalt": ("u", 7, 17),
    "roofRed": ("u", 18, 2),
    # ── doors & windows ─────────────────────────────────────────────────────
    "doorWhite": ("u", 12, 9),
    "doorGlass": ("u", 15, 9),
    "winTall": ("u", 13, 12),
    "winArch": ("u", 12, 12),
    "winSmall": ("u", 11, 16),
    "frameBeige": ("u", 11, 12),
    "framePic": ("u", 11, 13),
    # ── indoor furniture ────────────────────────────────────────────────────
    "desk": ("i", 5, 5),
    "drafting": ("i", 4, 5),
    "dresser": ("i", 6, 5),
    "chairS": ("i", 0, 2),
    "chairN": ("i", 1, 2),
    "stool": ("i", 14, 9),
    "sofaL": ("i", 14, 6),
    "sofaR": ("i", 15, 6),
    "sofaGL": ("i", 12, 7),
    "sofaGR": ("i", 13, 7),
    "armchair": ("i", 8, 6),
    "armchairG": ("i", 8, 7),
    "benchWood": ("i", 4, 6),
    "tableRound": ("i", 7, 0),
    "table3L": ("i", 0, 0),
    "table3M": ("i", 1, 0),
    "table3R": ("i", 2, 0),
    "boardGreen": ("i", 19, 12),
    "boardBeige": ("i", 20, 12),
    "boardOrange": ("i", 19, 13),
    "panelCream": ("i", 19, 14),
    "bulletin": ("i", 17, 15),
    "plantA": ("i", 16, 0),
    "plantB": ("i", 17, 0),
    "pianoTL": ("i", 23, 8),
    "pianoTR": ("i", 24, 8),
    "pianoBL": ("i", 23, 9),
    "pianoBR": ("i", 24, 9),
    "organTL": ("i", 25, 8),
    "organTR": ("i", 26, 8),
    "organBL": ("i", 25, 9),
    "organBR": ("i", 26, 9),
    "tallBrownT": ("i", 26, 10),
    "tallBrownB": ("i", 26, 11),
    "tallGreenT": ("i", 25, 15),
    "tallGreenB": ("i", 25, 16),
    "wardTL": ("i", 23, 10),
    "wardTR": ("i", 24, 10),
    "wardBL": ("i", 23, 11),
    "wardBR": ("i", 24, 11),
    "counterKettle": ("i", 6, 12),
    "counterJars": ("i", 5, 12),
    "counterSink": ("i", 7, 12),
    "counterPaper": ("i", 8, 12),
    "counterPlain": ("i", 0, 12),
    "fridge": ("i", 11, 15),
    "washer": ("i", 11, 16),
    "stoveT": ("i", 14, 14),
    "stoveB": ("i", 14, 15),
    "vendT": ("i", 14, 12),
    "vendB": ("i", 14, 13),
    "speakerA": ("i", 12, 17),
    "speakerB": ("i", 13, 17),
    "machineGrey": ("i", 14, 17),
    "aquaL": ("i", 16, 14),
    "aquaM": ("i", 17, 14),
    "aquaR": ("i", 18, 14),
    "mirrorT": ("i", 22, 14),
    "mirrorB": ("i", 22, 15),
    "ladder": ("i", 21, 9),
    "candStand": ("i", 20, 1),
    "sconce": ("i", 21, 1),
    "fGreen": ("i", 16, 12),
    "fOrange": ("i", 17, 12),
    "fTeal": ("i", 18, 12),
    "shelfPotions": ("i", 19, 17),
    # ── outdoors ────────────────────────────────────────────────────────────
    "treeAC": ("u", 16, 8),
    "treeAT": ("u", 16, 9),
    "treeBC": ("u", 17, 8),
    "treeBT": ("u", 17, 9),
    "autumnC": ("u", 16, 11),
    "autumnT": ("u", 16, 12),
    "pine": ("u", 22, 9),
    "bushSq": ("u", 21, 8),
    "shrub": ("u", 22, 8),
    "treeTiny": ("u", 21, 10),
    "planterBush": ("u", 16, 10),
    "stoneA": ("u", 0, 10),
    "stoneB": ("u", 1, 10),
    "lamppost": ("u", 1, 6),
    "lightTall": ("u", 7, 6),
    "signGreen": ("u", 4, 6),
    "benchPark": ("u", 4, 12),
    "hydrant": ("u", 8, 10),
    "mailbox": ("u", 8, 11),
    "binGrey": ("u", 9, 10),
    "barrel": ("u", 8, 9),
    "crate": ("u", 3, 11),
    "crateFull": ("u", 6, 10),
    "stallStripe": ("u", 4, 8),
    "taxiL": ("u", 18, 15),
    "taxiR": ("u", 19, 15),
    "carRedL": ("u", 18, 17),
    "carRedR": ("u", 19, 17),
    "vanGreenT": ("u", 21, 15),
    "vanGreenB": ("u", 21, 16),
    # ── people (paper doll) ─────────────────────────────────────────────────
    "body0": ("c", 0, 0),
    "body1": ("c", 0, 1),
    "body2": ("c", 0, 2),
    "shirt1": ("c", 10, 0),
    "shirt2": ("c", 14, 0),
    "shirt3": ("c", 10, 5),
    "shirt4": ("c", 6, 5),
    "shirt5": ("c", 6, 0),
    # h6 is the teal accent. Its garment is the pack's LIGHT teal — the saturated teal at (12,0)
    # collides with shirt1's dark teal, and the first replacement, the (10,3) jacket, is an orange
    # that measured 16 RGB points from shirt5 (two pods dressed alike). (12,1) sits 84 from the
    # dark teal with the gap in LIGHTNESS, which survives every colour-vision deficiency.
    "shirt6": ("c", 12, 1),
    "shirt7": ("c", 12, 6),
    "shirtDark": ("c", 14, 5),
    "hatBrain": ("c", 30, 8),
    # hair: 5 colour blocks x 5 styles + a beard row, named hair<block><style>
    # blocks: 0 brown(19,0) 1 auburn(23,0) 2 blond(19,4) 3 grey(23,4) 4 white(19,8)
    # styles: 0 short(+0,+0) 1 round(+1,+0) 2 fringe(+2,+0) 3 long(+1,+1) 4 curl(+3,+1)
}

_HAIR_BLOCKS = [(19, 0), (23, 0), (19, 4), (23, 4), (19, 8)]
_HAIR_STYLES = [(0, 0), (1, 0), (2, 0), (1, 1), (3, 1)]
for b, (bc, br) in enumerate(_HAIR_BLOCKS):
    for s, (dc, dr) in enumerate(_HAIR_STYLES):
        MANIFEST[f"hair{b}{s}"] = ("c", bc + dc, br + dr)
    MANIFEST[f"beard{b}"] = ("c", bc, br + 3)


def cell_of(img: Image.Image, gutter: int, c: int, r: int) -> Image.Image:
    pitch = CELL + gutter
    x, y = c * pitch, r * pitch
    return img.crop((x, y, x + CELL, y + CELL))


# Cells whose art STANDS ON THE GROUND. The Kenney source reserves a fully-transparent bottom
# row in most of these (verified against the pristine sheets, byte-for-byte), so composited at a
# tile's bottom edge the silhouette stopped one source-pixel short of the floor — a flat-cut gap
# with ground peeking underneath, which is the "trees / plants are still floating" complaint in
# its eighth and final form: pure alpha data, invisible to every placement/collision probe the
# seven logic rounds added. bushSq is the one species whose cell already touches (and the one
# species never complained about) — the diff that located the mechanism.
GROUNDED = {
    "treeAT", "treeBT", "autumnT", "pine", "plantA", "plantB", "planterBush",
    "shrub", "treeTiny", "bushSq", "stoneA", "stoneB", "lamppost", "lightTall",
    "signGreen", "benchPark", "hydrant", "barrel",
}


def ground_fill(cell: Image.Image) -> Image.Image:
    """Extend a grounded cell's silhouette to the cell floor.

    A SMEAR (the lowest opaque row duplicated downward), never a shift: shifting a trunk down
    would open the same 1px gap at its canopy join instead. One row of extra trunk is invisible
    at 16px art; the ground contact is the whole point."""
    px = cell.load()
    lowest = -1
    for y in range(CELL - 1, -1, -1):
        if any(px[x, y][3] > 0 for x in range(CELL)):
            lowest = y
            break
    if lowest in (-1, CELL - 1):
        return cell  # empty, or already touching
    for y in range(lowest + 1, CELL):
        for x in range(CELL):
            px[x, y] = px[x, lowest]
    return cell


def main() -> None:
    sheets = {
        key: (Image.open(MEDIA / name).convert("RGBA"), gutter)
        for key, (name, gutter) in SHEETS.items()
    }
    names = list(MANIFEST.keys())
    cols = 12
    rows = (len(names) + cols - 1) // cols
    atlas = Image.new("RGBA", (cols * CELL, rows * CELL), (0, 0, 0, 0))
    for i, name in enumerate(names):
        sheet, c, r = MANIFEST[name]
        img, gutter = sheets[sheet]
        cell = cell_of(img, gutter, c, r)
        if name in GROUNDED:
            cell = ground_fill(cell)
        atlas.paste(cell, ((i % cols) * CELL, (i // cols) * CELL))
    # The build FAILS if a grounded cell floats — this class of defect never ships again.
    for i, name in enumerate(names):
        if name not in GROUNDED:
            continue
        tile = atlas.crop(((i % cols) * CELL, (i // cols) * CELL,
                           (i % cols + 1) * CELL, (i // cols + 1) * CELL))
        bottom = tile.load()
        assert any(bottom[x, CELL - 1][3] > 0 for x in range(CELL)), \
            f"grounded cell '{name}' has a transparent bottom row — it will render floating"
    atlas.save(MEDIA / "atlas.png", optimize=True)

    buf = io.BytesIO()
    atlas.save(buf, format="PNG", optimize=True)
    uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    entries = ",\n".join(f'  {name}: {i}' for i, name in enumerate(names))
    OUT_TS.write_text(
        "/** GENERATED by scripts/build-world-atlas.py — do not edit.\n"
        " *\n"
        " *  The world's art, packed from the CC0 Kenney sheets in media/world/ (see CREDITS.md\n"
        " *  there). One data URI so the webview document stays self-contained; one index per\n"
        " *  16x16 cell. Which cells COMPOSE a tile is tiles.ts's business.\n"
        " */\n"
        f"export const CELL = {CELL};\n"
        f"export const ATLAS_COLS = {cols};\n"
        f"export const ATLAS_ROWS = {rows};\n"
        f"export const ATLAS_URI =\n  \"{uri}\";\n"
        "export const K = {\n"
        f"{entries},\n"
        "} as const;\n"
        "export type CellName = keyof typeof K;\n",
        encoding="utf-8",
    )
    print(f"atlas: {cols}x{rows} cells, {len(names)} named, "
          f"{(MEDIA / 'atlas.png').stat().st_size} bytes png, {len(uri)} chars uri")

    # A proof sheet, for the build loop only: every cell at 4x with its name.
    try:
        from PIL import ImageDraw
        proof = Image.new("RGBA", (cols * 76, rows * 86), (56, 56, 66, 255))
        d = ImageDraw.Draw(proof)
        for i, name in enumerate(names):
            x, y = (i % cols) * 76, (i // cols) * 86
            tile = atlas.crop(((i % cols) * CELL, (i // cols) * CELL,
                               (i % cols + 1) * CELL, (i // cols + 1) * CELL))
            proof.alpha_composite(tile.resize((64, 64), Image.NEAREST), (x + 6, y + 4))
            d.text((x + 2, y + 70), name[:12], fill=(255, 255, 160, 255))
        proof.save("/tmp/atlas-proof.png")
    except Exception:
        pass


if __name__ == "__main__":
    main()
