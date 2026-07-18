from playwright.sync_api import sync_playwright
import os
OUT = "/Users/cornna/project/corlinman/audit/landing-redesign-2026-06-13/shotsopen"
os.makedirs(OUT, exist_ok=True)
URL = "http://localhost:8731/compare.html"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1512, "height": 900}, device_scale_factor=2)
    errs=[]; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type=="error" else None)
    pg.goto(URL); pg.wait_for_load_state("networkidle")
    # fine time sequence of the opening dissolve (default ink)
    marks=[800,2000,2800,3600,4600,6000]; prev=0
    for ms in marks:
        pg.wait_for_timeout(ms-prev); prev=ms
        pg.screenshot(path=f"{OUT}/t{ms:04d}.png"); print("t",ms)
    # reassembly into frame 1
    span=pg.evaluate("innerHeight*0.95"); ftop=pg.evaluate("document.getElementById('film').offsetTop")
    pg.evaluate("yy=>window.scrollTo(0,yy)", ftop+1.95*span); pg.wait_for_timeout(700)
    pg.screenshot(path=f"{OUT}/reassemble-mid.png"); pg.wait_for_timeout(2200)
    pg.screenshot(path=f"{OUT}/reassemble-done.png"); print("reassemble")
    print("ERRORS:", errs[:8] if errs else "none")
    b.close()
