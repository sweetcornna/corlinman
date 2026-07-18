#!/usr/bin/env python3
"""Slice gen2/ ink sheets into clean transparent stop-motion frames.

Animals  -> 2x2 grid sliced into 4 frames each.
Plants/objects -> single frame.
Per frame: key white->transparent, autocrop to content, downscale, pad.
Writes assets/cr/<name>_<i>.png and assets/cr/manifest.json
"""
import json, os
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "gen2")
DST = os.path.join(HERE, "assets", "cr")
os.makedirs(DST, exist_ok=True)

ANIM = ["bird", "dog", "butterfly", "deer", "cat", "rabbit", "fish"]   # 2x2 -> 4 frames
SINGLE = ["flower", "fern", "vine", "grass", "gear", "brain"]          # 1 frame
MAXSIDE = 360          # longest side of an exported frame
PAD = 8                # transparent padding around content

def key_white(im):
    """Dark ink on white -> RGBA with white keyed to transparent (soft)."""
    a = np.asarray(im.convert("RGB")).astype(np.float32)
    lum = 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]
    # >=246 fully transparent, <=200 fully opaque, linear ramp between
    alpha = np.clip((246.0 - lum) / (246.0 - 200.0), 0.0, 1.0) * 255.0
    out = np.dstack([a, alpha]).astype(np.uint8)
    return Image.fromarray(out, "RGBA")

def autocrop(im, thresh=12):
    arr = np.asarray(im)
    ys, xs = np.where(arr[..., 3] > thresh)
    if len(xs) == 0:
        return None
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    return im.crop((int(x0), int(y0), int(x1), int(y1)))

def finish(im):
    im = autocrop(im)
    if im is None:
        return None
    w, h = im.size
    s = MAXSIDE / max(w, h)
    if s < 1:
        im = im.resize((max(1, round(w * s)), max(1, round(h * s))), Image.LANCZOS)
    w, h = im.size
    canvas = Image.new("RGBA", (w + 2 * PAD, h + 2 * PAD), (0, 0, 0, 0))
    canvas.paste(im, (PAD, PAD), im)
    return canvas

def cells_2x2(im):
    W, H = im.size
    mx, my = W // 2, H // 2
    return [im.crop(b) for b in
            [(0, 0, mx, my), (mx, 0, W, my), (0, my, mx, H), (mx, my, W, H)]]

def main():
    manifest = {}
    for name in ANIM + SINGLE:
        src = os.path.join(SRC, f"{name}.png")
        if not os.path.exists(src):
            print(f"-- missing {name}.png, skip"); continue
        keyed = key_white(Image.open(src))
        frames = cells_2x2(keyed) if name in ANIM else [keyed]
        out = []
        for i, fr in enumerate(frames):
            done = finish(fr)
            if done is None:
                print(f"!! {name} frame {i} empty"); continue
            fn = f"{name}_{i}.png"
            done.save(os.path.join(DST, fn))
            out.append({"f": fn, "w": done.size[0], "h": done.size[1]})
        manifest[name] = out
        print(f"{name}: {len(out)} frame(s)  {[ (o['w'],o['h']) for o in out]}")
    json.dump(manifest, open(os.path.join(DST, "manifest.json"), "w"), indent=0)
    print("wrote manifest:", os.path.join(DST, "manifest.json"))

if __name__ == "__main__":
    main()
