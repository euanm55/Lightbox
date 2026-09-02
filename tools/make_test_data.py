#!/usr/bin/env python3
"""Generate a synthetic multi-series DICOM study as a zip file.

The study contains a smooth "head" phantom (an ellipsoid with a couple of
internal structures and one small bright lesion) written out in several
different encodings so that the viewer can be exercised without any real
patient data:

  * explicit VR little endian, 16-bit unsigned, axial, with window tags
  * implicit VR little endian, 16-bit signed with rescale, sagittal,
    shuffled file names (viewer must sort by position)
  * a single multi-frame file, coronal
  * RLE lossless compressed
  * MONOCHROME1 (inverted grey scale)
  * deflated explicit VR little endian
  * 8-bit RGB secondary capture, uncompressed and JPEG baseline
  * a file without the 128-byte preamble / DICM prefix
  * a non-image DICOM object and a plain text file that must be ignored

Usage:
    python tools/make_test_data.py sample/synthetic_mri.zip
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import zipfile

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.uid import (
    DeflatedExplicitVRLittleEndian,
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    JPEGBaseline8Bit,
    RLELossless,
    generate_uid,
)

STUDY_UID = generate_uid()
PATIENT = {
    "PatientName": "PHANTOM^SYNTHETIC",
    "PatientID": "SYN-0001",
    "PatientBirthDate": "19800101",
    "PatientSex": "O",
}


def phantom(nx: int = 96, ny: int = 96, nz: int = 24) -> np.ndarray:
    """Return a float32 volume in [0, 1] shaped (nz, ny, nx)."""
    z, y, x = np.mgrid[0:nz, 0:ny, 0:nx].astype(np.float32)
    x = (x - nx / 2) / (nx / 2)
    y = (y - ny / 2) / (ny / 2)
    z = (z - nz / 2) / (nz / 2)
    vol = np.zeros_like(x)
    head = (x / 0.85) ** 2 + (y / 0.95) ** 2 + (z / 0.9) ** 2 <= 1
    vol[head] = 0.45  # "scalp / brain"
    inner = (x / 0.7) ** 2 + (y / 0.8) ** 2 + (z / 0.75) ** 2 <= 1
    vol[inner] = 0.6
    vent_l = ((x + 0.15) / 0.12) ** 2 + (y / 0.3) ** 2 + (z / 0.3) ** 2 <= 1
    vent_r = ((x - 0.15) / 0.12) ** 2 + (y / 0.3) ** 2 + (z / 0.3) ** 2 <= 1
    vol[vent_l | vent_r] = 0.15
    lesion = ((x - 0.4) / 0.1) ** 2 + ((y + 0.3) / 0.1) ** 2 + ((z - 0.2) / 0.12) ** 2 <= 1
    vol[lesion] = 1.0
    # gentle bias field so slices are not perfectly flat
    vol *= 1 - 0.15 * (x + y) / 2
    return np.clip(vol, 0, 1).astype(np.float32)


def base_dataset(modality: str, series_no: int, desc: str, sop_class: str, series_uid: str | None = None) -> Dataset:
    ds = Dataset()
    for k, v in PATIENT.items():
        setattr(ds, k, v)
    now = dt.datetime(2024, 3, 14, 9, 26, 53)
    ds.StudyDate = now.strftime("%Y%m%d")
    ds.StudyTime = now.strftime("%H%M%S")
    ds.SeriesDate = ds.StudyDate
    ds.AccessionNumber = "ACC123"
    ds.Modality = modality
    ds.Manufacturer = "Synthetic Imaging"
    ds.ManufacturerModelName = "Phantomatron 3000"
    ds.InstitutionName = "Nowhere General"
    ds.StudyDescription = "MRI BRAIN SYNTHETIC"
    ds.SeriesDescription = desc
    ds.ProtocolName = desc
    ds.StudyInstanceUID = STUDY_UID
    ds.SeriesInstanceUID = series_uid or generate_uid()
    ds.SeriesNumber = series_no
    ds.StudyID = "1"
    ds.FrameOfReferenceUID = generate_uid()
    ds.SOPClassUID = sop_class
    ds.SOPInstanceUID = generate_uid()
    ds.PatientPosition = "HFS"
    ds.MagneticFieldStrength = 3
    ds.BodyPartExamined = "BRAIN"
    ds.ImageType = ["ORIGINAL", "PRIMARY", "M", "ND"]
    return ds


def file_meta(ts: str, sop_class: str, sop_uid: str) -> FileMetaDataset:
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = sop_class
    fm.MediaStorageSOPInstanceUID = sop_uid
    fm.TransferSyntaxUID = ts
    fm.ImplementationClassUID = "1.2.826.0.1.3680043.8.498.1"
    fm.ImplementationVersionName = "SYNTH_1.0"
    return fm


MR_SOP = "1.2.840.10008.5.1.4.1.1.4"
SC_SOP = "1.2.840.10008.5.1.4.1.1.7"

ORIENT = {
    "axial": ([1, 0, 0, 0, 1, 0], lambda i, sp: [-96.0, -96.0, -48.0 + i * sp]),
    "sagittal": ([0, 1, 0, 0, 0, -1], lambda i, sp: [-48.0 + i * sp, -96.0, 96.0]),
    "coronal": ([1, 0, 0, 0, 0, -1], lambda i, sp: [-96.0, -48.0 + i * sp, 96.0]),
}


def to_bytes(ds: Dataset, ts: str, preamble: bool = True) -> bytes:
    ds.file_meta = file_meta(ts, ds.SOPClassUID, ds.SOPInstanceUID)
    ds.is_little_endian = True
    ds.is_implicit_VR = ts == ImplicitVRLittleEndian
    buf = io.BytesIO()
    if preamble:
        ds.save_as(buf, enforce_file_format=True)
    else:
        ds.preamble = None
        del ds.file_meta
        ds.save_as(buf, enforce_file_format=False, implicit_vr=True, little_endian=True)
    return buf.getvalue()


def add_geometry(ds: Dataset, plane: str, index: int, spacing: float, thickness: float):
    iop, ipp = ORIENT[plane]
    ds.ImageOrientationPatient = [str(v) for v in iop]
    ds.ImagePositionPatient = [str(v) for v in ipp(index, spacing)]
    ds.SliceLocation = str(ipp(index, spacing)[{"axial": 2, "sagittal": 0, "coronal": 1}[plane]])
    # volume voxels are 2 x 2 x 4 mm, so reformats through it have 4 mm rows
    ds.PixelSpacing = ["2.0", "2.0"] if plane == "axial" else ["4.0", "2.0"]
    ds.SliceThickness = str(thickness)
    ds.SpacingBetweenSlices = str(spacing)


def mono_slice_series(
    vol: np.ndarray,
    plane: str,
    series_no: int,
    desc: str,
    ts: str,
    *,
    signed: bool = False,
    intercept: float = 0.0,
    window: bool = True,
    mono1: bool = False,
    preamble: bool = True,
    seq: str = "SE",
    te: str = "10",
    tr: str = "500",
) -> list[tuple[str, bytes]]:
    """Yield (filename, bytes) for one file per slice."""
    if plane == "axial":
        slices = [vol[i] for i in range(vol.shape[0])]
    elif plane == "sagittal":
        slices = [vol[:, :, i] for i in range(vol.shape[2])]
    else:
        slices = [vol[:, i, :] for i in range(vol.shape[1])]
    out = []
    series_uid = generate_uid()
    for i, sl in enumerate(slices):
        ds = base_dataset("MR", series_no, desc, MR_SOP, series_uid)
        ds.ScanningSequence = seq
        ds.SequenceVariant = "NONE"
        ds.MRAcquisitionType = "2D"
        ds.EchoTime = te
        ds.RepetitionTime = tr
        ds.FlipAngle = "90"
        ds.InstanceNumber = i + 1
        ds.AcquisitionNumber = 1
        add_geometry(ds, plane, i, 4.0, 4.0)
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME1" if mono1 else "MONOCHROME2"
        ds.Rows, ds.Columns = sl.shape
        ds.BitsAllocated = 16
        ds.BitsStored = 12
        ds.HighBit = 11
        ds.PixelRepresentation = 1 if signed else 0
        arr = np.round(sl * 4000).astype(np.int32)
        if signed:
            arr = arr - int(-intercept)  # stored = (true - intercept) / slope
            ds.RescaleIntercept = str(intercept)
            ds.RescaleSlope = "1"
            ds.RescaleType = "US"
            ds.PixelData = arr.astype("<i2").tobytes()
        else:
            ds.PixelData = arr.astype("<u2").tobytes()
        if mono1:
            ds.PixelData = (4095 - arr).astype("<u2").tobytes()
        if window:
            ds.WindowCenter = "2000"
            ds.WindowWidth = "4000"
        if ts == RLELossless:
            ds.file_meta = file_meta(ExplicitVRLittleEndian, MR_SOP, ds.SOPInstanceUID)
            ds.compress(RLELossless)
            buf = io.BytesIO()
            ds.save_as(buf, enforce_file_format=True)
            data = buf.getvalue()
        else:
            data = to_bytes(ds, ts, preamble=preamble)
        out.append((f"IM{i + 1:04d}.dcm", data))
    return out


def multiframe_series(vol: np.ndarray, series_no: int, desc: str) -> list[tuple[str, bytes]]:
    frames = [vol[:, i, :] for i in range(0, vol.shape[1], 8)]  # coronal, every 8th
    ds = base_dataset("MR", series_no, desc, MR_SOP)
    ds.InstanceNumber = 1
    add_geometry(ds, "coronal", 0, 16.0, 8.0)
    ds.NumberOfFrames = len(frames)
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows, ds.Columns = frames[0].shape
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.WindowCenter = "30000"
    ds.WindowWidth = "60000"
    ds.PixelData = b"".join(np.round(f * 60000).astype("<u2").tobytes() for f in frames)
    return [("MULTIFRAME.dcm", to_bytes(ds, ExplicitVRLittleEndian))]


def rgb_secondary_captures(series_no: int) -> list[tuple[str, bytes]]:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (160, 120), (20, 20, 40))
    d = ImageDraw.Draw(img)
    d.rectangle([10, 10, 70, 60], fill=(220, 40, 40))
    d.ellipse([80, 30, 150, 110], fill=(40, 200, 80))
    d.text((12, 90), "RGB SC", fill=(255, 255, 255))
    arr = np.asarray(img)

    out = []
    series_uid = generate_uid()
    for kind in ("raw", "jpeg"):
        ds = base_dataset("OT", series_no, "Secondary capture RGB", SC_SOP, series_uid)
        ds.ConversionType = "WSD"
        ds.InstanceNumber = 1 if kind == "raw" else 2
        ds.SamplesPerPixel = 3
        ds.PhotometricInterpretation = "RGB"
        ds.PlanarConfiguration = 0
        ds.Rows, ds.Columns = arr.shape[:2]
        ds.BitsAllocated = 8
        ds.BitsStored = 8
        ds.HighBit = 7
        ds.PixelRepresentation = 0
        if kind == "raw":
            ds.PixelData = arr.tobytes()
            out.append(("sc/RGB_RAW.dcm", to_bytes(ds, ExplicitVRLittleEndian)))
        else:
            jpg = io.BytesIO()
            img.save(jpg, format="JPEG", quality=92)
            ds.PhotometricInterpretation = "YBR_FULL_422"
            ds.LossyImageCompression = "01"
            ds.PixelData = encapsulate([jpg.getvalue()])
            ds["PixelData"].is_undefined_length = True
            out.append(("sc/RGB_JPEG.dcm", to_bytes(ds, JPEGBaseline8Bit)))
    return out


def non_image_object() -> tuple[str, bytes]:
    ds = base_dataset("SR", 99, "Radiology report (empty)", "1.2.840.10008.5.1.4.1.1.88.11")
    ds.InstanceNumber = 1
    ds.ContentDate = ds.StudyDate
    ds.ContentTime = ds.StudyTime
    ds.CompletionFlag = "COMPLETE"
    ds.VerificationFlag = "UNVERIFIED"
    return ("reports/SR0001.dcm", to_bytes(ds, ExplicitVRLittleEndian))


def build_zip(path: str, small: bool = False) -> None:
    vol = phantom(64, 64, 16) if small else phantom()
    entries: list[tuple[str, bytes]] = []
    entries.append(("README.txt", b"Synthetic DICOM study generated by make_test_data.py\n"))

    s1 = mono_slice_series(vol, "axial", 1, "T1 AX (explicit LE)", ExplicitVRLittleEndian)
    entries += [(f"study/S1_T1_AX/{n}", b) for n, b in s1]

    s2 = mono_slice_series(
        vol, "sagittal", 2, "T2 SAG (implicit, signed, rescale)", ImplicitVRLittleEndian,
        signed=True, intercept=-1024, window=False, seq="SE", te="90", tr="4000",
    )
    # shuffle names so ordering must come from geometry, not file names
    rng = np.random.default_rng(42)
    names = [f"study/S2_T2_SAG/{n}" for n in rng.permutation(9000)[: len(s2)] + 1000]
    entries += [(n, b) for n, (_, b) in zip(names, s2)]

    entries += [(f"study/S3_FLAIR_COR_MF/{n}", b) for n, b in multiframe_series(vol, 3, "FLAIR COR multiframe")]

    s4 = mono_slice_series(vol, "axial", 4, "T1 AX (RLE lossless)", RLELossless)
    entries += [(f"study/S4_RLE/{n}", b) for n, b in s4]

    s5 = mono_slice_series(vol, "axial", 5, "T1 AX (MONOCHROME1)", ExplicitVRLittleEndian, mono1=True)
    entries += [(f"study/S5_MONO1/{n}", b) for n, b in s5[::4]]

    s6 = mono_slice_series(vol, "axial", 6, "T1 AX (deflated)", DeflatedExplicitVRLittleEndian)
    entries += [(f"study/S6_DEFLATED/{n}", b) for n, b in s6[::4]]

    s7 = mono_slice_series(vol, "axial", 7, "T1 AX (no preamble)", ImplicitVRLittleEndian, preamble=False)
    entries += [(f"study/S7_NOPREAMBLE/{n.replace('.dcm', '')}", b) for n, b in s7[::4]]

    entries += rgb_secondary_captures(8)
    entries.append(non_image_object())

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    print(f"wrote {path}: {len(entries)} entries")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", nargs="?", default="sample/synthetic_mri.zip")
    ap.add_argument("--small", action="store_true", help="64x64x16 volume instead of 96x96x24")
    a = ap.parse_args()
    import os

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    build_zip(a.out, small=a.small)
