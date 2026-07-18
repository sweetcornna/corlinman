from playwright.sync_api import sync_playwright
import os

OUT = "/Users/cornna/project/corlinman/audit/landing-redesign-2026-06-13/shotsfinal"
os.makedirs(OUT, exist_ok=True)
URL = "http://localhost:8731/compare.html"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1512, "height": 900}, device_scale_factor=2)
    errs = []
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errs.append("PAGEERR: " + str(e)))
    pg.goto(URL); pg.wait_for_load_state("networkidle"); pg.wait_for_timeout(300)

    # 1) dissolving opening as a TIME sequence (default palette = ink)
    for ms, tag in [(300,"a-form"),(1100,"b-cracking"),(2200,"c-dissolving"),(3600,"d-settled")]:
        pg.wait_for_timeout(ms if tag=="a-form" else (ms - prev))
        pg.screenshot(path=f"{OUT}/open-{tag}.png"); print("open", tag)
        prev = ms
    # reset to top, then scroll into frame 1 to catch the reassembly
    span = pg.evaluate("innerHeight*0.95")
    ftop = pg.evaluate("document.getElementById('film').offsetTop")
    wstart1 = pg.evaluate("(()=>{let a=[];return null})()")  # noop
    # frame 1 begins at weighted unit = hold(frame0)=1.8
    pg.evaluate("yy=>window.scrollTo(0,yy)", ftop + 1.85*span)
    pg.wait_for_timeout(500); pg.screenshot(path=f"{OUT}/reassemble-mid.png"); print("reassemble-mid")
    pg.wait_for_timeout(2200); pg.screenshot(path=f"{OUT}/reassemble-done.png"); print("reassemble-done")

    # 2) all five palettes on the OPENING (scroll back to top each time, let it form)
    for pal in ["ink","obsidian","bone","slate","paper"]:
        pg.evaluate("window.scrollTo(0,0)"); pg.wait_for_timeout(200)
        pg.click(f'#palRow button[data-pal="{pal}"]'); pg.wait_for_timeout(1300)
        pg.screenshot(path=f"{OUT}/pal-{pal}-open.png"); print("pal", pal)
    # 3) one palette across a mid frame (loop glyph) + manifesto to judge tone consistency
    pg.click('#palRow button[data-pal="ink"]')
    for idx,name in [(2,"loop"),(8,"manifesto"),(9,"install")]:
        # weighted start: frame0 hold 1.8, frame8 hold 1.3, others 1
        starts=[0,1.8,2.8,3.8,4.8,5.8,6.8,7.8,8.8,10.1,11.1]
        pg.evaluate("yy=>window.scrollTo(0,yy)", ftop + (starts[idx]+0.3)*span)
        pg.mouse.move(720,430,steps=3); pg.wait_for_timeout(2200)
        pg.screenshot(path=f"{OUT}/ink-{name}.png"); print("ink", name)

    print("CONSOLE ERRORS:", errs[:12] if errs else "none")
    b.close()
