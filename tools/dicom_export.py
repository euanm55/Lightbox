#!/usr/bin/env python3
"""Convert a DICOM study (zip, folder or loose files) into PNGs an AI can read.

For every image series in the input this writes

    OUT/summary.md                 human/AI readable overview of the study
    OUT/summary.json               same information, machine readable
    OUT/series_NN_<label>/
        montage.png                labelled contact sheet of the series
        slice_001.png ...          individual slices (window/level applied)

Typical use inside Claude Code or a ChatGPT code-interpreter session:

    pip install -r tools/requirements.txt
    python tools/dicom_export.py scan.zip -o out
    # then read out/summary.md and look at out/series_*/montage.png

Options let you subsample slices, change window/level, or only print the
series listing.  Nothing here is a medical device; it just renders pixels.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

try:
    import numpy as np
    import pydicom
    from PIL import Image, ImageDraw, ImageFont
    from pydicom.uid import ImplicitVRLittleEndian, ExplicitVRLittleEndian
except ImportError as exc:  # pragma: no cover - depends on environment
    sys.exit(
        f"missing dependency: {exc}.\n"
        "Install with:  pip install -r tools/requirements.txt\n"
        "(pydicom, numpy, pillow; plus pylibjpeg packages for JPEG/JPEG2000 files)"
    )

DECODER_HINT = (
    "This series uses a compressed transfer syntax that needs an extra decoder.\n"
    "Try:  pip install pylibjpeg pylibjpeg-libjpeg pylibjpeg-openjpeg pylibjpeg-rle pyjpegls"
)


# --------------------------------------------------------------------------- input


def iter_input_files(inputs: Iterable[str]) -> Iterable[tuple[str, bytes]]:
    """Yield (display_name, bytes) for every candidate file in the inputs."""
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    if f.suffix.lower() == ".zip":
                        yield from iter_zip(f.read_bytes(), str(f))
                    else:
                        yield str(f), f.read_bytes()
        elif p.suffix.lower() == ".zip" or zipfile.is_zipfile(p):
            yield from iter_zip(p.read_bytes(), str(p))
        elif p.is_file():
            yield str(p), p.read_bytes()
        else:
            print(f"warning: {item} not found", file=sys.stderr)


def iter_zip(data: bytes, label: str) -> Iterable[tuple[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            if info.is_dir() or "__MACOSX" in info.filename:
                continue
            content = zf.read(info)
            name = f"{label}!{info.filename}"
            if info.filename.lower().endswith(".zip"):
                yield from iter_zip(content, name)
            else:
                yield name, content


def read_dicom(name: str, data: bytes):
    """Return a pydicom Dataset with pixel data, or None if not a usable image."""
    if len(data) < 132:
        return None
    try:
        ds = pydicom.dcmread(io.BytesIO(data), force=True)
    except Exception:
        return None
    if "PixelData" not in ds or "Rows" not in ds or "Columns" not in ds:
        return None
    if not getattr(ds, "file_meta", None) or "TransferSyntaxUID" not in ds.file_meta:
        # No part-10 header: guess the encoding pydicom used when reading.
        implicit, little = ds.original_encoding
        if not hasattr(ds, "file_meta"):
            ds.file_meta = pydicom.dataset.FileMetaDataset()
        ds.file_meta.TransferSyntaxUID = ImplicitVRLittleEndian if implicit else ExplicitVRLittleEndian
    ds._source_name = name  # noqa: SLF001 (private annotation for reporting)
    return ds


# --------------------------------------------------------------------------- geometry


def orientation_label(iop) -> str:
    if not iop or len(iop) != 6:
        return "unknown"
    r = np.array(iop[:3], float)
    c = np.array(iop[3:], float)
    n = np.abs(np.cross(r, c))
    return ["sagittal", "coronal", "axial"][int(np.argmax(n))]


def slice_position(ds) -> float | None:
    iop = ds.get("ImageOrientationPatient")
    ipp = ds.get("ImagePositionPatient")
    if iop is not None and ipp is not None and len(iop) == 6 and len(ipp) == 3:
        r = np.array([float(v) for v in iop[:3]])
        c = np.array([float(v) for v in iop[3:]])
        return float(np.dot(np.cross(r, c), np.array([float(v) for v in ipp])))
    sl = ds.get("SliceLocation")
    return float(sl) if sl is not None else None


def sort_key(ds):
    pos = slice_position(ds)
    inst = ds.get("InstanceNumber")
    return (
        0 if pos is not None else 1,
        pos if pos is not None else 0.0,
        int(inst) if inst not in (None, "") else 0,
        ds._source_name,
    )


# --------------------------------------------------------------------------- series


@dataclass
class Series:
    uid: str
    datasets: list = field(default_factory=list)
    frames: list = field(default_factory=list)  # np arrays after decoding
    sources: list = field(default_factory=list)  # (file name, frame index)
    error: str | None = None

    @property
    def first(self):
        return self.datasets[0]

    def tag(self, name, default=""):
        v = self.first.get(name, default)
        if v is None or v == "":
            return default
        if isinstance(v, pydicom.multival.MultiValue):
            return [str(x) for x in v]
        return str(v)


def group_series(datasets) -> list[Series]:
    groups: dict[str, Series] = {}
    for ds in datasets:
        uid = ds.get("SeriesInstanceUID") or f"noseries-{ds.get('SeriesNumber', '')}-{os.path.dirname(ds._source_name)}"
        groups.setdefault(uid, Series(uid)).datasets.append(ds)
    series = list(groups.values())
    for s in series:
        s.datasets.sort(key=sort_key)
    series.sort(key=lambda s: (int(s.first.get("SeriesNumber") or 0), s.first._source_name))
    return series


def decode_series(s: Series) -> None:
    for ds in s.datasets:
        try:
            arr = pydicom.pixels.pixel_array(ds)  # converts YBR->RGB, applies nothing else
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "plugin" in msg.lower() or "decoder" in msg.lower() or "handler" in msg.lower():
                msg += "\n" + DECODER_HINT
            s.error = f"{os.path.basename(ds._source_name)}: {msg}"
            return
        nframes = int(ds.get("NumberOfFrames", 1) or 1)
        spp = int(ds.get("SamplesPerPixel", 1))
        if nframes > 1 or (arr.ndim == 3 and spp == 1) or arr.ndim == 4:
            for i in range(arr.shape[0]):
                s.frames.append(arr[i])
                s.sources.append((ds._source_name, i))
        else:
            s.frames.append(arr)
            s.sources.append((ds._source_name, 0))


# --------------------------------------------------------------------------- rendering


def modality_values(frame: np.ndarray, ds) -> np.ndarray:
    slope = float(ds.get("RescaleSlope", 1) or 1)
    intercept = float(ds.get("RescaleIntercept", 0) or 0)
    out = frame.astype(np.float32)
    if slope != 1 or intercept != 0:
        out = out * slope + intercept
    return out


def dicom_window(ds) -> tuple[float, float] | None:
    wc = ds.get("WindowCenter")
    ww = ds.get("WindowWidth")
    if wc is None or ww is None:
        return None
    if isinstance(wc, pydicom.multival.MultiValue):
        wc = wc[0]
    if isinstance(ww, pydicom.multival.MultiValue):
        ww = ww[0]
    try:
        c, w = float(wc), float(ww)
    except (TypeError, ValueError):
        return None
    return (c, w) if w > 0 else None


def choose_window(s: Series, mode: str) -> tuple[float, float, str]:
    """Return (center, width, description)."""
    ds = s.first
    if re.fullmatch(r"-?[\d.]+[,:]-?[\d.]+", mode):
        c, w = (float(x) for x in re.split("[,:]", mode))
        return c, w, "manual"
    if mode in ("dicom", "default"):
        win = dicom_window(ds)
        if win:
            return win[0], win[1], "dicom header"
    # auto: robust percentiles over a subsample of the whole series
    step = max(1, len(s.frames) // 12)
    per_frame = s.datasets_for_frames()
    sample = np.concatenate([modality_values(f, d).ravel()[::7] for f, d in zip(s.frames[::step], per_frame[::step])])
    # ignore the background (the minimum value, e.g. air), which otherwise dominates the percentiles
    fg = sample[sample > sample.min()]
    if fg.size > 100:
        sample = fg
    lo, hi = np.percentile(sample, [0.5, 99.5])
    if hi <= lo:
        lo, hi = float(sample.min()), float(sample.max()) or 1.0
    return float((lo + hi) / 2), float(hi - lo), "auto (0.5-99.5 percentile of non-background)"


def _datasets_for_frames(self: Series):
    """Parallel list of the dataset each frame came from."""
    out, by_name = [], {d._source_name: d for d in self.datasets}
    for name, _ in self.sources:
        out.append(by_name[name])
    return out


Series.datasets_for_frames = _datasets_for_frames  # type: ignore[attr-defined]


def render_frame(frame: np.ndarray, ds, center: float, width: float) -> Image.Image:
    if frame.ndim == 3:  # RGB (already converted from YBR by pydicom)
        return Image.fromarray(np.ascontiguousarray(frame[..., :3]).astype(np.uint8), "RGB")
    if ds.get("PhotometricInterpretation") == "PALETTE COLOR":
        try:
            rgb = pydicom.pixels.apply_color_lut(frame, ds)
            if rgb.dtype != np.uint8:
                rgb = (rgb / 257).astype(np.uint8)
            return Image.fromarray(rgb, "RGB")
        except Exception:  # noqa: BLE001 - fall back to grey
            pass
    vals = modality_values(frame, ds)
    lo = center - 0.5 - (width - 1) / 2
    hi = center - 0.5 + (width - 1) / 2
    g = np.clip((vals - lo) / max(hi - lo, 1e-6), 0, 1)
    if ds.get("PhotometricInterpretation") == "MONOCHROME1":
        g = 1 - g
    return Image.fromarray((g * 255 + 0.5).astype(np.uint8), "L")


def pixel_aspect(ds) -> float:
    """Row spacing / column spacing; >1 means each row is taller than it is wide."""
    sp = ds.get("PixelSpacing")
    try:
        if sp is not None and len(sp) == 2 and float(sp[1]) > 0:
            return float(sp[0]) / float(sp[1])
        par = ds.get("PixelAspectRatio")
        if par is not None and len(par) == 2 and float(par[1]) > 0:
            return float(par[0]) / float(par[1])
    except (TypeError, ValueError):
        pass
    return 1.0


def scale_to(img: Image.Image, target: int | None, aspect: float = 1.0) -> Image.Image:
    """Resize so the longest side is `target` px (or keep size), correcting non-square pixels."""
    w, h = img.width, img.height * aspect
    if target:
        f = target / max(w, h)
        w, h = w * f, h * f
    new = (max(1, round(w)), max(1, round(h)))
    if new == img.size:
        return img
    return img.resize(new, Image.Resampling.LANCZOS if new[0] < img.width else Image.Resampling.BICUBIC)


def font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # older Pillow
        return ImageFont.load_default()


def make_montage(images: list[Image.Image], labels: list[str], title: str, cols: int, tile: int, aspect: float = 1.0) -> Image.Image:
    cols = max(1, min(cols, len(images)))
    rows = math.ceil(len(images) / cols)
    header = 28
    sheet = Image.new("RGB", (cols * tile, rows * tile + header), (0, 0, 0))
    d = ImageDraw.Draw(sheet)
    d.text((6, 6), title, fill=(255, 255, 255), font=font(14))
    for i, (img, lab) in enumerate(zip(images, labels)):
        img = scale_to(img.convert("RGB"), tile, aspect)
        x = (i % cols) * tile + (tile - img.width) // 2
        y = header + (i // cols) * tile + (tile - img.height) // 2
        sheet.paste(img, (x, y))
        d.text(((i % cols) * tile + 4, header + (i // cols) * tile + 2), lab, fill=(255, 255, 0), font=font(12))
    return sheet


# --------------------------------------------------------------------------- summary


def safe_label(text: str, n: int = 40) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")[:n] or "series"


def series_info(s: Series, idx: int) -> dict:
    ds = s.first
    info = {
        "index": idx,
        "series_number": s.tag("SeriesNumber"),
        "description": s.tag("SeriesDescription") or s.tag("ProtocolName"),
        "modality": s.tag("Modality"),
        "plane": orientation_label([float(v) for v in ds.get("ImageOrientationPatient", [])] or None),
        "rows": int(ds.get("Rows", 0)),
        "columns": int(ds.get("Columns", 0)),
        "frames": len(s.frames),
        "files": len(s.datasets),
        "pixel_spacing_mm": s.tag("PixelSpacing"),
        "slice_thickness_mm": s.tag("SliceThickness"),
        "spacing_between_slices_mm": s.tag("SpacingBetweenSlices"),
        "photometric": s.tag("PhotometricInterpretation"),
        "bits_stored": s.tag("BitsStored"),
        "transfer_syntax": ds.file_meta.TransferSyntaxUID.name if "TransferSyntaxUID" in ds.file_meta else "",
        "sequence": " ".join(filter(None, [s.tag("ScanningSequence") if isinstance(s.tag("ScanningSequence"), str) else " ".join(s.tag("ScanningSequence")), s.tag("SequenceName")])),
        "TE_ms": s.tag("EchoTime"),
        "TR_ms": s.tag("RepetitionTime"),
        "TI_ms": s.tag("InversionTime"),
        "flip_angle": s.tag("FlipAngle"),
        "field_strength_T": s.tag("MagneticFieldStrength"),
        "contrast_agent": s.tag("ContrastBolusAgent"),
        "body_part": s.tag("BodyPartExamined"),
        "image_type": s.tag("ImageType"),
        "kvp": s.tag("KVP"),
        "convolution_kernel": s.tag("ConvolutionKernel"),
        "error": s.error,
    }
    return info


def study_info(datasets, anonymize: bool) -> dict:
    ds = datasets[0]
    hide = lambda v: "(hidden)" if anonymize else v  # noqa: E731
    return {
        "patient_name": hide(str(ds.get("PatientName", ""))),
        "patient_id": hide(str(ds.get("PatientID", ""))),
        "patient_birth_date": hide(str(ds.get("PatientBirthDate", ""))),
        "patient_sex": str(ds.get("PatientSex", "")),
        "patient_age": str(ds.get("PatientAge", "")),
        "study_date": str(ds.get("StudyDate", "")),
        "study_description": str(ds.get("StudyDescription", "")),
        "institution": hide(str(ds.get("InstitutionName", ""))),
        "manufacturer": " ".join(filter(None, [str(ds.get("Manufacturer", "")), str(ds.get("ManufacturerModelName", ""))])),
        "modalities": sorted({str(d.get("Modality", "")) for d in datasets}),
    }


def write_summary(out: Path, study: dict, series: list[dict], skipped: list[str]) -> None:
    (out / "summary.json").write_text(json.dumps({"study": study, "series": series, "skipped_files": skipped}, indent=2))
    lines = ["# DICOM study export", ""]
    lines += [f"- **{k.replace('_', ' ')}**: {v}" for k, v in study.items() if v not in ("", [], None)]
    lines += ["", "## Series", ""]
    lines.append("| # | Series | Description | Modality | Plane | Matrix | Slices | Spacing (mm) | Thickness | TE/TR (ms) | Output |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for s in series:
        sp = s["pixel_spacing_mm"]
        sp = "x".join(sp) if isinstance(sp, list) else sp
        te_tr = "/".join(filter(None, [s["TE_ms"], s["TR_ms"]]))
        outp = s.get("output_dir", "") + ("" if not s["error"] else " (FAILED)")
        lines.append(
            f"| {s['index']} | {s['series_number']} | {s['description']} | {s['modality']} | {s['plane']} | "
            f"{s['columns']}x{s['rows']} | {s['frames']} | {sp} | {s['slice_thickness_mm']} | {te_tr} | {outp} |"
        )
    for s in series:
        lines += ["", f"### Series {s['index']}: {s['description']}", ""]
        if s["error"]:
            lines += ["Could not decode pixel data:", "", "```", s["error"], "```"]
            continue
        lines.append(f"- montage: `{s['montage']}`" if s.get("montage") else "- montage: (not written)")
        lines.append(f"- slices written: {s.get('slices_written', 0)} of {s['frames']} (every {s.get('every', 1)})")
        lines.append(f"- window/level used: center {s['window_center']:.1f}, width {s['window_width']:.1f} ({s['window_source']})")
        for k in ("sequence", "TI_ms", "flip_angle", "field_strength_T", "contrast_agent", "body_part", "image_type", "photometric", "bits_stored", "transfer_syntax", "kvp", "convolution_kernel"):
            if s.get(k) not in ("", None, []):
                lines.append(f"- {k.replace('_', ' ')}: {s[k]}")
        if s.get("slice_files"):
            lines += ["", "| Slice | File | Position (mm) | Source |", "|---|---|---|---|"]
            for row in s["slice_files"]:
                lines.append(f"| {row['slice']} | {row['png']} | {row['position']} | {row['source']} |")
    if skipped:
        lines += ["", f"## Skipped {len(skipped)} non-image file(s)", ""]
        lines += [f"- {n}" for n in skipped[:50]]
        if len(skipped) > 50:
            lines.append(f"- ... and {len(skipped) - 50} more")
    lines += [
        "",
        "---",
        "Rendered by tools/dicom_export.py. Slice PNGs are window/levelled 8-bit renderings, ",
        "not the raw pixel values. This is a viewing aid, not a diagnostic device.",
    ]
    (out / "summary.md").write_text("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- main


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="zip file(s), folder(s) or DICOM file(s)")
    ap.add_argument("-o", "--out", default="dicom_out", help="output folder (default: dicom_out)")
    ap.add_argument("--series", help="only export series whose index or number is in this comma list, e.g. 2,5")
    ap.add_argument("--every", type=int, default=1, help="write every Nth slice (default 1 = all)")
    ap.add_argument("--max-slices", type=int, default=0, help="cap on individual slice PNGs per series (0 = no cap)")
    ap.add_argument("--montage-slices", type=int, default=36, help="max tiles per montage (evenly subsampled)")
    ap.add_argument("--montage-cols", type=int, default=0, help="montage columns (default: about sqrt(n), max 6)")
    ap.add_argument("--tile", type=int, default=256, help="montage tile size in px (default 256)")
    ap.add_argument("--size", type=int, default=0, help="scale slice PNGs so the longest side is this many px (0 = native)")
    ap.add_argument("--window", default="default", help="'default' (DICOM header, else auto), 'auto', or 'CENTER,WIDTH'")
    ap.add_argument("--no-slices", action="store_true", help="write only montages")
    ap.add_argument("--no-montage", action="store_true", help="write only individual slices")
    ap.add_argument("--anonymize", action="store_true", help="hide patient name/ID/DOB/institution in the summary")
    ap.add_argument("--list", action="store_true", help="only list series; write nothing")
    a = ap.parse_args(argv)

    datasets, skipped = [], []
    for name, data in iter_input_files(a.inputs):
        ds = read_dicom(name, data)
        (datasets if ds is not None else skipped).append(ds if ds is not None else name)
    if not datasets:
        print("no DICOM images found in the input", file=sys.stderr)
        return 1

    series = group_series(datasets)
    wanted = None
    if a.series:
        wanted = {x.strip() for x in a.series.split(",")}
    infos = []
    out = Path(a.out)
    if not a.list:
        out.mkdir(parents=True, exist_ok=True)

    for idx, s in enumerate(series, 1):
        if wanted and str(idx) not in wanted and s.tag("SeriesNumber") not in wanted:
            continue
        decode_series(s)
        info = series_info(s, idx)
        infos.append(info)
        if a.list or s.error:
            if s.error:
                print(f"series {idx}: decode failed: {s.error.splitlines()[0]}", file=sys.stderr)
            continue

        center, width, wsrc = choose_window(s, a.window)
        info.update(window_center=center, window_width=width, window_source=wsrc)
        sdir = out / f"series_{idx:02d}_{safe_label(info['description'] or info['modality'])}"
        sdir.mkdir(exist_ok=True)
        info["output_dir"] = str(sdir.relative_to(out))
        per_frame_ds = s.datasets_for_frames()
        n = len(s.frames)

        # individual slices
        rows = []
        if not a.no_slices:
            picks = list(range(0, n, max(1, a.every)))
            if a.max_slices and len(picks) > a.max_slices:
                picks = [picks[round(i * (len(picks) - 1) / (a.max_slices - 1))] for i in range(a.max_slices)] if a.max_slices > 1 else [picks[len(picks) // 2]]
            for i in picks:
                img = scale_to(render_frame(s.frames[i], per_frame_ds[i], center, width), a.size or None, pixel_aspect(per_frame_ds[i]))
                fn = f"slice_{i + 1:03d}.png"
                img.save(sdir / fn)
                pos = slice_position(per_frame_ds[i])
                src = os.path.basename(s.sources[i][0]) + (f"#frame{s.sources[i][1] + 1}" if info["files"] < n else "")
                rows.append({"slice": i + 1, "png": f"{info['output_dir']}/{fn}", "position": f"{pos:.1f}" if pos is not None else "", "source": src})
            info["slices_written"] = len(rows)
            info["every"] = a.every
            info["slice_files"] = rows

        # montage
        if not a.no_montage:
            m = min(n, a.montage_slices)
            picks = [round(i * (n - 1) / (m - 1)) for i in range(m)] if m > 1 else [0]
            imgs = [render_frame(s.frames[i], per_frame_ds[i], center, width) for i in picks]
            labels = [f"{i + 1}/{n}" for i in picks]
            cols = a.montage_cols or min(6, math.ceil(math.sqrt(m)))
            title = f"S{info['series_number']} {info['description']} | {info['modality']} {info['plane']} | {info['columns']}x{info['rows']} x{n} | W/L {width:.0f}/{center:.0f}"
            sheet = make_montage(imgs, labels, title, cols, a.tile, pixel_aspect(s.first))
            sheet.save(sdir / "montage.png")
            info["montage"] = f"{info['output_dir']}/montage.png"
        print(f"series {idx}: {info['description']!r} {info['modality']} {info['plane']} {info['columns']}x{info['rows']} x{n} -> {sdir}")

    study = study_info(datasets, a.anonymize)
    if a.list:
        print(json.dumps({"study": study, "series": infos}, indent=2))
        return 0
    write_summary(out, study, infos, skipped)
    print(f"wrote {out / 'summary.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
