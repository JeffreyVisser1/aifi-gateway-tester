"""Genereert synthetische DICOM-series voor een rooktest van de pipeline.

Dit zijn GEEN realistische beelden: elke modaliteit krijgt een eigen patroon, matrixgrootte en
pixelcodering (CT met HU-offset, DX als MONOCHROME1, US als RGB-multiframe, ...), zodat je kunt
zien dat alle codepaden werken. Een hoge score op deze data zegt niets over echte data.

  python tests/make_synthetic.py --out /tmp/synth --per-class 6
"""
from __future__ import annotations

import argparse
import os

import numpy as np
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid

SITES = ["Ziekenhuis A", "Ziekenhuis B", "Ziekenhuis C"]

# modality, body part, rows, cols, n_files, frames per file, rgb, photometric
SPECS = [
    ("CT", "CHEST", 256, 256, 12, 1, False, "MONOCHROME2"),
    ("CT", "ABDOMEN", 256, 256, 12, 1, False, "MONOCHROME2"),
    ("MR", "HEAD", 192, 192, 10, 1, False, "MONOCHROME2"),
    ("MR", "KNEE", 192, 160, 10, 1, False, "MONOCHROME2"),
    ("DX", "CHEST", 320, 260, 1, 1, False, "MONOCHROME1"),
    ("CR", "HAND", 300, 240, 1, 1, False, "MONOCHROME1"),
    ("MG", "", 320, 200, 1, 1, False, "MONOCHROME2"),
    ("US", "ABDOMEN", 240, 320, 1, 6, True, "RGB"),
    ("NM", "WHOLEBODY", 128, 64, 1, 4, False, "MONOCHROME2"),
    ("PT", "CHEST", 96, 96, 10, 1, False, "MONOCHROME2"),
    ("XA", "HEART", 256, 256, 1, 6, False, "MONOCHROME2"),
    ("SC", "", 200, 300, 1, 1, True, "RGB"),
]


def pattern(mod, rows, cols, rng, idx):
    yy, xx = np.mgrid[0:rows, 0:cols]
    cy, cx = rows / 2, cols / 2
    ell = ((yy - cy) / (rows * 0.42)) ** 2 + ((xx - cx) / (cols * 0.38)) ** 2 < 1
    if mod == "CT":
        img = np.full((rows, cols), -1000.0)
        img[ell] = 40
        img[((yy - cy) / (rows * 0.1)) ** 2 + ((xx - cx) / (cols * 0.1)) ** 2 < 1] = 700  # "bot"
        img += rng.normal(0, 15, img.shape)
        return img + 1024  # opgeslagen met RescaleIntercept -1024
    if mod == "MR":
        img = ell * (300 + 200 * np.sin(xx / 7.0) * np.cos(yy / 9.0)) + rng.rayleigh(20, (rows, cols))
        return img
    if mod in ("DX", "CR"):
        return 3000 - 2000 * ell * (0.5 + 0.5 * np.cos(xx / 15.0)) + rng.normal(0, 30, (rows, cols))
    if mod == "MG":
        half = ((yy - cy) / (rows * 0.45)) ** 2 + (xx / (cols * 0.9)) ** 2 < 1
        return 200 + 2500 * half * (1 - xx / cols) + rng.normal(0, 40, (rows, cols))
    if mod == "US":
        r = np.hypot(yy, xx - cx)
        ang = np.abs(np.arctan2(xx - cx, yy + 1e-6))
        sector = (ang < 0.6) & (r < rows * 0.95)
        return sector * rng.rayleigh(60, (rows, cols)) * (1 - r / (rows * 1.5))
    if mod in ("NM", "PT"):
        return rng.poisson(5 + 60 * ell * (1 + np.sin(yy / 5.0 + idx))).astype(float)
    if mod == "XA":
        vessels = (np.abs(np.sin(xx / 20.0 + yy / 35.0 + idx)) < 0.08)
        return 900 - 500 * vessels + rng.normal(0, 20, (rows, cols))
    # SC: "screenshot" met blokken
    img = np.zeros((rows, cols))
    img[20:60, 20:280] = 255
    img[100:120, :] = 128
    return img


def write(path, mod, body, rows, cols, frames_arr, rgb, photometric, study_uid, series_uid, site, pid, inst):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = SecondaryCaptureImageStorage
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID, ds.SeriesInstanceUID = study_uid, series_uid
    ds.Modality, ds.BodyPartExamined, ds.InstitutionName = mod, body, site
    ds.PatientID, ds.InstanceNumber = pid, inst
    ds.Rows, ds.Columns = rows, cols
    ds.ImagePositionPatient = [0.0, 0.0, float(inst) * 3.0]
    ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    ds.PixelSpacing = [1.0, 1.0]
    ds.SliceThickness = 3.0
    n_frames = frames_arr.shape[0]
    if n_frames > 1:
        ds.NumberOfFrames = n_frames
    if rgb:
        ds.SamplesPerPixel, ds.PhotometricInterpretation, ds.PlanarConfiguration = 3, "RGB", 0
        ds.BitsAllocated = ds.BitsStored = 8
        ds.HighBit, ds.PixelRepresentation = 7, 0
        data = np.clip(frames_arr, 0, 255).astype(np.uint8)
        data = np.repeat(data[..., None], 3, axis=-1)
        data[..., 0] = np.clip(data[..., 0].astype(int) + 20, 0, 255)  # beetje kleur
    else:
        ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, photometric
        ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 16, 12, 11, 0
        data = np.clip(frames_arr, 0, 4095).astype(np.uint16)
        if mod == "CT":
            ds.RescaleIntercept, ds.RescaleSlope = -1024, 1
    ds.PixelData = data.tobytes()
    ds.save_as(path, enforce_file_format=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--per-class", type=int, default=6, help="series per spec")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)
    rng = np.random.default_rng(args.seed)
    n = 0
    for mod, body, rows, cols, n_files, frames, rgb, phot in SPECS:
        for i in range(args.per_class):
            site = SITES[i % len(SITES)]
            study, series = generate_uid(), generate_uid()
            d = os.path.join(args.out, f"{mod}_{body or 'NA'}_{i}")
            os.makedirs(d, exist_ok=True)
            for f in range(n_files):
                arr = np.stack([pattern(mod, rows, cols, rng, f + j) for j in range(frames)])
                write(os.path.join(d, f"{f:03d}.dcm"), mod, body, rows, cols, arr, rgb, phot,
                      study, series, site, f"P{mod}{i}", f + 1)
            n += 1
    # een niet-beeld object (SR) dat overgeslagen moet worden
    sr = Dataset()
    sr.file_meta = FileMetaDataset()
    sr.file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.88.11"
    sr.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    sr.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    sr.SOPClassUID, sr.SOPInstanceUID = sr.file_meta.MediaStorageSOPClassUID, sr.file_meta.MediaStorageSOPInstanceUID
    sr.Modality, sr.SeriesInstanceUID, sr.StudyInstanceUID = "SR", generate_uid(), generate_uid()
    os.makedirs(os.path.join(args.out, "SR"), exist_ok=True)
    sr.save_as(os.path.join(args.out, "SR", "sr.dcm"), enforce_file_format=True)
    with open(os.path.join(args.out, "README.txt"), "w") as fh:
        fh.write("geen DICOM, moet overgeslagen worden\n")
    print(f"{n} series geschreven naar {args.out}")


if __name__ == "__main__":
    main()
