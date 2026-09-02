#!/usr/bin/env python3
"""Build the browser test fixtures.

Writes into the given output directory:

  corpus.zip        pydicom's bundled sample files plus JPEG-lossless files
                    made with tests/jpeg_lossless_encode.py
  expected.json     for every image: shape and a SHA-256 of the pixel values
                    (little-endian bytes of pydicom's pixel_array), so the
                    browser decoder can be checked bit-for-bit.

Usage: python tests/make_expected.py OUTDIR
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import zipfile

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.uid import JPEGLosslessSV1, JPEGLossless, generate_uid

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from jpeg_lossless_encode import encode  # noqa: E402
from make_test_data import phantom  # noqa: E402

CORPUS = os.path.join(os.path.dirname(pydicom.__file__), "data", "test_files")
# files that are deliberately broken, or that need a decoder the browser cannot have
SKIP = {"badVR.dcm", "MR_truncated.dcm", "JPEG2000-embedded-sequence-delimiter.dcm"}


def lossless_dicoms() -> list[tuple[str, bytes]]:
    vol = phantom(64, 48, 4)
    out = []
    cases = [  # name, bits stored, predictor, restart interval (MCUs), rescale intercept, signed
        ("JLL_16bit_pred1.dcm", 16, 1, 0, 0, False),
        ("JLL_16bit_pred7_rst.dcm", 16, 7, 64, 0, False),   # restart every row
        ("JLL_16bit_pred5_rst2.dcm", 16, 5, 128, 0, False),  # restart every two rows
        ("JLL_16bit_pred6.dcm", 16, 6, 0, 0, False),
        ("JLL_12bit_pred4_signed.dcm", 12, 4, 0, -1024, True),
        ("JLL_16bit_pred3_signed.dcm", 16, 3, 0, 0, True),
        ("JLL_8bit_pred2.dcm", 8, 2, 0, 0, False),
    ]
    for name, bits, pred, rst, intercept, signed in cases:
        sl = vol[2]
        maxv = (1 << bits) - 1
        arr = np.round(sl * maxv).astype(np.int64)
        if signed:
            arr = arr - (1 << (bits - 1))  # signed stored values
        stored = arr & maxv  # two's complement pattern within BitsStored bits, as DICOM requires
        ds = Dataset()
        ds.PatientName = "JLL^TEST"
        ds.PatientID = "JLL"
        ds.Modality = "MR"
        ds.SeriesDescription = name
        ds.SeriesNumber = 50 + len(out)
        ds.StudyInstanceUID = generate_uid()
        ds.SeriesInstanceUID = generate_uid()
        ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.4"
        ds.SOPInstanceUID = generate_uid()
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.Rows, ds.Columns = sl.shape
        ds.BitsAllocated = 16 if bits > 8 else 8
        ds.BitsStored = bits
        ds.HighBit = bits - 1
        ds.PixelRepresentation = 1 if signed else 0
        if intercept:
            ds.RescaleIntercept = str(intercept)
            ds.RescaleSlope = "1"
        ds.PixelData = encapsulate([encode(stored, precision=bits, predictor=pred, restart=rst)])
        ds["PixelData"].is_undefined_length = True
        fm = FileMetaDataset()
        fm.MediaStorageSOPClassUID = ds.SOPClassUID
        fm.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
        fm.TransferSyntaxUID = JPEGLosslessSV1 if pred == 1 else JPEGLossless
        ds.file_meta = fm
        buf = io.BytesIO()
        ds.save_as(buf, enforce_file_format=True)
        out.append((name, buf.getvalue()))
    return out


def expected_for(name: str, data: bytes):
    try:
        ds = pydicom.dcmread(io.BytesIO(data), force=True)
    except Exception:
        return None
    if "PixelData" not in ds or "Rows" not in ds:
        return None
    if not getattr(ds, "file_meta", None) or "TransferSyntaxUID" not in ds.file_meta:
        implicit, _ = ds.original_encoding
        if not hasattr(ds, "file_meta"):
            ds.file_meta = FileMetaDataset()
        ds.file_meta.TransferSyntaxUID = pydicom.uid.ImplicitVRLittleEndian if implicit else pydicom.uid.ExplicitVRLittleEndian
    try:
        arr = pydicom.pixels.pixel_array(ds)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e).splitlines()[0][:120]}
    spp = int(ds.get("SamplesPerPixel", 1))
    nframes = int(ds.get("NumberOfFrames", 1) or 1)
    # first frame only
    if nframes > 1 or (arr.ndim == 3 and spp == 1) or arr.ndim == 4:
        frame = arr[0]
    else:
        frame = arr
    info = {
        "rows": int(ds.Rows), "cols": int(ds.Columns), "spp": spp, "frames": nframes,
        "ts": str(ds.file_meta.TransferSyntaxUID), "photometric": str(ds.get("PhotometricInterpretation", "")),
        "bits": int(ds.BitsAllocated), "series_uid": str(ds.get("SeriesInstanceUID", "")),
    }
    if spp == 1 and str(ds.get("PhotometricInterpretation", "")) != "PALETTE COLOR":
        le = frame.astype(frame.dtype.newbyteorder("<"))
        info["sha256"] = hashlib.sha256(le.tobytes()).hexdigest()
        info["dtype"] = str(frame.dtype)
        info["min"] = int(frame.min())
        info["max"] = int(frame.max())
    else:
        if str(ds.get("PhotometricInterpretation", "")) == "PALETTE COLOR":
            frame = pydicom.pixels.apply_color_lut(frame, ds)
        rgb = frame[..., :3].astype(np.float64)
        info["mean_rgb"] = [float(x) for x in rgb.reshape(-1, 3).mean(axis=0)]
        info["lossy"] = str(ds.file_meta.TransferSyntaxUID) in ("1.2.840.10008.1.2.4.50", "1.2.840.10008.1.2.4.51") or "YBR" in str(ds.get("PhotometricInterpretation", ""))
        # the browser renders colour at 8 bits: high-depth samples are shifted down
        if str(ds.get("PhotometricInterpretation", "")) == "PALETTE COLOR":
            lut_bits = int(ds.RedPaletteColorLookupTableDescriptor[2])
            info["color_shift"] = max(0, lut_bits - 8)
        else:
            info["color_shift"] = max(0, int(ds.BitsAllocated) - 8)
    return info


def main(outdir: str):
    os.makedirs(outdir, exist_ok=True)
    entries = []
    for f in sorted(os.listdir(CORPUS)):
        if f.endswith(".dcm") and f not in SKIP:
            with open(os.path.join(CORPUS, f), "rb") as fh:
                entries.append((f, fh.read()))
    entries += lossless_dicoms()
    expected = {}
    with zipfile.ZipFile(os.path.join(outdir, "corpus.zip"), "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr("corpus/" + name, data)
            info = expected_for(name, data)
            if info is not None:
                expected[name] = info
    with open(os.path.join(outdir, "expected.json"), "w") as fh:
        json.dump(expected, fh, indent=1)
    n_img = sum(1 for v in expected.values() if "error" not in v)
    print(f"{len(entries)} files, {n_img} decodable images, {sum(1 for v in expected.values() if 'error' in v)} pydicom failures")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures")
