# DICOM-classifier — projectcontext voor Claude Code

Pipeline die per DICOM-serie modaliteit en body part bepaalt uit **alleen pixeldata**, en dat vergelijkt
met de DICOM-header. Gebouwd en getest op Linux (CPU, Python 3.13). Windows-versie, DICOM-dienst en
gateway-add-on zijn gebouwd (versie 1.1); nog niet op echte Windows gedraaid.

## Werkafspraken
- Communiceer in het **Nederlands**. Code-identifiers en commentaar mogen Engels/Nederlands blijven zoals ze zijn.
- Claims over bibliotheken, licenties en standaarden: **met bron**. Geen bron → zeg dat het een inschatting is.
- Rekenwerk en metrics: **verifiëren** (tests tegen scikit-learn of een handberekening), niet aannemen.
- Na elke wijziging: `python -m pytest -q tests/` moet groen blijven (nu 68 tests).
- Zeg expliciet wat getest is en wat niet. Een geslaagde run op synthetische data zegt niets over echte data.

## Architectuur (één model, twee heads)
DICOM-serie → K beelden samplen (`dicom_io.py`) → percentiel-normalisatie 1–99% + resize met behoud van
aspect (`data.py`, MONAI) → torchvision-encoder per beeld → gemiddelde over K → modaliteit-head (softmax,
`CT MR DX MG US NM PT XA OT`) + body-part-head (multi-label sigmoid, 9 regio's) → CID 4031-code.

| Bestand | Rol |
|---|---|
| `taxonomy.py` | Klassen, regio's, SNOMED-codes (DICOM PS3.16 CID 4031, versie 20250709), synoniemen EN+NL, TotalSegmentator-regels |
| `dicom_io.py` | Series groeperen/sorteren, exact K beelden samplen, MONOCHROME1 omkeren, RGB → grijs |
| `autolabel_totalseg.py` | Body-part-labels voor CT/MR via TotalSegmentator (`total`, `total_mr`) |
| `build_dataset.py` | DICOM-map → `manifest.jsonl` + `review.csv` |
| `train.py` | Training, site-split, BN-herkalibratie, overrides uit review.csv |
| `infer.py` | Voorspellen + vergelijken met header → CSV met status per serie, of JSON met oordeel (`--format json`) |
| `engine.py` | `Classifier` (model één keer laden), `compare()`, `verdict()` per studie; gedeeld door infer en dienst |
| `dicom_service.py` | C-STORE SCP → spool → classificeren → results + Basic Text SR (resultTarget) + store-and-forward; HTTP-API `/classify` voor de AIFI-gateway-add-on |
| `metrics.py` | P/R/F1, confusion matrix (geverifieerd tegen scikit-learn) |
| `tests/` | `make_synthetic.py` (synthetische DICOM, 12 typen) + `test_core.py` (44) + `test_service.py` (24: paden, manifest, CSV, JSON, SR, dienst e2e, API) |
| `run_smoke_test.sh` / `.ps1` | End-to-end rooktest (bash / PowerShell) |
| `start_service.ps1`, `windows/` | Dienst starten; installeren als Windows-service (WinSW v2.12.0, SHA-256-pin) |

## Ontwerpbeslissingen — niet zonder overleg wijzigen
- Model krijgt **geen semantische header-tags** als input (Modality, BodyPartExamined, beschrijvingen).
  Alleen decodeertags: PhotometricInterpretation, SamplesPerPixel, NumberOfFrames.
- RescaleSlope/Intercept worden niet toegepast: percentiel-normalisatie is daarvoor invariant (getest).
- Body part is multi-label; onbekend = alle waarden −1 en gemaskeerd in de loss.
- Validatie per ziekenhuis (`InstitutionName`, `--site-tag`), fallback per patiënt met waarschuwing.
- `recalibrate_bn()` na elke epoch: zonder dit scoorde het model 100% in train-modus en 17% in eval-modus
  op dezelfde data (achterlopende BatchNorm-statistieken). Niet verwijderen.
- PatientID alleen als SHA-256-hash (16 tekens). Manifest en review.csv zijn wel vertrouwelijk (paden, InstitutionName).
- Manifest: paden relatief aan `root` met `/` (uitwisselbaar Windows/Linux, `--data-root`). Oude absolute paden blijven werken.
- Oordeelvolgorde per studie: `afwijking` > `onzeker` > `akkoord`; `geen_beeld` zonder beeldseries. De gateway-add-on
  (proxyAIFI 2.1, `addons:`) leest exact dit JSON-formaat (`verdict`, `series[].pred_modality/pred_regions/status`).
  Velden niet hernoemen zonder de gateway mee te wijzigen.
- De dienst logt studies alleen als hash; `results/`, `held/`, `failed/`, `spool/` zijn vertrouwelijk.

## Status
- Getest op Linux CPU: 68 unit tests groen; rooktest 72 series in ~1 min.
- `run_smoke_test.ps1` / `start_service.ps1` gedraaid met PowerShell 7.4 op Linux (paden met `/`); foutpad gecontroleerd.
- Dienst end-to-end met de AIFI Gateway Tester (Java): 3/3 geslaagd (SR terug in ~3 s).
- Gateway-add-on (proxyAIFI-branch `claude/adoring-planck-j3q4y2`, 42 Java-tests groen) tegen de echte dienst en tegen
  `infer.py --format json`.
- Niet getest: echte Windows (PowerShell 5.1, spawn-workers, WinSW-service, lange paden), echte data, TotalSegmentator-run
  (alleen API tegen 2.18.0), GPU, gecomprimeerde transfer syntaxes, echte ImageNet/RadImageNet-gewichten.
- `TOTALSEG_REGION_RULES`-volumedrempels hebben **geen bron**: startwaarden, moeten gekalibreerd.

## Windows-taak: wat is gedaan
1. `run_smoke_test.ps1` (stopt bij eerste fout, exitcode-check per python-commando).
2. Installatie vastgelegd in README (venv, PyTorch via pytorch.org-selector, requirements-test.txt).
3. Risico's: `\\?\`-prefix voor lange paden + melding van onleesbare mappen (`dicom_io.fs_path`); portable manifest;
   `--workers` 2 op Windows + `persistent_workers`; TemporaryDirectory met `ignore_cleanup_errors`; CSV met BOM en
   `;`/cp1252 bij inlezen; TotalSegmentator: volgens de README ook Windows, anders WSL2 alleen voor labeling.
4. README-sectie Windows.
5. GitHub Actions `.github/workflows/classifier.yml`: tests op `windows-latest` en `ubuntu-latest` (+ PowerShell-rooktest op Windows).

Nog open: een eerste run op een echte Windows-machine (of de CI-run) bevestigen.

## Openstaande punten buiten de Windows-taak
- `pylibjpeg-libjpeg` in requirements is **GPLv3**; vervangen of verwijderen als het product verspreid wordt.
- `--init biomedclip` (MIT-licentie) als alternatief voor ImageNet/RadImageNet-gewichten: nog niet gebouwd.
- RadImageNet-gewichten: geen licentie vermeld op GitHub → niet commercieel gebruiken zonder toestemming.
- MDR: afhankelijk van het beoogde doel kan dit onder Rule 11 (klasse IIa+) vallen; nu bedoeld als metadatacontrole.

## Handige commando's
```
python -m pytest -q tests/
python tests/make_synthetic.py --out smoke/synth --per-class 6
python build_dataset.py --input smoke/synth --out smoke/work
python train.py --manifest smoke/work/manifest.jsonl --out smoke/run --encoder resnet18 --init none --k 4 --img-size 96 --epochs 4 --batch-size 8 --lr 1e-3 --workers 2
python infer.py --ckpt smoke/run/best.pt --input smoke/synth --out smoke/predictions.csv
```
