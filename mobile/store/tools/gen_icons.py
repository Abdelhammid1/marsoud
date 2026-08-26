"""Generate the Marsoud Android launcher icon set from the brand logo.

Source: app/static/img/logo.png — a green ledger mark on transparency,
512x512 with the mark occupying 432x445 of it.

Produces three things Android/Play actually need:

  1. Legacy ic_launcher.png at five densities. Pre-API-26 launchers draw
     these as-is, so they must carry their own background — a
     transparent icon renders as a floating mark with no shape.

  2. Adaptive icon (API 26+): a 108dp canvas whose CENTRE 72dp is what
     the launcher shows, and only the centre 66dp is guaranteed visible
     under a circular mask. Art placed edge-to-edge gets cropped, so the
     mark is scaled into that safe zone rather than filling the canvas.

  3. A 512x512 listing icon for the Play Console, which requires 32-bit
     PNG with no transparency.

Background is white with the mark in its brand green, matching how the
logo is presented in the web header.
"""
import os
import sys

from PIL import Image

sys.stdout.reconfigure(encoding="utf-8")

ROOT = r"D:\Programming\marsoud\marsoud"
SRC = os.path.join(ROOT, "app", "static", "img", "logo.png")
RES = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "res")
OUT_STORE = os.path.join(
    ROOT, "mobile", "store", "play-icon-512.png")

BG = (255, 255, 255, 255)

# Legacy launcher: dp size per density bucket.
LEGACY = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
# Adaptive foreground is always a 108dp canvas.
ADAPTIVE = {"mdpi": 108, "hdpi": 162, "xhdpi": 216,
            "xxhdpi": 324, "xxxhdpi": 432}


def trimmed(src):
    """The mark with its transparent margin removed, so scaling is
    driven by the artwork and not by whatever padding the file has."""
    im = Image.open(src).convert("RGBA")
    box = im.split()[-1].getbbox()
    return im.crop(box)


def compose(mark, size, coverage, background=BG):
    """Mark centred on `size`, occupying `coverage` of the shorter edge."""
    canvas = Image.new("RGBA", (size, size), background)
    target = int(size * coverage)
    w, h = mark.size
    scale = target / max(w, h)
    m = mark.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                    Image.LANCZOS)
    canvas.paste(m, ((size - m.width) // 2, (size - m.height) // 2), m)
    return canvas


def main():
    mark = trimmed(SRC)
    print("  source mark: %sx%s (trimmed)" % mark.size)

    # 1 — legacy. 0.72 leaves a visible margin so the mark is not flush
    # against the icon edge on launchers that do not mask.
    for bucket, px in LEGACY.items():
        d = os.path.join(RES, "mipmap-" + bucket)
        os.makedirs(d, exist_ok=True)
        compose(mark, px, 0.72).save(os.path.join(d, "ic_launcher.png"))
    print("  legacy ic_launcher.png : %s densities" % len(LEGACY))

    # 2 — adaptive foreground. 0.42 of the 108dp canvas lands the mark
    # inside the 66dp safe circle (66/108 = 0.61) with margin to spare,
    # which is what keeps it uncropped under a circle, squircle or
    # teardrop mask. Transparent: the background layer is a colour.
    for bucket, px in ADAPTIVE.items():
        d = os.path.join(RES, "mipmap-" + bucket)
        os.makedirs(d, exist_ok=True)
        compose(mark, px, 0.42, background=(0, 0, 0, 0)).save(
            os.path.join(d, "ic_launcher_foreground.png"))
    print("  adaptive foreground    : %s densities" % len(ADAPTIVE))

    # 3 — Play listing icon. Must be 512x512 and must NOT have alpha;
    # the Console rejects transparency.
    os.makedirs(os.path.dirname(OUT_STORE), exist_ok=True)
    compose(mark, 512, 0.72).convert("RGB").save(OUT_STORE)
    print("  play listing icon      : %s" % os.path.relpath(OUT_STORE, ROOT))


if __name__ == "__main__":
    main()
