// Рендер HTML-слайдов в PNG 1600x1200. Запуск: node avito/src/render.js
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

(async () => {
  const root = __dirname;
  const outRoot = path.join(root, '..', 'images');
  const manifest = JSON.parse(fs.readFileSync(path.join(root, 'manifest.json'), 'utf8'));
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1600, height: 1200 }, deviceScaleFactor: 1 });
  for (const m of manifest) {
    await page.goto('file://' + m.html);
    await page.evaluate(() => document.fonts.ready);
    const out = path.join(outRoot, m.png);
    fs.mkdirSync(path.dirname(out), { recursive: true });
    await page.screenshot({ path: out });
    console.log(out);
  }
  await browser.close();
})();
