"""Regenerate the PWA icons (server/static/icons/) with the green-blue-cyan scheme.

Run from the repo root:  python3 scripts/gen_icons.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "server" / "static" / "icons"
W = 512
C1 = (72, 219, 128)    # green
C2 = (64, 156, 240)    # blue (cyan appears mid-gradient)


def gradient(w: int, h: int):
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            t = (x + y) / (w + h)
            px[x, y] = tuple(int(a + (b - a) * t) for a, b in zip(C1, C2))
    return img


def rounded(img: Image.Image, radius: int) -> Image.Image:
    mask = Image.new("L", img.size, 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, img.size[0] - 1, img.size[1] - 1], radius=radius, fill=255)
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def ring(base: Image.Image, size: int, stroke: int) -> Image.Image:
    d = ImageDraw.Draw(base)
    cx = cy = size // 2
    r = int(size * 0.27)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 255, 255, 255), width=stroke)
    dr = int(size * 0.075)
    dx, dy = cx + int(r * 0.62), cy - int(r * 0.62)
    d.ellipse([dx - dr, dy - dr, dx + dr, dy + dr], fill=(255, 255, 255, 255))
    return base


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    # Plain icons (rounded corners, transparent outside)
    icon = rounded(gradient(W, W), int(W * 0.22)).convert("RGBA")
    ring(icon, W, int(W * 0.055))
    icon.resize((192, 192), Image.LANCZOS).save(OUT / "icon-192.png")
    icon.save(OUT / "icon-512.png")

    # Maskable icons (full-bleed, content inside the circular safe zone)
    m = gradient(W, W).convert("RGBA")
    ring(m, W, int(W * 0.05))
    m.resize((192, 192), Image.LANCZOS).save(OUT / "icon-maskable-192.png")
    m.save(OUT / "icon-maskable-512.png")
    print(f"icons written to {OUT}")


if __name__ == "__main__":
    main()
