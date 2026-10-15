"""Generate the Unminute app icon (mic + soundwave) as a multi-resolution .ico.

Unlimit colours: a flat dark blue (#142966) tile, a white microphone and orange
(#E56600) sound bars, matching the redesigned app.

Draws everything on a 1024px supersampled canvas, then downsamples with LANCZOS
to each target size so edges stay crisp. Pillow only.
"""
import sys
from PIL import Image, ImageDraw

S = 1024  # supersample canvas size
DARK_BLUE = (20, 41, 102)        # #142966
ORANGE = (229, 102, 0, 255)      # #E56600


def rounded_mask(size, radius):
    """White rounded-rect alpha mask on black."""
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def build():
    # --- background: flat dark blue rounded tile ---
    tile = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    tile.paste(Image.new("RGB", (S, S), DARK_BLUE), (0, 0), rounded_mask(S, int(S * 0.225)))

    d = ImageDraw.Draw(tile)
    W = (255, 255, 255, 255)
    cx = S // 2

    # --- microphone capsule (pill) ---
    cap_w = int(S * 0.26)
    cap_top = int(S * 0.24)
    cap_bot = int(S * 0.55)
    x0, x1 = cx - cap_w // 2, cx + cap_w // 2
    d.rounded_rectangle([x0, cap_top, x1, cap_bot], radius=cap_w // 2, fill=W)

    # --- U-shaped holder cradling the capsule ---
    stroke = int(S * 0.035)
    pad = int(S * 0.05)
    hb = [x0 - pad, cap_top - pad, x1 + pad, cap_bot + pad]
    d.arc(hb, start=10, end=170, fill=W, width=stroke)

    # --- stand + base ---
    stand_top = hb[3] - stroke // 2
    stand_bot = int(S * 0.78)
    d.rounded_rectangle([cx - stroke // 2, stand_top, cx + stroke // 2, stand_bot],
                        radius=stroke // 2, fill=W)
    base_w = int(S * 0.20)
    d.rounded_rectangle([cx - base_w // 2, stand_bot - stroke // 2,
                         cx + base_w // 2, stand_bot + stroke // 2],
                        radius=stroke // 2, fill=W)

    # --- soundwave bars flanking the mic (audio -> text feel) ---
    bar_w = int(S * 0.028)
    gap = int(S * 0.052)
    heights = [0.10, 0.17, 0.10]  # fraction of S
    midy = int((cap_top + cap_bot) / 2)
    for side in (-1, 1):
        bx = cx + side * (cap_w // 2 + pad + int(S * 0.055))
        for i, h in enumerate(heights):
            hh = int(S * h)
            step = side * i * gap
            d.rounded_rectangle([bx + step - bar_w // 2, midy - hh // 2,
                                 bx + step + bar_w // 2, midy + hh // 2],
                                radius=bar_w // 2, fill=ORANGE)

    return tile


def main(out_ico, out_png):
    tile = build()
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [tile.resize((s, s), Image.LANCZOS) for s in sizes]
    imgs[-1].save(out_ico, format="ICO",
                  sizes=[(s, s) for s in sizes],
                  append_images=imgs[:-1])
    # preview sheet at a couple of sizes
    tile.resize((256, 256), Image.LANCZOS).save(out_png)
    print("wrote", out_ico)
    print("wrote", out_png)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
