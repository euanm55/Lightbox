# Lightbox — a DICOM viewer for you and for your AI assistant

Open a zip of DICOM images (an MRI, CT or X-ray study exported from a hospital
portal or CD) in the browser, scroll through the series, adjust window/level,
measure, and export **labelled contact sheets and a study summary** that
Claude or ChatGPT can read. There is also a command-line tool that does the
same conversion for Claude Code or ChatGPT's code interpreter.

Nothing is uploaded anywhere by the viewer: the whole thing is one HTML file
that runs in your browser tab.

| Part | What it is |
|---|---|
| `index.html` | The viewer. Zero dependencies. Works from GitHub Pages, a local web server, or opened directly. |
| `tools/dicom_export.py` | Command-line converter: DICOM zip → PNG slices + montages + `summary.md`. For Claude Code / ChatGPT code interpreter. |
| `tools/make_test_data.py` | Builds `sample/synthetic_mri.zip`, a synthetic phantom "study" in eight encodings. No real patient data anywhere in this repo. |
| `tests/` | Headless-browser tests that check the viewer's decoders bit-for-bit against pydicom. |

## 1. Viewing your scans

**Online:** https://euanm55.github.io/Lightbox/ — published by the
`Deploy viewer to GitHub Pages` workflow on every push to `main`
(*Settings → Pages → Source: GitHub Actions*). It ships only `index.html` and
the sample study. If Pages is set to "Deploy from a branch" instead, GitHub
publishes the branch itself and the workflow skips its deploy step.

**Offline:** download `index.html` and double-click it. Everything except the
"Sample study" button works from a plain file. (The sample needs to be fetched,
so serve the folder with `python -m http.server` if you want it.)

Then drop your zip onto the page. You can also drop a folder, choose loose
`.dcm` files, or a zip that contains zips. The viewer:

- groups images into series (and splits a series that mixes orientations, as localizers do),
- orders slices by their position in the scanner, not by file name,
- applies the window/level from the DICOM header, or a robust automatic one,
- shows PACS-style overlays: slice position, pixel spacing, thickness, TE/TR, window,
- corrects non-square pixels, so sagittal and coronal reformats are not squashed,
- measures distances in millimetres (**M**, then drag),
- shows the pixel value under the cursor and every DICOM tag of the image,
- plays a series as cine,
- hides patient name, ID and date of birth by default (toggle in the side panel).

Mouse and keys: wheel = next slice, drag = window/level, Shift-drag or right-drag = pan,
Ctrl-wheel = zoom, arrows, PgUp/PgDn, Home/End, `[` `]` = series, `F` fit, `R` reset,
`I` invert, `O` overlay, Space = cine, `?` = help.

On a phone or tablet the series list becomes a strip under the image and the
side panel a bottom sheet (**Info & share**). Swipe up or down to move through
slices, pinch to zoom, drag with two fingers to pan, double-tap to fit, and
press **W/L** to make a one-finger drag adjust the window instead.

### Supported encodings

| Transfer syntax | In the browser | With `dicom_export.py` |
|---|---|---|
| Implicit / explicit VR little endian, explicit big endian | ✅ | ✅ |
| Deflated explicit VR | ✅ | ✅ |
| RLE lossless | ✅ | ✅ |
| JPEG baseline (8-bit) | ✅ (browser decoder) | ✅ |
| JPEG lossless (process 14, SV1) — the usual hospital-CD format | ✅ (own decoder) | ✅ with `pylibjpeg-libjpeg` |
| JPEG 2000 | Safari only | ✅ with `pylibjpeg-openjpeg` |
| JPEG-LS, 12-bit JPEG extended | ❌ | ✅ with `pyjpegls` / `pylibjpeg-libjpeg` |

Grey-scale (8/16/32-bit, signed or unsigned, MONOCHROME1/2, rescale slope and
intercept), RGB, YBR (including 4:2:2) and palette colour images all render.
Multi-frame files, including enhanced MR objects that keep their geometry in
functional-group sequences, are supported. Files without the 128-byte preamble
are read too.

## 2. Showing the images to Claude or ChatGPT

Claude and ChatGPT can look at ordinary images, not DICOM. So the workflow is:
render the slices to PNG in a form that carries the context an AI needs
(series description, plane, slice numbers and positions, window used), then
hand those over. There are three ways.

### a) From the viewer (chat interfaces)

In the **Share with an AI** panel:

1. Choose the series, set a window that shows what you care about.
2. **Make contact sheet** → **Copy sheet** (or download). This is one PNG with
   16–36 slices laid out in a grid, each labelled with its slice number and
   position, plus a header naming the series, plane, matrix, spacing and window.
3. **Copy study summary** → a Markdown table of every series in the study.
4. Paste both into the chat and ask your question. For a specific slice,
   **Copy this slice** gives a full-resolution PNG.

**Whole study at once:** **Save a sheet for every series** writes one
labelled contact-sheet PNG per series (a 300 MB study becomes ten or twenty
images of a few hundred KB each). Drag them into the chat together with the
study summary. **…as one zip** bundles the same sheets with `summary.md`; tick
the box to add individual slices too, for chats that can open zips.

Patient name, ID and birth date are left out of everything unless you untick
the privacy box.

### b) From Claude Code (or any agent with a shell)

Put the zip somewhere the agent can reach and say, for example:
"Look at `scan.zip` with `tools/dicom_export.py` and describe the series."
`CLAUDE.md` in this repo already tells Claude Code how. Manually:

```bash
pip install -r tools/requirements.txt
python tools/dicom_export.py scan.zip -o out          # all series, all slices
python tools/dicom_export.py scan.zip -o out --every 3 --size 512   # lighter
python tools/dicom_export.py scan.zip --list           # just the series table
python tools/dicom_export.py scan.zip -o out --series 2,5 --window 400,40
```

The output is:

```
out/summary.md                 study + series tables, slice → file map, window used
out/summary.json               the same, machine readable
out/series_02_T2_SAG/montage.png
out/series_02_T2_SAG/slice_001.png ...
```

Claude Code then reads `summary.md` and looks at the montages and slices with
its image reader. `--anonymize` hides identifying fields in the summary.

### c) From ChatGPT's code interpreter

Upload the zip and `tools/dicom_export.py` together, and ask it to
`pip install pydicom pillow numpy pylibjpeg pylibjpeg-libjpeg pylibjpeg-openjpeg`
and run the script. It can then display the montages inline.

## 3. Development

```bash
pip install -r tools/requirements.txt pyjpegls          # pydicom, numpy, pillow + decoders
python tools/make_test_data.py sample/synthetic_mri.zip  # rebuild the sample study
python tests/make_expected.py tests/fixtures             # pydicom corpus + expected hashes
npm install playwright && npx playwright install chromium
node tests/browser_test.mjs --screens shots/             # 75 checks, screenshots optional
node tests/mobile_test.mjs --screens shots/              # phone layout and touch gestures
```

The browser tests load the synthetic study and pydicom's bundled sample files
through the real file input and compare every decoded first frame against
pydicom's `pixel_array` by SHA-256. `tests/jpeg_lossless_encode.py` is a small
lossless-JPEG *encoder* used only to make 16-bit, signed and restart-interval
test files, since pydicom cannot write that format.

## Caveats

- This is a viewing aid. It is not a medical device and nothing it or an AI
  says about your images is a diagnosis; take questions to your clinicians.
- Large studies live in browser memory (roughly the size of the raw pixel data).
  A few thousand 512×512 slices is fine; whole-body CT with tens of thousands may not be.
- The viewer does not upload anything, but a contact sheet you paste into a
  chat leaves your machine. Keep the privacy box ticked unless you mean otherwise.
