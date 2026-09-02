# Notes for Claude Code

This repository is a DICOM viewer (`index.html`) and a converter
(`tools/dicom_export.py`). Its owner uses it to look at their own MRI / CT scans
and to show them to an AI.

## When asked to look at scans

You cannot read DICOM files directly, but you can read PNGs. Do this:

```bash
pip install -r tools/requirements.txt pyjpegls      # once; includes JPEG/JPEG 2000 decoders
python tools/dicom_export.py <the zip or folder> -o dicom_out --every 2 --size 512
```

Then:

1. Read `dicom_out/summary.md` for the series table (modality, plane, matrix,
   slice count, spacing, TE/TR, window used) and the slice → file map.
2. Look at each `dicom_out/series_*/montage.png` with the Read tool for an overview.
3. Open individual `slice_NNN.png` files where detail matters. Use
   `--every 1` or `--series N` to re-export one series at full density, and
   `--window CENTER,WIDTH` to change the contrast (e.g. `--window 40,80` for CT brain).
4. `--list` prints the series table without writing images.

`dicom_out/` is git-ignored. Do not commit real scans (`*.dcm` is ignored) and
prefer `--anonymize` if a summary might be quoted anywhere.

Be plain that image impressions are a viewing aid, not a diagnosis.

## Working on the code

- `index.html` is dependency-free on purpose (no CDN, works offline and on
  GitHub Pages). Keep it that way.
- Tests: `python tools/make_test_data.py sample/synthetic_mri.zip`,
  `python tests/make_expected.py tests/fixtures`, then
  `node tests/browser_test.mjs` (needs Playwright; `NODE_PATH=$(npm root -g)`
  if it is installed globally). Decoders are checked bit-for-bit against pydicom.
- The sample study is synthetic. Regenerate it if `make_test_data.py` changes.
