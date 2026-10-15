"""Dataset en preprocessing (MONAI-transforms).

Elke serie levert een tensor (K, 1, S, S): K gesamplede beelden, modaliteit-agnostisch
genormaliseerd, met behoud van aspect ratio naar S x S gebracht.
"""
from __future__ import annotations

import logging

import numpy as np
import torch
from monai.transforms import (Compose, RandAdjustContrast, RandAffine, RandFlip,
                              RandGaussianNoise, Resize, SpatialPad)

from dicom_io import load_series_images
from taxonomy import MODALITY_CLASSES, REGIONS

log = logging.getLogger(__name__)


def percentile_norm(img: np.ndarray, lower: float = 1.0, upper: float = 99.0) -> np.ndarray:
    """Clip op het [lower, upper]-percentiel en schaal naar [0, 1].

    Kent de modaliteit niet en heeft hem niet nodig. Invariant voor y = a*x + b met a > 0,
    dus RescaleSlope/Intercept maken niet uit. Constante beelden worden 0.
    """
    lo, hi = np.percentile(img, [lower, upper])
    if hi <= lo:
        return np.zeros_like(img, dtype=np.float32)
    return ((np.clip(img, lo, hi) - lo) / (hi - lo)).astype(np.float32)


def image_transform(img_size: int, train: bool) -> Compose:
    t = [Resize(spatial_size=img_size, size_mode="longest", mode="area"),
         SpatialPad(spatial_size=(img_size, img_size), mode="constant")]
    if train:
        t += [RandAffine(prob=0.5, rotate_range=0.2, scale_range=0.1, padding_mode="zeros"),
              RandFlip(prob=0.5, spatial_axis=1),
              RandAdjustContrast(prob=0.3, gamma=(0.7, 1.5)),
              RandGaussianNoise(prob=0.2, std=0.02)]
    return Compose(t)


def encode_regions(regions: list[str]) -> torch.Tensor:
    """Multi-hot vector; volledig -1 als body part onbekend is (wordt gemaskeerd in de loss)."""
    if not regions:
        return torch.full((len(REGIONS),), -1.0)
    y = torch.zeros(len(REGIONS))
    for r in regions:
        y[REGIONS.index(r)] = 1.0
    return y


class SeriesDataset(torch.utils.data.Dataset):
    def __init__(self, records: list[dict], k: int = 8, img_size: int = 224, train: bool = False):
        self.records, self.k, self.train = records, k, train
        self.tf = image_transform(img_size, train)

    def __len__(self):
        return len(self.records)

    def load_x(self, files: list[str]) -> torch.Tensor:
        rng = np.random.default_rng() if self.train else None
        imgs = load_series_images(files, self.k, rng)
        xs = [torch.as_tensor(np.asarray(self.tf(percentile_norm(im)[None]))) for im in imgs]
        return torch.stack(xs).float()  # (K, 1, S, S)

    def __getitem__(self, i):
        rec = self.records[i]
        try:
            x = self.load_x(rec["files"])
        except Exception as exc:  # bv. gecomprimeerde pixeldata zonder decoder
            log.warning("Serie %s overgeslagen: %s", rec["series_uid"], exc)
            return None
        return x, MODALITY_CLASSES.index(rec["modality"]), encode_regions(rec["regions"])


def collate_skip_none(batch):
    batch = [b for b in batch if b is not None]
    if not batch:
        return None
    xs, ym, yb = zip(*batch)
    return torch.stack(xs), torch.tensor(ym), torch.stack(yb)
