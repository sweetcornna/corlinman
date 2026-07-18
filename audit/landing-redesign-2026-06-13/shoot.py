from playwright.sync_api import sync_playwright
import os

OUT = "/Users/cornna/project/corlinman/audit/landing-redesign-2026-06-13/shots"
os.makedirs(OUT, exist_ok=True)
URL = "http://localhost:8731/compare.html"

# (label, mood, scroll fraction of page)
SHOTS = [
    ("journey-00-hero",     "journey", 0.00),
    ("journey-01-loop",     "journey", 0.18),
    ("journey-02-prompt",   "journey", 0.36),
    ("journey-03-manifesto","journey", 0.55),
    ("journey-04-wordmark", "journey", 0.74),
    ("journey-05-install",  "journey", 0.95),
    ("light-hero",          "light",   0.00),
    ("light-loop",          "light",   0.18),
    ("light-wordmark",      "light",   0.74),
    ("dark-hero",           "dark",    0.00),
    ("dark-loop",           "dark",    0.18),
    ("dark-prompt",         "dark",    0.36),
]

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1512, "height": 900}, device_scale_factor=2)
    errs = []
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errs.append("PAGEERR: " + str(e)))
    pg.goto(URL)
    pg.wait_for_load_state("networkidle")
    pg.wait_for_timeout(800)

    cur_mood = "journey"
    for label, mood, frac in SHOTS:
        if mood != cur_mood:
            pg.click(f'#moodRow button[data-mood="{mood}"]')
            cur_mood = mood
            pg.wait_for_timeout(200)
        pg.evaluate(
            "f => window.scrollTo(0, f*(document.body.scrollHeight - innerHeight))", frac
        )
        # let particles settle into the section shape
        pg.wait_for_timeout(2200)
        pg.screenshot(path=f"{OUT}/{label}.png")
        print("shot", label)

    print("CONSOLE ERRORS:", errs[:10] if errs else "none")
    b.close()
