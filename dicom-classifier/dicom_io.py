"""DICOM inlezen: series groeperen en pixeldata samplen.

Het model krijgt ALLEEN pixels. Uit de header gebruiken we uitsluitend tags die nodig zijn om
de pixels correct te decoderen (PhotometricInterpretation, SamplesPerPixel, NumberOfFrames).
Semantische tags (Modality, BodyPartExamined, beschrijvingen) worden alleen als label en
voor de vergelijking achteraf gebruikt, nooit als modelinput.

RescaleSlope/Intercept passen we bewust niet toe: de percentiel-normalisatie in data.py is
invariant voor een lineaire herschaling met positieve slope (getest in tests/test_core.py).
"""
from __future__ import annotations

import hashlib
import logging
import os
import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
import pydicom
from pydicom.errors import InvalidDicomError

log = logging.getLogger(__name__)

HEADER_TAGS = [
    "SeriesInstanceUID", "StudyInstanceUID", "SOPClassUID", "Modality", "BodyPartExamined",
    "StudyDescription", "SeriesDescription", "InstitutionName", "StationName", "PatientID",
    "InstanceNumber", "ImagePositionPatient", "ImageOrientationPatient", "NumberOfFrames",
    "Rows", "Columns",
]


@dataclass
class SeriesInfo:
    series_uid: str
    study_uid: str = ""
    modality_raw: str = ""
    body_part_raw: str = ""
    study_description: str = ""
    series_description: str = ""
    site: str = "unknown"
    patient_hash: str = ""
    files: list[str] = field(default_factory=list)
    n_frames: int = 0

    def to_dict(self) -> dict:
        return self.__dict__.copy()


# ------------------------------------------------------------------ paden (Windows en Linux)
_WIN = os.name == "nt"
_LONG = "\\\\?\\"            # Win32-prefix voor paden langer dan MAX_PATH (260 tekens)
_LONG_UNC = "\\\\?\\UNC\\"   # idem voor een netwerkshare (\\server\share)
_LONG_FROM = 240             # marge: sommige API's tellen de bestandsnaam van een tijdelijk bestand mee


def fs_path(path: str) -> str:
    r"""Pad zoals het besturingssysteem het moet openen.

    Windows zonder LongPathsEnabled kan paden vanaf 260 tekens niet openen, en os.walk slaat zulke
    mappen dan stil over; diepe PACS-exportmappen halen die lengte makkelijk. Een lang pad krijgt
    daarom de prefix \\?\ (of \\?\UNC\ voor een netwerkshare). Korte paden en andere
    besturingssystemen blijven ongewijzigd.
    """
    if not _WIN or not path or path.startswith(_LONG):
        return path
    p = os.path.abspath(path)
    if len(p) < _LONG_FROM:
        return path
    if p.startswith("\\\\"):
        return _LONG_UNC + p[2:]
    return _LONG + p


def display_path(path: str) -> str:
    r"""Pad zonder \\?\-prefix, voor logs, CSV en manifest."""
    if path.startswith(_LONG_UNC):
        return "\\\\" + path[len(_LONG_UNC):]
    if path.startswith(_LONG):
        return path[len(_LONG):]
    return path


def to_portable(path: str, root: str) -> str:
    """Pad relatief aan root met '/' als scheidingsteken: werkt op Windows en Linux."""
    rel = os.path.relpath(display_path(os.path.abspath(path)), display_path(os.path.abspath(root)))
    return rel.replace(os.sep, "/")


def from_portable(rel: str, root: str) -> str:
    """Omgekeerde van to_portable. Absolute paden (oude manifesten) blijven zoals ze zijn."""
    if os.path.isabs(rel) or posixpath.isabs(rel) or re.match(r"^[A-Za-z]:[\\/]", rel):
        return rel
    return os.path.join(root, *rel.split("/"))


def _hash(value: str) -> str:
    """Eenrichtings-hash zodat PatientID niet in manifesten belandt (alleen voor de split)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16] if value else ""


def _sort_key(item):
    path, inst, pos, orient = item
    # Sorteer op positie langs de slice-normaal als die bekend is, anders op InstanceNumber.
    if pos is not None and orient is not None and len(orient) == 6:
        normal = np.cross(np.asarray(orient[:3], float), np.asarray(orient[3:], float))
        return (0, float(np.dot(normal, np.asarray(pos, float))), inst or 0, path)
    return (1, 0.0, inst or 0, path)


def scan_series(root: str, site_tag: str = "InstitutionName") -> list[SeriesInfo]:
    """Doorzoek een map recursief en groepeer DICOM-bestanden per SeriesInstanceUID."""
    groups: dict[str, list] = defaultdict(list)
    meta: dict[str, SeriesInfo] = {}
    n_files = n_skipped = 0
    unreadable_dirs: list[str] = []

    def walk_error(exc: OSError):  # os.walk slaat onleesbare mappen anders stil over
        unreadable_dirs.append(display_path(getattr(exc, "filename", "") or str(exc)))
        log.warning("Map niet leesbaar, overgeslagen: %s (%s)", display_path(str(exc.filename)), exc.strerror)

    for dirpath, _, filenames in os.walk(fs_path(root), onerror=walk_error):
        for name in filenames:
            path = display_path(os.path.join(dirpath, name))
            n_files += 1
            try:
                ds = pydicom.dcmread(fs_path(path), stop_before_pixels=True,
                                     specific_tags=HEADER_TAGS + [site_tag])
            except (InvalidDicomError, OSError, AttributeError, ValueError):
                n_skipped += 1
                continue
            uid = str(getattr(ds, "SeriesInstanceUID", "") or "")
            if not uid or not getattr(ds, "Rows", None):
                n_skipped += 1  # geen serie of geen beeld (SR, KO, ...)
                continue
            frames = int(getattr(ds, "NumberOfFrames", 1) or 1)
            pos = getattr(ds, "ImagePositionPatient", None)
            orient = getattr(ds, "ImageOrientationPatient", None)
            inst = getattr(ds, "InstanceNumber", None)
            groups[uid].append((path, int(inst) if inst is not None else None,
                                list(pos) if pos is not None else None,
                                list(orient) if orient is not None else None))
            if uid not in meta:
                meta[uid] = SeriesInfo(
                    series_uid=uid,
                    study_uid=str(getattr(ds, "StudyInstanceUID", "") or ""),
                    modality_raw=str(getattr(ds, "Modality", "") or "").upper(),
                    body_part_raw=str(getattr(ds, "BodyPartExamined", "") or ""),
                    study_description=str(getattr(ds, "StudyDescription", "") or ""),
                    series_description=str(getattr(ds, "SeriesDescription", "") or ""),
                    site=str(getattr(ds, site_tag, "") or "unknown").strip() or "unknown",
                    patient_hash=_hash(str(getattr(ds, "PatientID", "") or "")),
                )
            meta[uid].n_frames += frames
    out = []
    for uid, items in groups.items():
        info = meta[uid]
        info.files = [it[0] for it in sorted(items, key=_sort_key)]
        out.append(info)
    log.info("%d bestanden gelezen, %d overgeslagen, %d series gevonden", n_files, n_skipped, len(out))
    if unreadable_dirs:
        log.warning("%d map(pen) konden niet gelezen worden; zie de waarschuwingen hierboven", len(unreadable_dirs))
    return out


def _to_gray(arr: np.ndarray, ds) -> np.ndarray:
    """Eén frame naar float32 2D grijswaarden, met MONOCHROME1 omgekeerd."""
    spp = int(getattr(ds, "SamplesPerPixel", 1) or 1)
    if spp == 3 or (arr.ndim == 3 and arr.shape[-1] == 3):
        arr = arr.astype(np.float32).mean(axis=-1)  # pydicom levert YBR al als RGB
    arr = arr.astype(np.float32)
    if str(getattr(ds, "PhotometricInterpretation", "")).upper() == "MONOCHROME1":
        arr = arr.max() + arr.min() - arr
    return arr


def _frames_of(ds) -> np.ndarray:
    """Pixeldata als (F, H, W[, 3]) ongeacht single- of multi-frame."""
    arr = ds.pixel_array
    frames = int(getattr(ds, "NumberOfFrames", 1) or 1)
    spp = int(getattr(ds, "SamplesPerPixel", 1) or 1)
    if frames == 1:
        arr = arr[None]
    elif spp == 1 and arr.ndim == 2:  # sommige encoders leveren 1 frame zonder frame-as
        arr = arr[None]
    return arr


def _pick(n: int, k: int, rng: np.random.Generator | None) -> np.ndarray:
    """k indices uit range(n): random bij training, gelijk verdeeld bij evaluatie.
    Bij n < k wordt aangevuld met herhaling, zodat elke serie exact k beelden levert."""
    if rng is not None:
        return np.sort(rng.choice(n, size=k, replace=n < k))
    return np.linspace(0, n - 1, k).round().astype(int)


def load_series_images(files: list[str], k: int, rng: np.random.Generator | None = None) -> list[np.ndarray]:
    """Sample exact k 2D-beelden uit een serie (slices en/of frames)."""
    file_idx = _pick(len(files), k, rng)
    # Tel per bestand hoeveel beelden we eruit nodig hebben. Een serie van 1 multiframe-bestand
    # krijgt zo k keer index 0, en daarna k verschillende frames uit dat bestand.
    need = defaultdict(int)
    for i in file_idx:
        need[int(i)] += 1
    images: list[np.ndarray] = []
    for i, n_need in sorted(need.items()):
        ds = pydicom.dcmread(fs_path(files[i]), force=True)
        frames = _frames_of(ds)
        for j in _pick(frames.shape[0], n_need, rng):
            images.append(_to_gray(frames[int(j)], ds))
    assert len(images) == k, (len(images), k)
    return images
