from playwright.sync_api import sync_playwright
import os, sys
HERE = "/Users/cornna/project/corlinman/audit/landing-redesign-2026-06-13"
OUT = os.path.join(HERE, "shotsverify")
os.makedirs(OUT, exist_ok=True)
URL = "http://localhost:8731/compare.html"
SCENES = ["home", "mascot", "receive", "remember", "reason", "act", "ask", "reply", "quote", "platform", "sixty"]
STARTS = [0, 1.9, 2.9, 3.9, 4.9, 5.9, 6.9, 7.9, 8.9, 10.2, 11.2]

def shoot(pg, tag):
    errs = []
    pg.on("pageerror", lambda e: errs.append("PAGEERR " + str(e)))
    pg.on("console", lambda m: errs.append("CONSOLE " + m.text) if m.type == "error" else None)
    pg.goto(URL); pg.wait_for_load_state("networkidle"); pg.wait_for_timeout(2500)
    span = pg.evaluate("innerHeight*0.95"); ftop = pg.evaluate("document.getElementById('film').offsetTop")
    for i, name in enumerate(SCENES):
        pg.evaluate("yy=>window.scrollTo(0,yy)", ftop + (STARTS[i] + 0.45) * span)
        pg.mouse.move(700, 430, steps=2); pg.wait_for_timeout(2200)
        pg.screenshot(path=f"{OUT}/{tag}-{i:02d}-{name}.png")
    # motion check: two shots of the act(dog) scene 600ms apart
    pg.evaluate("yy=>window.scrollTo(0,yy)", ftop + (STARTS[5] + 0.45) * span); pg.wait_for_timeout(1500)
    pg.screenshot(path=f"{OUT}/{tag}-motionA.png"); pg.wait_for_timeout(650)
    pg.screenshot(path=f"{OUT}/{tag}-motionB.png")
    print(f"[{tag}] ERRORS:", errs[:10] if errs else "none")

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1512, "height": 900}, device_scale_factor=2)
    shoot(pg, "D")
    pg.close()
    mp = b.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
    shoot(mp, "M")
    b.close()
print("done")
