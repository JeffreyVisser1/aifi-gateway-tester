"""Eén model: gedeelde 2D-encoder per beeld, gemiddelde over de K beelden van een serie,
daarna een modaliteit-head (softmax) en een body-part-head (multi-label, sigmoid).

De encoder komt uit torchvision, zodat RadImageNet- en ImageNet-gewichten direct passen.
"""
from __future__ import annotations

import logging

import torch
import torch.nn as nn
import torchvision

log = logging.getLogger(__name__)

ENCODERS = {
    "resnet18": (torchvision.models.resnet18, "fc", "ResNet18_Weights"),
    "resnet50": (torchvision.models.resnet50, "fc", "ResNet50_Weights"),
    "densenet121": (torchvision.models.densenet121, "classifier", "DenseNet121_Weights"),
}


def _strip(key: str) -> str:
    for prefix in ("module.", "backbone.", "encoder.", "model."):
        if key.startswith(prefix):
            key = key[len(prefix):]
    return key


def load_external_weights(backbone: nn.Module, path: str) -> float:
    """Laad bv. RadImageNet-gewichten (state_dict of volledig model). Matcht op naam + vorm.

    Geeft de fractie gematchte encoder-parameters terug; controleer die (moet ~1.0 zijn).
    LET OP: een volledig gepickled model laden voert code uit dat bestand uit. Alleen doen
    met bestanden uit een bron die je vertrouwt.
    """
    obj = torch.load(path, map_location="cpu", weights_only=False)
    if isinstance(obj, nn.Module):
        obj = obj.state_dict()
    if isinstance(obj, dict) and "state_dict" in obj:
        obj = obj["state_dict"]
    src = {_strip(k): v for k, v in obj.items()}
    own = backbone.state_dict()
    matched = {k: src[k] for k, v in own.items() if k in src and src[k].shape == v.shape}
    backbone.load_state_dict(matched, strict=False)
    frac = len(matched) / max(1, len(own))
    log.info("Externe gewichten: %d/%d tensors gematcht (%.0f%%)", len(matched), len(own), 100 * frac)
    return frac


class SeriesClassifier(nn.Module):
    def __init__(self, n_modalities: int, n_regions: int, encoder: str = "resnet50",
                 init: str = "imagenet", dropout: float = 0.2):
        super().__init__()
        ctor, head_attr, weights_enum = ENCODERS[encoder]
        weights = getattr(torchvision.models, weights_enum).DEFAULT if init == "imagenet" else None
        self.backbone = ctor(weights=weights)
        if init not in ("imagenet", "none"):
            load_external_weights(self.backbone, init)
        feat_dim = getattr(self.backbone, head_attr).in_features
        setattr(self.backbone, head_attr, nn.Identity())
        self.drop = nn.Dropout(dropout)
        self.head_modality = nn.Linear(feat_dim, n_modalities)
        self.head_region = nn.Linear(feat_dim, n_regions)

    def forward(self, x: torch.Tensor):
        """x: (B, K, 1, H, W) -> (logits_modaliteit (B, M), logits_regio (B, R))."""
        b, k = x.shape[:2]
        x = x.flatten(0, 1).expand(-1, 3, -1, -1)  # grijs -> 3 kanalen voor voorgetrainde gewichten
        feats = self.backbone(x).view(b, k, -1).mean(dim=1)  # aggregatie per serie
        feats = self.drop(feats)
        return self.head_modality(feats), self.head_region(feats)
