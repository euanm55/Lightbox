// Headless browser tests for index.html.
//
//   python tools/make_test_data.py sample/synthetic_mri.zip
//   python tests/make_expected.py tests/fixtures
//   NODE_PATH=$(npm root -g) node tests/browser_test.mjs [--screens DIR]
//
// Loads the synthetic study and the pydicom corpus through the real file
// input, then compares every decoded first frame with pydicom's pixel_array
// (SHA-256 of the little-endian bytes), and exercises the export paths.

import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

// playwright from the project, or from a global install (NODE_PATH=$(npm root -g))
const require = createRequire(import.meta.url);
let chromium;
try { ({ chromium } = require('playwright')); }
catch { ({ chromium } = require(path.join(process.env.NODE_PATH || '', 'playwright'))); }

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const fixtures = path.join(root, 'tests', 'fixtures');
const screens = process.argv.includes('--screens') ? process.argv[process.argv.indexOf('--screens') + 1] : null;
if (screens) fs.mkdirSync(screens, { recursive: true });

let failures = 0;
const ok = (cond, msg) => { if (cond) console.log('  ok   ' + msg); else { failures++; console.log('  FAIL ' + msg); } };

// Browser-side helper: hash a series' first frame the same way make_expected.py does.
const HASH_FRAME = `async (i) => {
  const s = Lightbox.study.series[i];
  const f = await s.frame(0); const inst = s.instanceAt(0).inst;
  if (f.gray) {
    const g = f.gray; const bytes = new Uint8Array(g.buffer, g.byteOffset, g.byteLength);
    const h = await crypto.subtle.digest('SHA-256', bytes);
    let min = Infinity, max = -Infinity; for (const v of g) { if (v < min) min = v; if (v > max) max = v; }
    return { kind: 'gray', sha256: [...new Uint8Array(h)].map(b => b.toString(16).padStart(2, '0')).join(''), dtype: g.constructor.name, min, max, w: f.width, h: f.height };
  }
  let r = 0, g = 0, b = 0; const n = f.rgba.length / 4;
  for (let k = 0; k < f.rgba.length; k += 4) { r += f.rgba[k]; g += f.rgba[k + 1]; b += f.rgba[k + 2]; }
  return { kind: 'rgba', mean_rgb: [r / n, g / n, b / n], w: f.width, h: f.height };
}`;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
const errors = [];
page.on('pageerror', e => errors.push(String(e)));
page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push(m.text()); });
await page.goto('file://' + path.join(root, 'index.html'));

async function load(files) {
  await page.setInputFiles('#file-input', files);
  await page.waitForFunction(() => Lightbox.study.series.length > 0 || document.getElementById('toast').classList.contains('bad'), null, { timeout: 60000 });
  await page.waitForFunction(() => Lightbox.view.frame != null, null, { timeout: 60000 }).catch(() => {});
  await page.waitForTimeout(300);
}

// ------------------------------------------------------------ synthetic study
console.log('synthetic study');
await load([path.join(root, 'sample', 'synthetic_mri.zip')]);
const syn = await page.evaluate(() => Lightbox.study.series.map(s => ({ d: s.description, n: s.count, plane: s.plane, rows: s.rows, cols: s.cols, aspect: s.aspect, err: s.error })));
ok(syn.length === 8, `8 series found (got ${syn.length}: ${syn.map(s => s.d).join(' | ')})`);
const byDesc = Object.fromEntries(syn.map(s => [s.d, s]));
ok(byDesc['T1 AX (explicit LE)']?.n === 24, 'explicit LE series has 24 slices');
ok(byDesc['T2 SAG (implicit, signed, rescale)']?.n === 96 && byDesc['T2 SAG (implicit, signed, rescale)'].plane === 'sagittal', 'implicit sagittal series has 96 slices');
ok(Math.abs((byDesc['T2 SAG (implicit, signed, rescale)']?.aspect ?? 0) - 2) < 1e-6, 'sagittal aspect ratio is 2 (4 mm rows / 2 mm columns)');
ok(byDesc['FLAIR COR multiframe']?.n === 12, 'multi-frame file yields 12 frames');
ok(byDesc['T1 AX (RLE lossless)']?.n === 24, 'RLE series has 24 slices');
ok(byDesc['T1 AX (deflated)']?.n === 6, 'deflated series has 6 slices');
ok(byDesc['T1 AX (no preamble)']?.n === 6, 'no-preamble series has 6 slices');
ok(byDesc['Secondary capture RGB']?.n === 2, 'RGB secondary capture series has 2 images');
ok(syn.every(s => !s.err), 'no series errors: ' + syn.filter(s => s.err).map(s => `${s.d}: ${s.err}`).join('; '));
const skipped = await page.evaluate(() => ({ skipped: Lightbox.study.skipped.length, failed: Lightbox.study.failed }));
ok(skipped.skipped === 2 && skipped.failed.length === 0, `README.txt and SR skipped, nothing failed (skipped ${skipped.skipped}, failed ${JSON.stringify(skipped.failed)})`);

// sorting by position: shuffled sagittal names must come out in position order
const order = await page.evaluate(() => { const s = Lightbox.study.series.find(x => x.plane === 'sagittal'); return s.instances.map(i => i.position); });
ok(order.every((v, i) => i === 0 || v >= order[i - 1]), 'sagittal instances sorted by position');

// RLE and explicit series must decode to identical pixels
const same = await page.evaluate(async () => {
  const a = Lightbox.study.series.find(s => s.description.includes('explicit LE')), b = Lightbox.study.series.find(s => s.description.includes('RLE'));
  const fa = await a.frame(12), fb = await b.frame(12);
  let diff = 0; for (let i = 0; i < fa.gray.length; i++) if (fa.gray[i] !== fb.gray[i]) diff++;
  return { diff, n: fa.gray.length, max: Math.max(...fa.gray) };
});
ok(same.diff === 0 && same.max > 0, `RLE decode matches uncompressed (${same.diff} differing of ${same.n}, max ${same.max})`);

// rendering: window from header applied, lesion bright
const render = await page.evaluate(async () => {
  await Lightbox.selectSeries(0); await Lightbox.showFrame(15);
  const off = Lightbox.view.off; const d = off.getContext('2d').getImageData(0, 0, off.width, off.height).data;
  let bright = 0, dark = 0; for (let i = 0; i < d.length; i += 4) { if (d[i] > 240) bright++; if (d[i] < 10) dark++; }
  return { ww: Lightbox.view.ww, wc: Lightbox.view.wc, bright, dark, w: off.width };
});
ok(render.ww === 4000 && render.wc === 2000, `header window applied (W ${render.ww} L ${render.wc})`);
ok(render.bright > 5 && render.dark > 1000, `lesion renders bright (${render.bright} px) on dark background (${render.dark} px)`);
if (screens) await page.screenshot({ path: path.join(screens, 'synthetic.png') });

// exports
const sheet = await page.evaluate(async () => { const c = await Lightbox.renderContactSheet(Lightbox.view.series, { tiles: 16, tile: 128, privacy: true }); return { w: c.width, h: c.height }; });
ok(sheet.w === 512 && sheet.h === 512 + 30, `contact sheet 4x4 of 128 px tiles is ${sheet.w}x${sheet.h}`);
const summary = await page.evaluate(() => Lightbox.studySummary(true));
ok(summary.includes('| 1 | S1 T1 AX (explicit LE) | MR | axial | 96×96 | 24 |') && summary.includes('(hidden)'), 'study summary table row and privacy');
await page.click('#btn-sheet'); await page.waitForFunction(() => !document.getElementById('btn-sheet-copy').disabled);
ok(true, 'contact sheet button produced a preview');
const [dl] = await Promise.all([page.waitForEvent('download'), page.click('#btn-pack')]);
const packPath = await dl.path(); const packSize = fs.statSync(packPath).size;
ok(packSize > 50000, `AI pack zip downloaded (${packSize} bytes)`);
// the pack must itself be a readable zip: round-trip it through the page's own reader
const packOk = await page.evaluate(async (b64) => {
  const bin = atob(b64); const u8 = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
  const z = await Lightbox.readZip(u8.buffer); const names = z.entries.map(e => e.name);
  const md = new TextDecoder().decode(await z.readEntry(z.entries.find(e => e.name === 'summary.md')));
  return { n: names.length, hasSummary: md.startsWith('# DICOM study summary'), montages: names.filter(n => n.endsWith('montage.png')).length };
}, fs.readFileSync(packPath).toString('base64'));
ok(packOk.hasSummary && packOk.montages === 8, `AI pack has summary.md and 8 montages (${packOk.n} files)`);

// one contact sheet per series, as separate downloads
const sheetNames = [];
page.on('download', d => sheetNames.push(d.suggestedFilename()));
await page.click('#btn-sheets-all');
await page.waitForFunction(() => document.getElementById('btn-sheets-all').textContent.startsWith('Save a sheet'), null, { timeout: 60000 });
await page.waitForTimeout(500);
const sheetPngs = sheetNames.filter(n => n.endsWith('_sheet.png'));
ok(sheetPngs.length === 8 && sheetPngs[0] === '01_T1_AX_explicit_LE_sheet.png', `one sheet per series saved (${sheetPngs.length}: ${sheetPngs.slice(0, 2).join(', ')} …)`);

// keyboard and mouse interaction
await page.focus('#viewport');
const before = await page.evaluate(() => Lightbox.view.index);
await page.keyboard.press('ArrowDown');
const after = await page.evaluate(() => Lightbox.view.index);
ok(after === before + 1, 'arrow key advances slice');
await page.mouse.move(700, 400); await page.mouse.down(); await page.mouse.move(760, 420, { steps: 5 }); await page.mouse.up();
const wl = await page.evaluate(() => ({ ww: Lightbox.view.ww, wc: Lightbox.view.wc }));
ok(wl.ww !== 4000 || wl.wc !== 2000, `drag changes window (W ${wl.ww.toFixed(0)} L ${wl.wc.toFixed(0)})`);

// ------------------------------------------------------------ pydicom corpus
console.log('pydicom corpus');
const expected = JSON.parse(fs.readFileSync(path.join(fixtures, 'expected.json'), 'utf8'));
await load([path.join(fixtures, 'corpus.zip')]);
const corpus = await page.evaluate(() => Lightbox.study.series.map((s, i) => ({ i, name: s.instances[0].name.split('/').pop(), count: s.count, err: s.error })));
const unsupported = [];
let compared = 0;
for (const [name, exp] of Object.entries(expected)) {
  const s = corpus.find(c => c.name === name);
  if (!s) {
    // a file may share a series with another one; find it by instance name
    const found = await page.evaluate((nm) => { for (const s of Lightbox.study.series) { const k = s.instances.findIndex(i => i.name.endsWith('/' + nm)); if (k >= 0) return k; } return -1; }, name);
    if (found < 0) { ok(false, `${name}: not loaded`); continue; }
  }
  const res = await page.evaluate(async ({ nm, code }) => {
    const hash = eval('(' + code + ')');
    for (let i = 0; i < Lightbox.study.series.length; i++) {
      const s = Lightbox.study.series[i]; const k = s.instances.findIndex(x => x.name.endsWith('/' + nm));
      if (k < 0) continue;
      try { const inst = s.instances[k]; const f = await inst.decode(0);
        if (f.gray) { const g = f.gray; const bytes = new Uint8Array(g.buffer, g.byteOffset, g.byteLength); const h = await crypto.subtle.digest('SHA-256', bytes);
          let min = Infinity, max = -Infinity; for (const v of g) { if (v < min) min = v; if (v > max) max = v; }
          return { kind: 'gray', sha256: [...new Uint8Array(h)].map(b => b.toString(16).padStart(2, '0')).join(''), dtype: g.constructor.name, min, max, w: f.width, h: f.height, frames: inst.frames }; }
        let r = 0, gg = 0, b = 0; const n = f.rgba.length / 4; for (let q = 0; q < f.rgba.length; q += 4) { r += f.rgba[q]; gg += f.rgba[q + 1]; b += f.rgba[q + 2]; }
        return { kind: 'rgba', mean_rgb: [r / n, gg / n, b / n], w: f.width, h: f.height, frames: inst.frames };
      } catch (e) { return { error: e.message }; }
    }
    return { error: 'instance not found' };
  }, { nm: name, code: HASH_FRAME });
  if (exp.error) { console.log(`  skip ${name}: pydicom itself failed (${exp.error})`); continue; }
  const tsShort = exp.ts.replace('1.2.840.10008.1.2', '');
  if (res.error) {
    if (/JPEG 2000|JPEG-LS|12-bit|not supported/.test(res.error)) { unsupported.push(`${name} [${tsShort}]`); continue; }
    ok(false, `${name} [${tsShort}]: ${res.error}`); continue;
  }
  compared++;
  const dims = res.w === exp.cols && res.h === exp.rows;
  if (res.kind === 'gray' && exp.sha256) {
    ok(dims && res.sha256 === exp.sha256, `${name} [${tsShort}] ${exp.cols}x${exp.rows} ${exp.dtype} identical to pydicom` + (res.sha256 === exp.sha256 ? '' : ` (browser ${res.dtype} ${res.min}..${res.max} vs pydicom ${exp.min}..${exp.max})`));
  } else if (res.kind === 'rgba' && exp.mean_rgb) {
    const tol = exp.lossy ? 6 : 1.5; const scale = 2 ** (exp.color_shift || 0);
    const d = Math.max(...exp.mean_rgb.map((v, i) => Math.abs(v / scale - res.mean_rgb[i])));
    ok(dims && d < tol, `${name} [${tsShort}] colour mean within ${tol} of pydicom (max diff ${d.toFixed(2)})`);
  } else ok(false, `${name}: kind mismatch (browser ${res.kind}, expected ${exp.sha256 ? 'gray' : 'colour'})`);
}
console.log(`  compared ${compared} images; unsupported in browser: ${unsupported.length}\n    ${unsupported.join('\n    ')}`);
if (screens) { await page.evaluate(async () => { const i = Lightbox.study.series.findIndex(s => s.instances[0].name.endsWith('CT_small.dcm')); if (i >= 0) await Lightbox.selectSeries(i); }); await page.waitForTimeout(300); await page.screenshot({ path: path.join(screens, 'corpus.png') }); }

// ------------------------------------------------------------ page errors
ok(errors.length === 0, 'no uncaught page errors' + (errors.length ? ': ' + errors.slice(0, 3).join(' | ') : ''));
await browser.close();
console.log(failures ? `\n${failures} FAILED` : '\nall passed');
process.exit(failures ? 1 : 0);
