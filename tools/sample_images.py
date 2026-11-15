#!/usr/bin/env python3
"""Draw the labelled sample images the managed apartments start with (web/homes/sample-*.jpg).

    .venv/bin/python tools/sample_images.py

They are drawings, not photographs of any real place, and each carries a "SAMPLE IMAGE" label, so
nobody mistakes one for the apartment. Replace them in the host dashboard (Apartments, Photographs).
"""
import math, os, random

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "homes")
W, H = 1920, 1080

SKIES = {   # top, middle, horizon, sun glow, building tint, window light
    "sample-1": ((38, 30, 78), (196, 96, 96), (255, 190, 120), (255, 214, 150), (34, 26, 48), (255, 206, 130)),    # dusk
    "sample-2": ((92, 150, 214), (176, 208, 236), (250, 226, 196), (255, 244, 214), (58, 66, 88), (255, 232, 180)),  # morning
    "sample-3": ((10, 16, 44), (30, 48, 104), (96, 92, 150), (190, 170, 220), (12, 14, 30), (255, 196, 110)),        # night
    "sample-4": ((120, 86, 110), (232, 150, 96), (255, 214, 140), (255, 236, 180), (60, 36, 40), (255, 220, 150)),   # golden hour
}


def font(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/Library/Fonts/Arial Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"):
        if os.path.isfile(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size=size)


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def sky(top, mid, hor):
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / (H * 0.72)
        c = lerp(top, mid, t / 0.6) if t < 0.6 else lerp(mid, hor, min(1, (t - 0.6) / 0.4))
        d.line([(0, y), (W, y)], fill=c)
    return img


def glow(img, xy, r, colour, strength=0.75):
    g = Image.new("L", (W, H), 0)
    ImageDraw.Draw(g).ellipse([xy[0] - r, xy[1] - r, xy[0] + r, xy[1] + r], fill=int(255 * strength))
    g = g.filter(ImageFilter.GaussianBlur(r * 0.55))
    return Image.composite(Image.new("RGB", (W, H), colour), img, g)


def dome(d, cx, base, r, fill):
    d.pieslice([cx - r, base - r * 1.25, cx + r, base + r * 0.75], 180, 360, fill=fill)
    d.polygon([(cx - r * 0.12, base - r * 1.2), (cx, base - r * 1.75), (cx + r * 0.12, base - r * 1.2)], fill=fill)


def minaret(d, x, base, h, w, fill):
    d.rectangle([x - w / 2, base - h, x + w / 2, base], fill=fill)
    d.rectangle([x - w, base - h * 0.78, x + w, base - h * 0.74], fill=fill)
    d.pieslice([x - w, base - h - w * 1.6, x + w, base - h + w * 0.6], 180, 360, fill=fill)
    d.line([(x, base - h - w * 1.5), (x, base - h - w * 3)], fill=fill, width=3)


def skyline(img, rng, tint, hor):
    d = ImageDraw.Draw(img)
    far = lerp(tint, hor, 0.55)
    base = int(H * 0.66)
    x = -40
    while x < W + 40:   # far, hazy blocks
        w, h = rng.randint(50, 130), rng.randint(40, 150)
        d.rectangle([x, base - h, x + w, base + 40], fill=far)
        x += w + rng.randint(-10, 18)
    mid = lerp(tint, hor, 0.3)
    cx = int(W * 0.31)   # a mosque outline in the middle distance, Mughal in spirit
    d.rectangle([cx - 250, base - 70, cx + 250, base + 40], fill=mid)
    dome(d, cx, base - 70, 92, mid)
    dome(d, cx - 160, base - 70, 58, mid)
    dome(d, cx + 160, base - 70, 58, mid)
    for mx in (cx - 300, cx + 300):
        minaret(d, mx, base + 40, 300, 22, mid)
    return base


def towers(img, rng, tint, light, base):
    d = ImageDraw.Draw(img)
    x = -30
    while x < W + 30:
        w, h = rng.randint(120, 240), rng.randint(150, 420)
        if W * 0.16 < x < W * 0.47:
            h = rng.randint(90, 170)   # keep the mosque in view
        top = base + 70 - h
        d.rectangle([x, top, x + w, H], fill=tint)
        for wy in range(top + 18, int(H * 0.86), 30):   # windows, some lit
            for wx in range(x + 14, x + w - 18, 26):
                if rng.random() < 0.34:
                    d.rectangle([wx, wy, wx + 12, wy + 16], fill=lerp(light, tint, rng.random() * 0.45))
        x += w + rng.randint(6, 40)


def terrace(img, tint):
    d = ImageDraw.Draw(img)
    floor = lerp(tint, (0, 0, 0), 0.35)
    d.rectangle([0, int(H * 0.9), W, H], fill=floor)
    d.rectangle([0, int(H * 0.855), W, int(H * 0.868)], fill=lerp(tint, (255, 255, 255), 0.12))
    for x in range(0, W, 46):
        d.line([(x, int(H * 0.868)), (x, int(H * 0.9))], fill=lerp(tint, (255, 255, 255), 0.08), width=5)


def label(img, sub):
    d = ImageDraw.Draw(img, "RGBA")
    big, small = font(28), font(17)
    t = "SAMPLE IMAGE"
    w = d.textlength(t, font=big)
    w2 = d.textlength(sub, font=small)
    box = [70, 56, 70 + max(w, w2) + 44, 56 + 88]
    d.rounded_rectangle(box, 18, fill=(0, 0, 0, 150), outline=(255, 255, 255, 190), width=2)
    d.text((92, 68), t, font=big, fill=(255, 255, 255, 255))
    d.text((92, 106), sub, font=small, fill=(255, 255, 255, 215))


def save(img, name, glass=True):
    img.save(os.path.join(OUT, name + ".jpg"), quality=86, optimize=True, progressive=True)
    if glass:
        g = ImageEnhance.Brightness(img.filter(ImageFilter.GaussianBlur(16))).enhance(1.3)
        ImageEnhance.Color(g).enhance(1.25).save(os.path.join(OUT, name + "-glass.jpg"), quality=80)


def hero(name, seed):
    top, mid, hor, sun, tint, light = SKIES[name]
    rng = random.Random(seed)
    img = sky(top, mid, hor)
    img = glow(img, (int(W * (0.62 + 0.1 * math.sin(seed))), int(H * 0.6)), 330, sun)
    if name == "sample-3":   # stars
        d = ImageDraw.Draw(img)
        for _ in range(260):
            x, y = rng.randint(0, W), rng.randint(0, int(H * 0.5))
            d.point((x, y), fill=lerp((255, 255, 255), top, rng.random() * 0.6))
    base = skyline(img, rng, tint, hor)
    img = img.filter(ImageFilter.GaussianBlur(1.2))
    towers(img, rng, tint, light, base)
    terrace(img, tint)
    label(img, "Replace with your apartment photograph")
    save(img, name)


def inside():
    """A plain room: wall, window with evening light, sofa, lamp."""
    img = Image.new("RGB", (W, H), (58, 50, 54))
    d = ImageDraw.Draw(img)
    for y in range(H):
        d.line([(0, y), (W, y)], fill=lerp((86, 74, 76), (40, 34, 40), y / H))
    d.rectangle([0, int(H * 0.74), W, H], fill=(52, 40, 36))
    d.rectangle([1120, 150, 1700, 640], fill=(250, 196, 130))
    d.rectangle([1120, 150, 1700, 640], outline=(30, 26, 30), width=14)
    d.line([(1410, 150), (1410, 640)], fill=(30, 26, 30), width=10)
    img = glow(img, (1410, 400), 420, (255, 206, 150), 0.45)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([220, 560, 980, 800], 40, fill=(96, 110, 120))
    d.rounded_rectangle([220, 470, 980, 640], 40, fill=(110, 126, 136))
    for x in (300, 640):
        d.rounded_rectangle([x, 520, x + 260, 650], 30, fill=(214, 190, 150))
    d.rectangle([1010, 420, 1022, 800], fill=(30, 26, 30))
    d.polygon([(950, 420), (1082, 420), (1050, 320), (982, 320)], fill=(255, 226, 170))
    label(img, "Replace with a photograph of the room")
    save(img, "sample-inside", glass=False)


def area():
    """A street at dusk for 'Around here'."""
    rng = random.Random(9)
    top, mid, hor, sun, tint, light = SKIES["sample-4"]
    img = sky(top, mid, hor)
    img = glow(img, (W // 2, int(H * 0.62)), 380, sun)
    d = ImageDraw.Draw(img)
    for side in (0, 1):   # two rows of shop fronts running to the middle
        for i in range(7):
            t0, t1 = i / 7, (i + 1) / 7
            x0 = int(t0 * W * 0.44) if side == 0 else int(W - t0 * W * 0.44)
            x1 = int(t1 * W * 0.44) if side == 0 else int(W - t1 * W * 0.44)
            y0 = int(H * (0.18 + 0.36 * t0))
            c = lerp(tint, hor, 0.1 + 0.5 * t0)
            d.polygon([(x0, y0), (x1, int(H * (0.18 + 0.36 * t1))), (x1, int(H * (0.92 - 0.3 * t1))), (x0, int(H * (0.92 - 0.3 * t0)))], fill=c)
            for k in range(3):
                if rng.random() < 0.8:
                    yy = y0 + 60 + k * int(90 * (1 - t0))
                    xa, xb = sorted((x0 + (x1 - x0) * 0.25, x0 + (x1 - x0) * 0.75))
                    d.rectangle([xa, yy, xb, yy + int(40 * (1 - t0)) + 6], fill=lerp(light, c, rng.random() * 0.4))
    d.polygon([(0, H), (W, H), (int(W * 0.56), int(H * 0.62)), (int(W * 0.44), int(H * 0.62))], fill=lerp(tint, (0, 0, 0), 0.2))
    label(img, "Replace with a photograph of the area")
    save(img, "sample-area", glass=False)


if __name__ == "__main__":
    for i, n in enumerate(SKIES):
        hero(n, i + 3)
    inside()
    area()
    print("Wrote the sample images to", OUT)
