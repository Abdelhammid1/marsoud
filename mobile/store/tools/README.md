# Store asset generators

MARSOUD-MOBILE-BRANDING (2026-08-26)

These live in the repo rather than in a scratch directory because
everything in `../` is generated output — without them the icon set and
the store graphics could never be rebuilt from the brand mark, only
hand-edited.

Run from the repository root (they use paths relative to it):

```bash
python mobile/store/tools/gen_icons.py       # launcher + Play icons
python mobile/store/tools/gen_store_art.py   # feature graphic, 9:16 sets
python mobile/store/tools/seed_demo.py       # demo employee for captures
python mobile/store/tools/seed_demo.py --undo
```

`gen_store_art.py` reads `../screenshots/` and writes `../framed/` and
`../screenshots-9x16/`, so re-capture first if the UI has changed.

Requires `pillow`, `arabic-reshaper` and `python-bidi` — the last two do
the letter-joining and right-to-left reordering that PIL does not, and
without them Arabic renders as disconnected letters in reverse.
