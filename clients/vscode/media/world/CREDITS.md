# World art credits

The workplace view's tiles and characters are built from three asset packs by
**Kenney** (www.kenney.nl), all released under **Creative Commons Zero (CC0)** —
free for personal, educational and commercial use, no permission required.
Each pack's own license text sits beside its sheet in this folder.

| sheet        | pack                                          | license                            |
| ------------ | --------------------------------------------- | ---------------------------------- |
| `urban.png`  | RPG Urban Pack (tilemap_packed)               | `LICENSE-rpg-urban-pack.txt`       |
| `indoor.png` | Roguelike Indoors (roguelikeIndoor)           | `LICENSE-roguelike-indoors.txt`    |
| `chars.png`  | Roguelike Characters (roguelikeChar)          | `LICENSE-roguelike-characters.txt` |

Credit is not required by CC0; it is given because it is right. Support Kenney at
https://kenney.nl/donate.

`atlas.png` + `webview/workplace/atlas.ts` are generated from these sheets by
`scripts/build-world-atlas.py` — only the tiles the view actually uses, packed and
inlined as a data URI so the webview stays self-contained.
