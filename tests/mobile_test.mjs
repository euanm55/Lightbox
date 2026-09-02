// Phone-sized layout and touch gestures for index.html.
//   NODE_PATH=$(npm root -g) node tests/mobile_test.mjs [--screens DIR]
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
const require = createRequire(import.meta.url);
let chromium;
try { ({ chromium } = require('playwright')); } catch { ({ chromium } = require(path.join(process.env.NODE_PATH || '', 'playwright'))); }

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const screens = process.argv.includes('--screens') ? process.argv[process.argv.indexOf('--screens') + 1] : null;
if (screens) fs.mkdirSync(screens, { recursive: true });
let failures = 0;
const ok = (c, m) => { if (c) console.log('  ok   ' + m); else { failures++; console.log('  FAIL ' + m); } };

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
const page = await ctx.newPage();
const errors = [];
page.on('pageerror', e => errors.push(String(e)));
await page.goto('file://' + path.join(root, 'index.html'));
const cdp = await ctx.newCDPSession(page);
const touch = async (type, pts) => cdp.send('Input.dispatchTouchEvent', { type, touchPoints: pts.map(([x, y], i) => ({ x, y, id: i })) });
async function swipe(x0, y0, x1, y1, steps = 8) {
  await touch('touchStart', [[x0, y0]]);
  for (let i = 1; i <= steps; i++) await touch('touchMove', [[x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps]]);
  await touch('touchEnd', []);
  await page.waitForTimeout(150);
}

console.log('phone layout');
await page.setInputFiles('#file-input', [path.join(root, 'sample', 'synthetic_mri.zip')]);
await page.waitForFunction(() => Lightbox.view.frame != null, null, { timeout: 60000 });
await page.waitForTimeout(400);
const layout = await page.evaluate(() => {
  const r = (id) => document.getElementById(id).getBoundingClientRect();
  const list = document.getElementById('series-list');
  return { w: innerWidth, h: innerHeight, vp: r('viewport'), rail: r('rail'), side: r('side'), scrub: r('scrub'), listScroll: list.scrollWidth, listClient: list.clientWidth,
    docScroll: document.documentElement.scrollWidth, panelsVisible: getComputedStyle(document.getElementById('btn-panels')).display !== 'none', wlVisible: getComputedStyle(document.getElementById('btn-wlmode')).display !== 'none' };
});
ok(layout.docScroll <= layout.w, `no horizontal page overflow (${layout.docScroll} <= ${layout.w})`);
ok(layout.vp.width >= layout.w - 2 && layout.vp.height > 300, `viewport spans the width (${Math.round(layout.vp.width)}x${Math.round(layout.vp.height)})`);
ok(layout.rail.top >= layout.vp.bottom - 1 && layout.listScroll > layout.listClient, 'series strip sits below the image and scrolls horizontally');
ok(layout.side.top >= layout.h - 1, 'side panel starts hidden as a bottom sheet');
ok(layout.scrub.bottom <= layout.h + 1 && layout.scrub.top < layout.h, 'scrubber is on screen');
ok(layout.panelsVisible && layout.wlVisible, 'Info & share and W/L buttons shown on touch devices');
if (screens) await page.screenshot({ path: path.join(screens, 'mobile.png') });

console.log('touch gestures');
const vp = layout.vp; const cx = vp.x + vp.width / 2, cy = vp.y + vp.height / 2;
const i0 = await page.evaluate(() => Lightbox.view.index);
await swipe(cx, cy - 60, cx, cy + 60);
const i1 = await page.evaluate(() => Lightbox.view.index);
ok(i1 > i0, `swipe down advances slices (${i0} -> ${i1})`);
await swipe(cx, cy + 60, cx, cy - 60);
const i2 = await page.evaluate(() => Lightbox.view.index);
ok(i2 < i1, `swipe up goes back (${i1} -> ${i2})`);
const wl0 = await page.evaluate(() => ({ ww: Lightbox.view.ww, wc: Lightbox.view.wc, i: Lightbox.view.index }));
await page.click('#btn-wlmode'); await swipe(cx - 60, cy, cx + 60, cy);
const wl1 = await page.evaluate(() => ({ ww: Lightbox.view.ww, wc: Lightbox.view.wc, i: Lightbox.view.index }));
ok(wl1.ww !== wl0.ww && wl1.i === wl0.i, `W/L mode: drag changes window (${wl0.ww.toFixed(0)} -> ${wl1.ww.toFixed(0)}), slice unchanged`);
await page.click('#btn-wlmode');
// pinch: two fingers moving apart
await touch('touchStart', [[cx - 30, cy], [cx + 30, cy]]);
for (let k = 1; k <= 6; k++) await touch('touchMove', [[cx - 30 - k * 15, cy], [cx + 30 + k * 15, cy]]);
await touch('touchEnd', []);
await page.waitForTimeout(150);
const zoom = await page.evaluate(() => Lightbox.view.zoom);
ok(zoom > 1.5, `pinch zooms in (zoom ${zoom.toFixed(2)})`);
await page.tap('#btn-fit');
ok((await page.evaluate(() => Lightbox.view.zoom)) === 1, 'Fit resets zoom');

console.log('bottom sheet');
await page.tap('#btn-panels'); await page.waitForTimeout(350);
const open = await page.evaluate(() => document.getElementById('side').getBoundingClientRect().top < innerHeight - 100);
ok(open, 'Info & share opens the bottom sheet');
if (screens) await page.screenshot({ path: path.join(screens, 'mobile-sheet.png') });
await page.tap('#btn-sheet-close'); await page.waitForTimeout(350);
const closed = await page.evaluate(() => document.getElementById('side').getBoundingClientRect().top >= innerHeight - 1);
ok(closed, 'Close hides the bottom sheet');
await page.tap('.series:nth-child(2)'); await page.waitForTimeout(400);
ok((await page.evaluate(() => Lightbox.view.series.plane)) === 'sagittal', 'tapping a series card in the strip selects it');
ok(errors.length === 0, 'no page errors' + (errors.length ? ': ' + errors[0] : ''));
await browser.close();
console.log(failures ? `\n${failures} FAILED` : '\nall passed');
process.exit(failures ? 1 : 0);
