"""Classificatie-engine: één keer het model laden, daarna series of mappen classificeren.

Gedeeld door infer.py (command line), dicom_service.py (DICOM-ontvanger + HTTP-API) en de
add-on van de AIFI-gateway. Alle drie geven dezelfde uitkomst per serie en hetzelfde oordeel
per studie (zie verdict()).
"""
from __future__ import annotations

import logging
import threading

import torch

from data import SeriesDataset
from dicom_io import SeriesInfo, scan_series
from model import SeriesClassifier
from taxonomy import header_to_regions, map_modality, regions_to_cid4031

log = logging.getLogger(__name__)

VERSION = "1.1.0"

# Oordeel per studie, van zwaar naar licht.
VERDICT_MISMATCH = "afwijking"     # model en header zijn het oneens over modaliteit of body part
VERDICT_UNSURE = "onzeker"         # lage zekerheid, gedeeltelijke overlap of header zonder waarde
VERDICT_OK = "akkoord"
VERDICT_NO_IMAGE = "geen_beeld"    # geen enkele beeldserie (alleen SR, KO, PR, ...)


def compare(header_mod_raw, header_mod, pred_mod, p_mod, header_regions, pred_regions, tau_mod):
    status = []
    if p_mod < tau_mod:
        status.append("lage_zekerheid")
    if not header_mod_raw:
        status.append("geen_header_modaliteit")
    elif header_mod != pred_mod:
        status.append("afwijking_modaliteit")
    if not pred_regions:
        status.append("bodypart_onzeker")  # geen enkele regio boven --tau-bp
    elif not header_regions:
        status.append("geen_header_bodypart")
    elif header_regions != pred_regions:
        status.append("gedeeltelijk_bodypart" if header_regions & pred_regions else "afwijking_bodypart")
    return ";".join(status) or "akkoord"


def verdict(series_results: list[dict]) -> str:
    """Eén oordeel voor een studie uit de status van haar beeldseries."""
    if not series_results:
        return VERDICT_NO_IMAGE
    statuses = [s for r in series_results for s in r["status"].split(";")]
    if any(s.startswith("afwijking_") for s in statuses):
        return VERDICT_MISMATCH
    if any(s != "akkoord" for s in statuses):
        return VERDICT_UNSURE
    return VERDICT_OK


class Classifier:
    """Model + drempels. Thread-safe: voorspellingen lopen na elkaar (één model in het geheugen)."""

    def __init__(self, ckpt_path: str, device: str = "auto", tau_mod: float = 0.7, tau_bp: float = 0.5):
        if device in ("", "auto", None):
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.ckpt_path, self.device, self.tau_mod, self.tau_bp = ckpt_path, device, tau_mod, tau_bp
        # weights_only=False: het checkpoint bevat ook de config (dict met lijsten). Alleen
        # checkpoints uit een vertrouwde bron laden: torch.load kan code uitvoeren.
        ckpt = torch.load(ckpt_path, map_location="cpu", weights_only=False)
        cfg = ckpt["config"]
        self.mod_classes, self.regions = cfg["modality_classes"], cfg["regions"]
        self.model = SeriesClassifier(len(self.mod_classes), len(self.regions), cfg["encoder"], init="none")
        self.model.load_state_dict(ckpt["state_dict"])
        self.model.to(device).eval()
        self.loader = SeriesDataset([], k=cfg["k"], img_size=cfg["img_size"], train=False)
        self.info = {"version": VERSION, "checkpoint": ckpt_path, "encoder": cfg["encoder"],
                     "epoch": ckpt.get("epoch"), "device": device}
        self._lock = threading.Lock()
        log.info("Model geladen: %s (%s, epoch %s) op %s", ckpt_path, cfg["encoder"], ckpt.get("epoch"), device)

    def classify_series(self, s: SeriesInfo) -> dict | None:
        """Uitkomst voor één serie, of None als het geen beeldserie is of niet te lezen."""
        header_mod = map_modality(s.modality_raw)
        if header_mod is None:
            return None  # SR, KO, ... geen beeld
        try:
            x = self.loader.load_x(s.files)[None].to(self.device)
        except Exception as exc:
            log.warning("Serie %s overgeslagen: %s", s.series_uid, exc)
            return None
        with self._lock, torch.no_grad():
            lm, lb = self.model(x)
        pm = torch.softmax(lm, 1)[0].cpu().numpy()
        pb = torch.sigmoid(lb)[0].cpu().numpy()
        pred_mod = self.mod_classes[int(pm.argmax())]
        pred_regions = {r for r, p in zip(self.regions, pb) if p >= self.tau_bp}
        header_regions = header_to_regions(s.body_part_raw, header_mod)
        cid = regions_to_cid4031(pred_regions)
        return {
            "series_uid": s.series_uid, "study_uid": s.study_uid, "site": s.site, "n_files": len(s.files),
            "series_description": s.series_description,
            "header_modality_raw": s.modality_raw, "header_modality": header_mod if s.modality_raw else "",
            "pred_modality": pred_mod, "p_modality": round(float(pm.max()), 4),
            "header_body_part_raw": s.body_part_raw, "header_regions": sorted(header_regions),
            "pred_regions": sorted(pred_regions),
            "p_regions": {r: round(float(p), 4) for r, p in zip(self.regions, pb)},
            "pred_cid4031_code": cid[0] if cid else "", "pred_cid4031_meaning": cid[1] if cid else "",
            "status": compare(s.modality_raw, header_mod, pred_mod, float(pm.max()),
                              header_regions, pred_regions, self.tau_mod),
            "first_file": s.files[0],
        }

    def classify_dir(self, path: str, site_tag: str = "InstitutionName") -> list[dict]:
        out = []
        for s in scan_series(path, site_tag=site_tag):
            r = self.classify_series(s)
            if r is not None:
                out.append(r)
        return out

    def classify_study(self, path: str) -> dict:
        """Map met (een deel van) één of meer studies -> oordeel + uitkomst per serie (JSON-vorm)."""
        series = self.classify_dir(path)
        return {"verdict": verdict(series), "series": series, "classifier": self.info}


def to_csv_row(r: dict) -> dict:
    """Uitkomst per serie in de kolomvorm van infer.py's CSV."""
    row = dict(r)
    row["header_regions"] = ";".join(r["header_regions"])
    row["pred_regions"] = ";".join(r["pred_regions"])
    row["p_regions"] = ";".join(f"{k}={v:.2f}" for k, v in r["p_regions"].items())
    return row


CSV_FIELDS = ["series_uid", "site", "n_files", "header_modality_raw", "header_modality", "pred_modality",
              "p_modality", "header_body_part_raw", "header_regions", "pred_regions", "p_regions",
              "pred_cid4031_code", "pred_cid4031_meaning", "status", "first_file"]
