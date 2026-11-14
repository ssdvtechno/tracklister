"""Draws assets/icon.png (1024px) and derives icon.icns (macOS) and icon.ico (Windows)."""

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent / "assets"
S = 4096  # supersample, then downscale for smooth edges


def draw() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    pad = int(S * 0.09)
    # vertical green gradient squircle
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    top, bottom = (38, 226, 110), (14, 150, 70)
    for y in range(S):
        t = y / S
        gd.line([(0, y), (S, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([pad, pad, S - pad, S - pad], radius=int(S * 0.2), fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    ink = (6, 43, 20, 255)
    # tracklist lines with a play-head dot on each
    x0, w, h = int(S * 0.24), int(S * 0.4), int(S * 0.075)
    for i, frac in enumerate((1.0, 0.78, 0.55)):
        y = int(S * 0.33) + i * int(S * 0.15)
        d.rounded_rectangle([x0 + int(S * 0.13), y, x0 + int(S * 0.13) + int(w * frac), y + h], radius=h // 2, fill=ink)
        d.ellipse([x0 - h // 4, y - h // 4, x0 + h + h // 4, y + h + h // 4], fill=(255, 255, 255, 255))
    return img.resize((1024, 1024), Image.LANCZOS)


def main():
    ROOT.mkdir(exist_ok=True)
    icon = draw()
    icon.save(ROOT / "icon.png")
    icon.save(ROOT / "icon.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    if sys.platform == "darwin":
        iconset = ROOT / "icon.iconset"
        iconset.mkdir(exist_ok=True)
        for s in (16, 32, 128, 256, 512):
            icon.resize((s, s), Image.LANCZOS).save(iconset / f"icon_{s}x{s}.png")
            icon.resize((s * 2, s * 2), Image.LANCZOS).save(iconset / f"icon_{s}x{s}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(ROOT / "icon.icns")], check=True)
        for f in iconset.iterdir():
            f.unlink()
        iconset.rmdir()


if __name__ == "__main__":
    main()
