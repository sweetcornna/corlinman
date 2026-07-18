from playwright.sync_api import sync_playwright
import os

OUT = "/Users/cornna/project/corlinman/audit/landing-redesign-2026-06-13/shotsfilm"
os.makedirs(OUT, exist_ok=True)
URL = "http://localhost:8731/compare.html"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1512, "height": 900}, device_scale_factor=2)
    errs = []
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errs.append("PAGEERR: " + str(e)))
    pg.goto(URL); pg.wait_for_load_state("networkidle"); pg.wait_for_timeout(900)

    nframes = pg.evaluate("document.querySelectorAll('#rail i').length")
    total = pg.evaluate("document.body.scrollHeight - innerHeight")
    print("frames:", nframes, "scrollH:", total)
    span = pg.evaluate("innerHeight*0.95")
    filmtop = pg.evaluate("document.getElementById('film').offsetTop")

    # settled shot of each narrative frame
    for i in range(nframes):
        y = filmtop + i * span + 4
        pg.evaluate("yy => window.scrollTo(0, yy)", y)
        pg.mouse.move(760 + (i % 3 - 1) * 240, 430, steps=4)  # vary rotation a bit
        pg.wait_for_timeout(2300)
        pg.screenshot(path=f"{OUT}/f{i:02d}.png")
        print("frame", i)

    # mid-morph (stop-motion) catch: jump back to a glyph frame and grab quickly
    y = filmtop + 3 * span + 4
    pg.evaluate("window.scrollTo(0,0)"); pg.wait_for_timeout(300)
    pg.evaluate("yy => window.scrollTo(0, yy)", y)
    pg.wait_for_timeout(180)
    pg.screenshot(path=f"{OUT}/morph-catch.png"); print("morph-catch")

    print("CONSOLE ERRORS:", errs[:12] if errs else "none")
    b.close()
