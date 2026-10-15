"""Stap 2: train het model op manifest.jsonl.

De validatieset bestaat uit hele ziekenhuizen (site) die niet in de training zitten, zodat je
meet hoe het model generaliseert naar onbekende scanners en protocollen. Is er maar één site,
dan wordt teruggevallen op een split per patiënt (met waarschuwing).

Voorbeeld:
  python train.py --manifest work/manifest.jsonl --overrides work/review.csv --out runs/v1 \
      --encoder resnet50 --init imagenet --epochs 30
  # RadImageNet:  --init /pad/naar/RadImageNet-ResNet50.pt
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import random

import numpy as np
import torch
import torch.nn.functional as F

from data import SeriesDataset, collate_skip_none
from dicom_io import from_portable
from metrics import modality_report, region_report
from model import SeriesClassifier
from taxonomy import MODALITY_CLASSES, REGIONS

log = logging.getLogger("train")

# Windows start dataloader-workers met 'spawn': elk proces importeert torch en MONAI opnieuw
# (enkele seconden per worker). Minder workers en persistent_workers houden dat beperkt.
DEFAULT_WORKERS = 2 if os.name == "nt" else 4


def load_manifest(path: str, data_root: str | None = None) -> list[dict]:
    """Records met absolute bestandspaden. Relatieve paden worden opgelost tegen data_root, of
    anders tegen de "root" die build_dataset.py in elk record schreef. Zo werkt een manifest dat
    op Linux is gemaakt ook op Windows (en omgekeerd) als je --data-root opgeeft."""
    with open(path, encoding="utf-8") as fh:
        records = [json.loads(line) for line in fh if line.strip()]
    for rec in records:
        root = data_root or rec.get("root") or os.path.dirname(os.path.abspath(path))
        rec["files"] = [from_portable(f, root) for f in rec["files"]]
    return records


def read_csv_rows(path: str) -> list[dict]:
    """CSV inlezen zoals Excel hem op Windows bewaart: UTF-8 met of zonder BOM, of Windows-1252,
    en met ',' of ';' als scheidingsteken (Excel met Nederlandse landinstellingen gebruikt ';')."""
    for enc in ("utf-8-sig", "cp1252", "latin-1"):  # latin-1 decodeert altijd: laatste vangnet
        try:
            with open(path, newline="", encoding=enc) as fh:
                text = fh.read()
            break
        except UnicodeDecodeError:
            continue
    header = text.splitlines()[0] if text else ""
    delim = ";" if header.count(";") > header.count(",") else ","
    return list(csv.DictReader(text.splitlines(), delimiter=delim))


def apply_overrides(records: list[dict], path: str) -> int:
    """Handmatige correcties uit review.csv (kolommen corrected_modality / corrected_regions).
    corrected_regions: regio's gescheiden door ';', of 'unknown' om het label te wissen."""
    by_uid = {r["series_uid"]: r for r in records}
    n = 0
    for row in read_csv_rows(path):
        rec = by_uid.get(row.get("series_uid", ""))
        if rec is None:
            continue
        mod = (row.get("corrected_modality") or "").strip().upper()
        reg = (row.get("corrected_regions") or "").strip()
        if mod:
            if mod not in MODALITY_CLASSES:
                raise ValueError(f"Onbekende modaliteit '{mod}' in {path}; kies uit {MODALITY_CLASSES}")
            rec["modality"] = mod
        if reg:
            regs = [] if reg.lower() in ("unknown", "onbekend", "none") else \
                [x.strip().lower() for x in reg.split(";") if x.strip()]
            bad = [x for x in regs if x not in REGIONS]
            if bad:
                raise ValueError(f"Onbekende regio('s) {bad} in {path}; kies uit {REGIONS}")
            rec["regions"], rec["region_source"] = sorted(regs), "manual"
        n += bool(mod or reg)
    return n


def group_split(records: list[dict], by: str, val_frac: float, seed: int):
    def key(r):
        if by == "site":
            return r.get("site") or "unknown"
        return r.get("patient_hash") or r.get("study_uid") or r["series_uid"]

    groups = sorted({key(r) for r in records})
    if by == "site" and len(groups) < 2:
        log.warning("Maar één site gevonden: validatie per patiënt in plaats van per ziekenhuis. "
                    "Generalisatie naar andere ziekenhuizen wordt dan NIET gemeten.")
        return group_split(records, "patient", val_frac, seed)
    random.Random(seed).shuffle(groups)
    counts = {}
    for r in records:
        counts[key(r)] = counts.get(key(r), 0) + 1
    target, val_groups, n_val = val_frac * len(records), set(), 0
    for g in groups[:-1]:  # minstens één groep blijft voor training
        if n_val >= target:
            break
        val_groups.add(g)
        n_val += counts[g]
    train = [r for r in records if key(r) not in val_groups]
    val = [r for r in records if key(r) in val_groups]
    log.info("Split per %s: %d train / %d val series (%d val-groepen: %s)",
             by, len(train), len(val), len(val_groups), sorted(val_groups)[:10])
    return train, val


def class_weights(records: list[dict]) -> torch.Tensor:
    """1/sqrt(frequentie), genormaliseerd op gemiddelde 1 over aanwezige klassen; afwezig = 0."""
    counts = np.zeros(len(MODALITY_CLASSES))
    for r in records:
        counts[MODALITY_CLASSES.index(r["modality"])] += 1
    w = np.zeros_like(counts)
    present = counts > 0
    w[present] = 1.0 / np.sqrt(counts[present])
    w[present] /= w[present].mean()
    return torch.tensor(w, dtype=torch.float32)


def masked_bce(logits: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
    mask = (target >= 0).float()
    if mask.sum() == 0:
        return logits.sum() * 0.0
    loss = F.binary_cross_entropy_with_logits(logits, target.clamp(min=0), reduction="none")
    return (loss * mask).sum() / mask.sum()


@torch.no_grad()
def recalibrate_bn(model, loader, device, max_batches: int = 50) -> None:
    """Schat BatchNorm-statistieken opnieuw als exact gemiddelde over (niet-geaugmenteerde)
    trainingsdata. Zonder deze stap liepen de running stats in de rooktest achter op de snel
    veranderende gewichten: 100% juist in train-modus, ~17% in eval-modus op dezelfde data."""
    bns = [m for m in model.modules() if isinstance(m, torch.nn.modules.batchnorm._BatchNorm)]
    if not bns:
        return
    saved = [m.momentum for m in bns]
    for m in bns:
        m.reset_running_stats()
        m.momentum = None  # cumulatief gemiddelde
    model.train()
    for i, batch in enumerate(loader):
        if i >= max_batches:
            break
        if batch is not None:
            model(batch[0].to(device))
    for m, mom in zip(bns, saved):
        m.momentum = mom


@torch.no_grad()
def evaluate(model, loader, device, threshold=0.5):
    model.eval()
    ym, pm, yb, pb = [], [], [], []
    for batch in loader:
        if batch is None:
            continue
        x, y_mod, y_bp = batch
        lm, lb = model(x.to(device))
        ym.append(y_mod.numpy()); pm.append(lm.argmax(1).cpu().numpy())
        yb.append(y_bp.numpy()); pb.append(torch.sigmoid(lb).cpu().numpy())
    if not ym:
        return None
    mod = modality_report(np.concatenate(ym), np.concatenate(pm), MODALITY_CLASSES)
    reg = region_report(np.concatenate(yb), np.concatenate(pb), REGIONS, threshold)
    return {"modality": mod, "region": reg}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--data-root", help="map waar de DICOM-bestanden van het manifest nu staan "
                                        "(als die verschilt van de map bij build_dataset.py)")
    ap.add_argument("--overrides", help="review.csv met handmatige correcties")
    ap.add_argument("--out", required=True)
    ap.add_argument("--encoder", default="resnet50", choices=["resnet18", "resnet50", "densenet121"])
    ap.add_argument("--init", default="imagenet", help="imagenet | none | pad naar gewichtenbestand (RadImageNet)")
    ap.add_argument("--k", type=int, default=8, help="beelden per serie")
    ap.add_argument("--img-size", type=int, default=224)
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--batch-size", type=int, default=8, help="series per batch (x K beelden)")
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--bp-weight", type=float, default=1.0, help="gewicht body-part-loss")
    ap.add_argument("--split-by", default="site", choices=["site", "patient"])
    ap.add_argument("--val-frac", type=float, default=0.2)
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help=f"dataloader-processen (default {DEFAULT_WORKERS}; 0 = in het hoofdproces)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    os.makedirs(args.out, exist_ok=True)
    torch.manual_seed(args.seed); np.random.seed(args.seed); random.seed(args.seed)

    records = load_manifest(args.manifest, args.data_root)
    if args.overrides:
        log.info("%d handmatige correcties toegepast", apply_overrides(records, args.overrides))
    train_recs, val_recs = group_split(records, args.split_by, args.val_frac, args.seed)

    dl_kw = dict(batch_size=args.batch_size, num_workers=args.workers, collate_fn=collate_skip_none,
                 persistent_workers=args.workers > 0)
    train_dl = torch.utils.data.DataLoader(SeriesDataset(train_recs, args.k, args.img_size, train=True),
                                           shuffle=True, drop_last=False, **dl_kw)
    val_dl = torch.utils.data.DataLoader(SeriesDataset(val_recs, args.k, args.img_size, train=False),
                                         shuffle=False, **dl_kw)
    calib_dl = torch.utils.data.DataLoader(SeriesDataset(train_recs, args.k, args.img_size, train=False),
                                           shuffle=True, **dl_kw)

    model = SeriesClassifier(len(MODALITY_CLASSES), len(REGIONS), args.encoder, args.init).to(args.device)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    steps = args.epochs * max(1, math.ceil(len(train_recs) / args.batch_size))
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=args.lr, total_steps=steps, pct_start=0.1)
    cw = class_weights(train_recs).to(args.device)
    log.info("Klassegewichten modaliteit: %s", dict(zip(MODALITY_CLASSES, cw.cpu().round(decimals=3).tolist())))

    config = {"encoder": args.encoder, "k": args.k, "img_size": args.img_size,
              "modality_classes": MODALITY_CLASSES, "regions": REGIONS}
    history, best = [], -1.0
    for epoch in range(1, args.epochs + 1):
        model.train()
        tot, n = 0.0, 0
        for batch in train_dl:
            if batch is None:
                continue
            x, y_mod, y_bp = (t.to(args.device) for t in batch)
            lm, lb = model(x)
            loss = F.cross_entropy(lm, y_mod, weight=cw) + args.bp_weight * masked_bce(lb, y_bp)
            opt.zero_grad(); loss.backward(); opt.step()
            if sched.last_epoch < steps - 1:
                sched.step()
            tot += loss.item() * len(y_mod); n += len(y_mod)
        recalibrate_bn(model, calib_dl, args.device)
        ev = evaluate(model, val_dl, args.device)
        mod_f1 = ev["modality"]["macro_f1"] if ev else 0.0
        bp_f1 = ev["region"]["macro_f1"] if ev else 0.0
        has_bp = bool(ev and ev["region"]["n_labeled"])
        score = (mod_f1 + bp_f1) / 2 if has_bp else mod_f1
        history.append({"epoch": epoch, "train_loss": tot / max(1, n), "val": ev, "score": score})
        log.info("epoch %d  loss %.4f  val modaliteit macro-F1 %.3f  body part macro-F1 %.3f (n=%d)",
                 epoch, tot / max(1, n), mod_f1, bp_f1, ev["region"]["n_labeled"] if ev else 0)
        if score > best:
            best = score
            torch.save({"state_dict": model.state_dict(), "config": config, "epoch": epoch, "val": ev},
                       os.path.join(args.out, "best.pt"))
        with open(os.path.join(args.out, "metrics.json"), "w", encoding="utf-8") as fh:
            json.dump(history, fh, indent=1)
    log.info("Klaar. Beste score %.3f, checkpoint: %s", best, os.path.join(args.out, "best.pt"))


if __name__ == "__main__":
    main()
