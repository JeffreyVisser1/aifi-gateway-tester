"""Body-part-labels voor CT (en optioneel MR) afleiden met TotalSegmentator.

Werkwijze: serie -> NIfTI (SimpleITK) -> TotalSegmentator multilabel -> volume per structuur
-> regio's via de regels in taxonomy.TOTALSEG_REGION_RULES.

Vereist: pip install totalsegmentator SimpleITK nibabel  (en een GPU voor redelijke snelheid;
op CPU kan `fast=True` helpen). De modelgewichten worden bij eerste gebruik gedownload.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile

import numpy as np

from taxonomy import TOTALSEG_REGION_RULES

log = logging.getLogger(__name__)

_RULES = {r: ([re.compile(p) for p in cfg["patterns"]], float(cfg["min_ml"]))
          for r, cfg in TOTALSEG_REGION_RULES.items()}


def volumes_to_regions(volumes_ml: dict[str, float]) -> set[str]:
    """Volume per TotalSegmentator-structuur (ml) -> set van regio's (pure functie, testbaar)."""
    regions = set()
    for region, (pats, min_ml) in _RULES.items():
        total = sum(v for name, v in volumes_ml.items() if any(p.search(name) for p in pats))
        if total >= min_ml:
            regions.add(region)
    return regions


def label_volumes(seg: np.ndarray, voxel_mm3: float, class_map: dict[int, str]) -> dict[str, float]:
    """Telt voxels per label en zet om naar ml."""
    counts = np.bincount(seg.astype(np.int64).ravel(), minlength=max(class_map) + 1)
    return {name: float(counts[idx]) * voxel_mm3 / 1000.0
            for idx, name in class_map.items() if idx < len(counts) and counts[idx] > 0}


def series_to_nifti(files: list[str], out_path: str) -> None:
    import SimpleITK as sitk
    # Paden gaan zonder \\?\-prefix naar SimpleITK; of ITK op Windows paden > 260 tekens zelf
    # aankan is niet getest. Zie README, sectie Windows.
    reader = sitk.ImageSeriesReader()
    reader.SetFileNames(files)  # al gesorteerd op positie in dicom_io.scan_series
    sitk.WriteImage(reader.Execute(), out_path)


def autolabel_series(files: list[str], modality: str, fast: bool = True, device: str = "gpu",
                     min_slices: int = 20) -> dict | None:
    """Geeft {'regions': set, 'volumes_ml': dict} of None als de serie ongeschikt is."""
    if len(files) < min_slices:
        return None  # localizer/scout of losse slices: geen betrouwbare 3D-segmentatie
    from totalsegmentator.map_to_binary import class_map
    from totalsegmentator.python_api import totalsegmentator

    task = "total_mr" if modality == "MR" else "total"
    # Windows: een bestand dat nog open is (SimpleITK, nibabel) kan niet verwijderd worden. Daarom
    # wordt het resultaat binnen het with-blok volledig in het geheugen gelezen, en breekt een
    # mislukte opruimactie de labeling niet af (ignore_cleanup_errors, Python 3.10+).
    with tempfile.TemporaryDirectory(prefix="dcmcls_", ignore_cleanup_errors=True) as tmp:
        nii = os.path.join(tmp, "img.nii.gz")
        try:
            series_to_nifti(files, nii)
            seg_img = totalsegmentator(nii, None, ml=True, task=task, fast=fast and task == "total",
                                       device=device, quiet=True)
            seg = np.asarray(seg_img.dataobj)
            voxel_mm3 = float(np.prod(seg_img.header.get_zooms()[:3]))
            del seg_img
        except Exception as exc:  # corrupte serie, niet-uniforme slices, ...
            log.warning("TotalSegmentator mislukt (%s): %s", files[0], exc)
            return None
    vols = label_volumes(seg, voxel_mm3, class_map[task])
    return {"regions": volumes_to_regions(vols), "volumes_ml": vols}
