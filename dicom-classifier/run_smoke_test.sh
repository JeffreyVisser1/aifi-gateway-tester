#!/usr/bin/env bash
# Rooktest: synthetische DICOM -> manifest -> korte training -> inferentie. Draait op CPU in ~1-2 min.
set -euo pipefail
cd "$(dirname "$0")"
W=${1:-./smoke}
rm -rf "$W"
python tests/make_synthetic.py --out "$W/synth" --per-class 6
python build_dataset.py --input "$W/synth" --out "$W/work"
python train.py --manifest "$W/work/manifest.jsonl" --out "$W/run" --encoder resnet18 --init none \
  --k 4 --img-size 96 --epochs 4 --batch-size 8 --lr 1e-3 --workers 2
python infer.py --ckpt "$W/run/best.pt" --input "$W/synth" --out "$W/predictions.csv"
python -m pytest -q tests/
echo "Klaar. Zie $W/predictions.csv en $W/run/metrics.json"
