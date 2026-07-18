from playwright.sync_api import sync_playwright
import os

OUT = "/Users/cornna/project/corlinman/audit/landing-redesign-2026-06-13/shots3d"
os.makedirs(OUT, exist_ok=True)
URL = "http://localhost:8731/compare.html"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1512, "height": 900}, device_scale_factor=2)
    errs = []
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errs.append("PAGEERR: " + str(e)))
    pg.goto(URL); pg.wait_for_load_state("networkidle"); pg.wait_for_timeout(1000)

    def shot(name, frac, mx=0.5, my=0.5, settle=2200):
        pg.evaluate("f => window.scrollTo(0, f*(document.body.scrollHeight-innerHeight))", frac)
        pg.wait_for_timeout(400)
        # drive 3D rotation via pointer
        pg.mouse.move(1512*mx, 900*my, steps=6)
        pg.wait_for_timeout(settle)
        pg.screenshot(path=f"{OUT}/{name}.png")
        print("shot", name)

    # hero mascot at three rotations to show 3D parallax
    shot("hero-center", 0.00, 0.5, 0.5)
    shot("hero-rot-left", 0.00, 0.12, 0.30)
    shot("hero-rot-right", 0.00, 0.88, 0.72)
    # mid-morph capture: jump scroll then grab quickly (cloud swirling in)
    pg.evaluate("window.scrollTo(0, 0.18*(document.body.scrollHeight-innerHeight))")
    pg.wait_for_timeout(420)
    pg.screenshot(path=f"{OUT}/glyphs-morphing.png"); print("shot glyphs-morphing")
    pg.wait_for_timeout(2000)
    pg.screenshot(path=f"{OUT}/glyphs-settled.png"); print("shot glyphs-settled")
    # later sections (light end)
    shot("prompt", 0.36, 0.7, 0.4)
    shot("manifesto", 0.55, 0.5, 0.5)
    shot("wordmark", 0.74, 0.3, 0.6)
    shot("install", 0.96, 0.6, 0.45)

    print("CONSOLE ERRORS:", errs[:12] if errs else "none")
    b.close()
