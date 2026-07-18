#!/usr/bin/env python3
"""Generate the hand-drawn ink stop-motion menagerie via gpt-image-2.

Each animal -> a 2x2 grid of 4 sequential motion frames (same character).
Plants/objects -> single illustrations. Output: raw 1024 PNGs in gen2/.
Idempotent: skips a subject if its PNG already exists.
"""
import base64, json, os, sys, time, urllib.request, urllib.error

API = "https://cdnapi.cornna.xyz/v1/images/generations"
KEY = "sk-3ee936d329e572a4b599ccffe1a585af4b1dc3c9a3802b979fc5dfbf090b6c8c"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gen2")
os.makedirs(OUT, exist_ok=True)

STYLE = ("vintage pen-and-ink engraving illustration, fine crosshatch shading, "
         "warm dark sepia-brown ink, hand-drawn storybook woodcut feel, "
         "on a pure flat solid white background, strong contrast between the dark ink "
         "subject and the bright white background, no text, no letters, no numbers, "
         "no frame borders, no captions, high detail")

def grid(animal, motion, frames):
    f = "; ".join(f"frame {i+1}: {d}" for i, d in enumerate(frames))
    return (f"Four hand-drawn ink illustrations of the SAME single {animal} arranged "
            f"in a clean 2x2 grid with generous empty space between them and no dividing "
            f"lines, showing four sequential frames of a {motion} animation loop -> {f}. "
            f"Identical character design, identical size and identical ink style in every "
            f"frame, each {animal} centered in its quadrant, full body, side profile. {STYLE}.")

def single(subject, desc):
    return (f"A single hand-drawn ink illustration of {desc}, centered, full shape, "
            f"large in frame. {STYLE}.")

JOBS = {
 # ---- animals: 2x2 grid, 4 motion frames ----
 "bird":   grid("small songbird in flight", "wing-flap flight cycle",
                ["wings raised high above the body",
                 "wings spread wide and level, gliding",
                 "wings swept fully down below the body",
                 "wings half-folded mid-beat, body gliding forward"]),
 "dog":    grid("running dog seen from the side", "four-legged gallop run cycle",
                ["front and back legs stretched far apart, fully extended gallop",
                 "all four legs gathered tightly under the body, airborne",
                 "front legs reaching forward, back legs pushing off the ground",
                 "mid-stride, legs crossing under the chest"]),
 "butterfly": grid("butterfly", "wing-flapping flight cycle",
                ["wings fully open and flat, top view",
                 "wings tilted three-quarters open",
                 "wings nearly closed together, side view",
                 "wings opening again three-quarters"]),
 "deer":   grid("graceful deer", "leaping bound cycle",
                ["crouched low gathering for a jump",
                 "launching upward, front legs lifting",
                 "fully airborne, all legs extended in a long leap",
                 "landing, front hooves reaching down"]),
 "cat":    grid("cat", "walking gait cycle",
                ["right front and left back legs forward",
                 "passing pose, legs gathered under",
                 "left front and right back legs forward",
                 "passing pose, tail raised"]),
 "rabbit": grid("rabbit", "hopping cycle",
                ["crouched low, haunches coiled",
                 "pushing off, body stretching upward",
                 "fully airborne, body stretched in a long hop, ears back",
                 "landing, front paws reaching down"]),
 "fish":   grid("fish", "swimming cycle",
                ["body straight, tail centered",
                 "body curving, tail swept to the left",
                 "body straight, tail centered",
                 "body curving, tail swept to the right"]),
 # ---- plants: single ----
 "flower": single("flower", "a tall slender wildflower with a single bloom on top and a few leaves on the stem"),
 "fern":   single("fern", "a single curling fern frond with delicate fronds"),
 "vine":   single("vine", "a climbing vine with heart-shaped leaves trailing along a thin stem"),
 "grass":  single("grass", "a tuft of tall wild grass blades"),
 # ---- objects: single ----
 "gear":   single("gear", "a single mechanical gear cog wheel with chunky teeth and a round hole in the centre"),
 "brain":  single("brain", "a brain in side profile with wavy folds and a small brain stem"),
}

def call(prompt):
    body = json.dumps({
        "model": "gpt-image-2", "prompt": prompt, "n": 1,
        "size": "1024x1024", "quality": "high",
        "output_format": "png",
    }).encode()
    req = urllib.request.Request(API, data=body, method="POST", headers={
        "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)

def main():
    only = sys.argv[1:]
    todo = [(k, v) for k, v in JOBS.items() if (not only or k in only)]
    for name, prompt in todo:
        dst = os.path.join(OUT, f"{name}.png")
        if os.path.exists(dst):
            print(f"skip {name} (exists)"); continue
        for attempt in range(3):
            try:
                t = time.time()
                d = call(prompt)
                b64 = d["data"][0]["b64_json"]
                open(dst, "wb").write(base64.b64decode(b64))
                print(f"OK   {name}  {len(b64)//1024}KB  {time.time()-t:.1f}s")
                break
            except urllib.error.HTTPError as e:
                print(f"HTTP {e.code} {name} attempt {attempt+1}: {e.read()[:200]}")
                time.sleep(3)
            except Exception as e:
                print(f"ERR  {name} attempt {attempt+1}: {e}")
                time.sleep(3)
        else:
            print(f"FAIL {name}")

if __name__ == "__main__":
    main()
