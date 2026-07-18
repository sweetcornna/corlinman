from playwright.sync_api import sync_playwright
import os
OUT="/Users/cornna/project/corlinman/audit/landing-redesign-2026-06-13/shotsfinal2"
os.makedirs(OUT,exist_ok=True)
URL="http://localhost:8731/compare.html"
with sync_playwright() as p:
    b=p.chromium.launch(headless=True)
    pg=b.new_page(viewport={"width":1512,"height":900},device_scale_factor=2)
    errs=[]; pg.on("pageerror",lambda e:errs.append(str(e)))
    pg.on("console",lambda m:errs.append(m.text) if m.type=="error" else None)
    pg.goto(URL); pg.wait_for_load_state("networkidle")
    # opening: formed (hold) then dissolved
    pg.wait_for_timeout(2200); pg.screenshot(path=f"{OUT}/open-formed.png"); print("formed")
    pg.wait_for_timeout(2600); pg.screenshot(path=f"{OUT}/open-dissolved.png"); print("dissolved")
    span=pg.evaluate("innerHeight*0.95"); ftop=pg.evaluate("document.getElementById('film').offsetTop")
    # palettes on frame 1 (mascot, reads clearly)
    pg.evaluate("yy=>window.scrollTo(0,yy)", ftop+1.95*span); pg.wait_for_timeout(2600)
    for pal in ["ink","obsidian","bone","slate","paper"]:
        pg.click(f'#palRow button[data-pal="{pal}"]'); pg.wait_for_timeout(1400)
        pg.screenshot(path=f"{OUT}/pal-{pal}.png"); print("pal",pal)
    # back to ink; ZH check on a loop glyph frame
    pg.click('#palRow button[data-pal="ink"]')
    pg.click('#langRow button[data-lang="zh"]'); pg.wait_for_timeout(300)
    starts=[0,1.9,2.9,3.9,4.9,5.9,6.9,7.9,8.9,10.2,11.2]
    pg.evaluate("yy=>window.scrollTo(0,yy)", ftop+(starts[3]+0.3)*span); pg.mouse.move(720,430,steps=3); pg.wait_for_timeout(2300)
    pg.screenshot(path=f"{OUT}/zh-remember.png"); print("zh")
    print("ERRORS:", errs[:8] if errs else "none")
    b.close()
