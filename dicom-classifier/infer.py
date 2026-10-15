r"""Stap 3: voorspel modaliteit en body part voor alle series in een map en vergelijk met de header.

Output-CSV per serie:
  header_*        wat de DICOM-header zegt (alleen ter vergelijking, niet gebruikt door het model)
  pred_*          wat het model uit de pixels haalt, met zekerheid
  pred_cid4031    body part als SNOMED-code uit DICOM CID 4031
  status          akkoord | afwijking_modaliteit | afwijking_bodypart | gedeeltelijk_bodypart |
                  lage_zekerheid | bodypart_onzeker | geen_header_modaliteit | geen_header_bodypart
                  (meerdere met ';')

Met --format json schrijft het een JSON-document met één oordeel voor de hele map
(akkoord | onzeker | afwijking | geen_beeld) plus de uitkomst per serie. Die vorm gebruikt de
add-on van de AIFI-gateway (type: command), en ook de HTTP-API van dicom_service.py.

Voorbeeld:
  python infer.py --ckpt runs/v1/best.pt --input /data/nieuw --out voorspellingen.csv
  python infer.py --ckpt runs/v1/best.pt --input C:\pool\studie --out uitkomst.json --format json
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys

from engine import CSV_FIELDS, Classifier, to_csv_row, verdict

log = logging.getLogger("infer")


def write_json_atomic(path: str, doc: dict) -> None:
    """Eerst naar een tijdelijk bestand, dan hernoemen: een lezer ziet nooit een half bestand."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True, help="uitvoerbestand; '-' = stdout (alleen met --format json)")
    ap.add_argument("--format", default="csv", choices=["csv", "json"])
    ap.add_argument("--tau-mod", type=float, default=0.7, help="minimale zekerheid modaliteit")
    ap.add_argument("--tau-bp", type=float, default=0.5, help="drempel per body-part-regio")
    ap.add_argument("--site-tag", default="InstitutionName")
    ap.add_argument("--device", default="auto", help="auto | cpu | cuda")
    args = ap.parse_args(argv)
    # Logging naar stderr, zodat stdout schoon blijft voor --out -
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)

    clf = Classifier(args.ckpt, args.device, args.tau_mod, args.tau_bp)
    results = clf.classify_dir(args.input, site_tag=args.site_tag)

    if args.format == "json":
        doc = {"verdict": verdict(results), "series": results, "classifier": clf.info}
        if args.out == "-":
            json.dump(doc, sys.stdout, indent=1, ensure_ascii=False)
            sys.stdout.write("\n")
        else:
            write_json_atomic(args.out, doc)
        log.info("%d series voorspeld, oordeel %s -> %s", len(results), doc["verdict"], args.out)
        return
    # utf-8-sig: Excel op Windows herkent de codering dan
    with open(args.out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow(to_csv_row(r))
    log.info("%d series voorspeld -> %s", len(results), args.out)


if __name__ == "__main__":
    main()
