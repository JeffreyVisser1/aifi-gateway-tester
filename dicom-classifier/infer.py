"""Stap 3: voorspel modaliteit en body part voor alle series in een map en vergelijk met de header.

Output-CSV per serie:
  header_*        wat de DICOM-header zegt (alleen ter vergelijking, niet gebruikt door het model)
  pred_*          wat het model uit de pixels haalt, met zekerheid
  pred_cid4031    body part als SNOMED-code uit DICOM CID 4031
  status          akkoord | afwijking_modaliteit | afwijking_bodypart | gedeeltelijk_bodypart |
                  lage_zekerheid | bodypart_onzeker | geen_header_modaliteit | geen_header_bodypart
                  (meerdere met ';')

Voorbeeld:
  python infer.py --ckpt runs/v1/best.pt --input /data/nieuw --out voorspellingen.csv
"""
from __future__ import annotations

import argparse
import csv
import logging

import torch

from data import SeriesDataset
from dicom_io import scan_series
from model import SeriesClassifier
from taxonomy import header_to_regions, map_modality, regions_to_cid4031

log = logging.getLogger("infer")


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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--tau-mod", type=float, default=0.7, help="minimale zekerheid modaliteit")
    ap.add_argument("--tau-bp", type=float, default=0.5, help="drempel per body-part-regio")
    ap.add_argument("--site-tag", default="InstitutionName")
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    ckpt = torch.load(args.ckpt, map_location="cpu", weights_only=False)
    cfg = ckpt["config"]
    mod_classes, regions = cfg["modality_classes"], cfg["regions"]
    model = SeriesClassifier(len(mod_classes), len(regions), cfg["encoder"], init="none")
    model.load_state_dict(ckpt["state_dict"])
    model.to(args.device).eval()
    loader = SeriesDataset([], k=cfg["k"], img_size=cfg["img_size"], train=False)

    fields = ["series_uid", "site", "n_files", "header_modality_raw", "header_modality", "pred_modality",
              "p_modality", "header_body_part_raw", "header_regions", "pred_regions", "p_regions",
              "pred_cid4031_code", "pred_cid4031_meaning", "status", "first_file"]
    n = 0
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for s in scan_series(args.input, site_tag=args.site_tag):
            header_mod = map_modality(s.modality_raw)
            if header_mod is None:
                continue  # SR, KO, ... geen beeld
            try:
                x = loader.load_x(s.files)[None].to(args.device)
            except Exception as exc:
                log.warning("Serie %s overgeslagen: %s", s.series_uid, exc)
                continue
            with torch.no_grad():
                lm, lb = model(x)
            pm = torch.softmax(lm, 1)[0].cpu().numpy()
            pb = torch.sigmoid(lb)[0].cpu().numpy()
            pred_mod = mod_classes[int(pm.argmax())]
            pred_regions = {r for r, p in zip(regions, pb) if p >= args.tau_bp}
            header_regions = header_to_regions(s.body_part_raw, header_mod)
            cid = regions_to_cid4031(pred_regions)
            w.writerow({
                "series_uid": s.series_uid, "site": s.site, "n_files": len(s.files),
                "header_modality_raw": s.modality_raw, "header_modality": header_mod if s.modality_raw else "",
                "pred_modality": pred_mod, "p_modality": round(float(pm.max()), 4),
                "header_body_part_raw": s.body_part_raw, "header_regions": ";".join(sorted(header_regions)),
                "pred_regions": ";".join(sorted(pred_regions)),
                "p_regions": ";".join(f"{r}={p:.2f}" for r, p in zip(regions, pb)),
                "pred_cid4031_code": cid[0] if cid else "", "pred_cid4031_meaning": cid[1] if cid else "",
                "status": compare(s.modality_raw, header_mod, pred_mod, float(pm.max()),
                                  header_regions, pred_regions, args.tau_mod),
                "first_file": s.files[0],
            })
            n += 1
    log.info("%d series voorspeld -> %s", n, args.out)


if __name__ == "__main__":
    main()
