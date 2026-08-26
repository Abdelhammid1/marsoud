# -*- coding: utf-8 -*-
"""Build the Play Store graphics that are not screenshots.

Two artefacts:

  · feature graphic — 1024x500, mandatory for every listing, shown at the
    top of the store page. No alpha channel allowed.

  · framed screenshots ("mockups") — each raw device capture drawn inside
    a phone bezel on a branded panel with a caption. These are what the
    user asked for alongside the plain captures; Play accepts either, and
    framed ones read better in the carousel.

Arabic needs two passes PIL will not do on its own: arabic_reshaper joins
the letters into their contextual forms, then python-bidi reorders the
run right-to-left. Skipping either renders Arabic as disconnected
letters in reverse, which is the usual way this goes wrong.
"""
import os
import sys

import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont

sys.stdout.reconfigure(encoding="utf-8")

ROOT = r"D:\Programming\marsoud\marsoud"
LOGO = os.path.join(ROOT, "app", "static", "img", "logo.png")
OUT = os.path.join(ROOT, "mobile", "store")
SHOTS = os.path.join(OUT, "screenshots")
FRAMED = os.path.join(OUT, "framed")

GREEN = (21, 155, 84)
INK = (17, 32, 28)
MUTED = (104, 122, 114)
PANEL = (243, 248, 245)

F_BOLD = r"C:\Windows\Fonts\arialbd.ttf"
F_REG = r"C:\Windows\Fonts\arial.ttf"


def ar(text):
    """Shape + reorder an Arabic string for PIL."""
    return get_display(arabic_reshaper.reshape(text))


def font(path, size):
    return ImageFont.truetype(path, size)


def centred(draw, xy, text, f, fill, anchor="mm"):
    draw.text(xy, text, font=f, fill=fill, anchor=anchor)


def logo_mark():
    im = Image.open(LOGO).convert("RGBA")
    return im.crop(im.split()[-1].getbbox())


def feature_graphic():
    """1024x500. Play flattens any alpha to black, so build on RGB."""
    W, H = 1024, 500
    im = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(im)

    # A soft green band on the right keeps the composition from being a
    # bare white rectangle without competing with the mark.
    for x in range(W):
        t = max(0.0, (W * 0.55 - x) / (W * 0.55))
        v = int(255 - 16 * t)
        g = int(255 - 6 * t)
        d.line([(x, 0), (x, H)], fill=(v, g, int(255 - 12 * t)))
    d.rectangle([0, H - 6, W, H], fill=GREEN)

    # RTL layout: the mark leads on the LEFT and the wordmark is
    # right-aligned, which is the reading order for an Arabic-first
    # product. Mirrored from the usual LTR arrangement on purpose.
    mark = logo_mark()
    s = 210
    mw = int(mark.width * s / mark.height)
    m = mark.resize((mw, s), Image.LANCZOS)
    im.paste(m, (95, (H - s) // 2), m)

    right = W - 80
    d.text((right, 150), ar("مرصود"), font=font(F_BOLD, 92), fill=INK,
           anchor="ra")
    d.text((right, 262), "Marsoud", font=font(F_BOLD, 44), fill=GREEN,
           anchor="ra")
    d.text((right, 332), ar("تطبيق الموظف — حضور ومهام وعملاء"),
           font=font(F_REG, 32), fill=MUTED, anchor="ra")
    p = os.path.join(OUT, "feature-graphic-1024x500.png")
    im.save(p)
    print("  feature graphic  %s" % os.path.relpath(p, ROOT))


PHONE_W, PHONE_H = 1080, 2400


def frame_one(shot_path, caption, out_path):
    """Draw one capture in a bezel on a branded panel, caption on top."""
    CW, CH = 1242, 2208           # a standard Play phone canvas
    im = Image.new("RGB", (CW, CH), PANEL)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, CW, 8], fill=GREEN)

    d.text((CW // 2, 118), ar(caption), font=font(F_BOLD, 58), fill=INK,
           anchor="mm")

    shot = Image.open(shot_path).convert("RGB")
    target_h = CH - 330
    target_w = int(shot.width * target_h / shot.height)
    if target_w > CW - 190:
        target_w = CW - 190
        target_h = int(shot.height * target_w / shot.width)
    shot = shot.resize((target_w, target_h), Image.LANCZOS)

    x = (CW - target_w) // 2
    y = 210
    pad = 14
    # bezel
    d.rounded_rectangle([x - pad, y - pad, x + target_w + pad,
                         y + target_h + pad], radius=46, fill=(24, 28, 26))
    im.paste(shot, (x, y))
    im.save(out_path)


def frame_all():
    if not os.path.isdir(SHOTS):
        print("  (no screenshots yet — run the capture step first)")
        return
    os.makedirs(FRAMED, exist_ok=True)
    caps = {}
    cap_file = os.path.join(SHOTS, "captions.txt")
    if os.path.exists(cap_file):
        for line in open(cap_file, encoding="utf-8"):
            if "|" in line:
                k, v = line.strip().split("|", 1)
                caps[k.strip()] = v.strip()
    n = 0
    for f in sorted(os.listdir(SHOTS)):
        if not f.endswith(".png"):
            continue
        cap = caps.get(f, "")
        frame_one(os.path.join(SHOTS, f), cap,
                  os.path.join(FRAMED, f))
        n += 1
    print("  framed shots     %s" % n)


def _main():
    os.makedirs(OUT, exist_ok=True)
    feature_graphic()
    frame_all()
    plain_9x16()


def plain_9x16():
    """A plain, uncaptioned set that still satisfies Play's phone
    screenshot ratio.

    The emulator captures at 1080x2400 — that is 9:20, and Play wants
    9:16 for phone screenshots (equivalently: the long side no more than
    twice the short side). Uploading the raw file gets it rejected on
    dimensions, so the capture is scaled to fit and centred on a 9:16
    canvas in the app's own background colour. Nothing is cropped: the
    whole screen stays visible, it just gains side margins.
    """
    out = os.path.join(OUT, "screenshots-9x16")
    os.makedirs(out, exist_ok=True)
    CW, CH = 1242, 2208
    n = 0
    for f in sorted(os.listdir(SHOTS)):
        if not f.endswith(".png"):
            continue
        shot = Image.open(os.path.join(SHOTS, f)).convert("RGB")
        h = CH
        w = int(shot.width * h / shot.height)
        shot = shot.resize((w, h), Image.LANCZOS)
        canvas = Image.new("RGB", (CW, CH), (245, 249, 252))
        canvas.paste(shot, ((CW - w) // 2, 0))
        canvas.save(os.path.join(out, f))
        n += 1
    print("  plain 9:16       %s" % n)


if __name__ == "__main__":
    _main()
