// Печатает tracker.html в tracker.pdf. Запуск: node render.js
const { chromium } = require('playwright');
const path = require('path');

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto('file://' + path.join(__dirname, 'tracker.html'), { waitUntil: 'networkidle' });
  await page.evaluate(() => document.fonts.ready);
  await page.pdf({
    path: path.join(__dirname, 'tracker.pdf'),
    format: 'A4', printBackground: true, preferCSSPageSize: true,
  });
  await browser.close();
  console.log('tracker.pdf ready');
})();
