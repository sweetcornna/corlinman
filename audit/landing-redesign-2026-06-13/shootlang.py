from playwright.sync_api import sync_playwright
import os

OUT = "/Users/cornna/project/corlinman/audit/landing-redesign-2026-06-13/shotslang"
os.makedirs(OUT, exist_ok=True)
URL = "http://localhost:8731/compare.html"

# frames worth comparing across languages
KEY = {1: "mascot", 2: "receive", 4: "reason", 8: "manifesto", 9: "platform", 10: "install"}

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1512, "height": 900}, device_scale_factor=2)
    errs = []
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errs.append("PAGEERR: " + str(e)))
    pg.goto(URL); pg.wait_for_load_state("networkidle"); pg.wait_for_timeout(900)

    span = pg.evaluate("innerHeight*0.95")
    filmtop = pg.evaluate("document.getElementById('film').offsetTop")

    for lang in ("en", "zh"):
        pg.click(f'#langRow button[data-lang="{lang}"]')
        pg.wait_for_timeout(250)
        for idx, label in KEY.items():
            pg.evaluate("yy => window.scrollTo(0, yy)", filmtop + idx * span + 4)
            pg.mouse.move(720, 430, steps=3)
            pg.wait_for_timeout(2200)
            pg.screenshot(path=f"{OUT}/{lang}-{idx:02d}-{label}.png")
            print(lang, idx, label)

    print("CONSOLE ERRORS:", errs[:12] if errs else "none")
    b.close()
