// Bas-relief Navy preview — screenshots login + admin shell.
import { chromium } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";

const OUT_DIR = resolve(process.cwd(), "..", "_design");
mkdirSync(OUT_DIR, { recursive: true });

const browser = await chromium.launch();
try {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();

  for (const target of [
    { url: "http://localhost:3000/login?theme=dark", file: "relief-login.png" },
    { url: "http://localhost:3000/?theme=dark", file: "relief-admin.png" },
  ]) {
    await page
      .goto(target.url, { waitUntil: "domcontentloaded", timeout: 20_000 })
      .catch(() => {});
    await page.waitForTimeout(2500);
    const outPath = resolve(OUT_DIR, target.file);
    await page.screenshot({ path: outPath, fullPage: false }).catch(() => {});
    console.log("wrote", outPath);
  }
  await ctx.close();
} finally {
  await browser.close();
}
