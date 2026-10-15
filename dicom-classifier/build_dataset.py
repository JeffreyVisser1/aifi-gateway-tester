"""Stap 1: maak een trainingsmanifest uit een map met DICOM.

Labels per serie:
  modaliteit  <- Modality-tag (gemapt via taxonomy.MODALITY_MAP)
  body part   <- TotalSegmentator (CT, optioneel MR) als --totalseg aan staat,
                 anders BodyPartExamined (+ StudyDescription als vangnet)

Output:
  manifest.jsonl   één regel per serie, met bestandslijst en labels
  review.csv       series die een mens moet bekijken: onbekende body part, of header en
                   TotalSegmentator zijn het oneens. Corrigeer daar en geef het bestand
                   terug aan train.py via --overrides.

Voorbeeld:
  python build_dataset.py --input /data/dicom --out work/ --totalseg --device gpu
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os

from dicom_io import scan_series
from taxonomy import header_to_regions, map_modality, regions_to_cid4031

log = logging.getLogger("build_dataset")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True, help="map met DICOM (recursief)")
    ap.add_argument("--out", required=True, help="outputmap")
    ap.add_argument("--site-tag", default="InstitutionName",
                    help="DICOM-tag die de bron/ziekenhuis aangeeft, voor de split (default InstitutionName)")
    ap.add_argument("--totalseg", action="store_true", help="body part voor CT afleiden met TotalSegmentator")
    ap.add_argument("--totalseg-mr", action="store_true", help="ook MR via TotalSegmentator (total_mr)")
    ap.add_argument("--no-fast", action="store_true", help="TotalSegmentator in volle resolutie")
    ap.add_argument("--device", default="gpu", help="gpu | cpu | mps (voor TotalSegmentator)")
    ap.add_argument("--use-study-description", action="store_true",
                    help="StudyDescription als extra bron voor body-part-labels (alleen labels, nooit modelinput)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    os.makedirs(args.out, exist_ok=True)

    series = scan_series(args.input, site_tag=args.site_tag)
    autolabel = None
    if args.totalseg or args.totalseg_mr:
        from autolabel_totalseg import autolabel_series
        autolabel = autolabel_series

    n_written = n_skipped = 0
    review_rows = []
    with open(os.path.join(args.out, "manifest.jsonl"), "w") as fh:
        for s in series:
            modality = map_modality(s.modality_raw)
            if modality is None:
                n_skipped += 1
                continue
            header_regions = header_to_regions(
                s.body_part_raw, modality, s.study_description if args.use_study_description else None)
            auto_regions = None
            if autolabel and ((modality == "CT" and args.totalseg) or (modality == "MR" and args.totalseg_mr)):
                res = autolabel(s.files, modality, fast=not args.no_fast, device=args.device)
                if res is not None:
                    auto_regions = res["regions"]

            if auto_regions:  # TotalSegmentator wint, header is dan alleen controle
                regions, source = auto_regions, "totalseg"
            elif header_regions:
                regions, source = header_regions, "header"
            else:
                regions, source = set(), "none"

            rec = s.to_dict()
            rec.update(modality=modality, header_regions=sorted(header_regions),
                       auto_regions=sorted(auto_regions) if auto_regions is not None else None,
                       regions=sorted(regions), region_source=source)
            fh.write(json.dumps(rec) + "\n")
            n_written += 1

            reason = None
            if not regions:
                reason = "body part onbekend"
            elif auto_regions and header_regions and set(auto_regions) != set(header_regions):
                reason = "header en TotalSegmentator oneens"
            if reason:
                cid = regions_to_cid4031(regions)
                review_rows.append({
                    "series_uid": s.series_uid, "reason": reason, "modality": modality,
                    "body_part_raw": s.body_part_raw, "series_description": s.series_description,
                    "header_regions": ";".join(sorted(header_regions)),
                    "auto_regions": ";".join(sorted(auto_regions or [])),
                    "proposed_regions": ";".join(sorted(regions)),
                    "proposed_cid4031": f"{cid[0]} {cid[1]}" if cid else "",
                    "corrected_modality": "", "corrected_regions": "",
                    "first_file": s.files[0],
                })

    review_path = os.path.join(args.out, "review.csv")
    fields = ["series_uid", "reason", "modality", "body_part_raw", "series_description", "header_regions",
              "auto_regions", "proposed_regions", "proposed_cid4031", "corrected_modality",
              "corrected_regions", "first_file"]
    with open(review_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(review_rows)
    log.info("%d series in manifest, %d overgeslagen (geen beeld), %d ter review in %s",
             n_written, n_skipped, len(review_rows), review_path)


if __name__ == "__main__":
    main()
