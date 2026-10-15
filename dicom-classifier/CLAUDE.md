# DICOM-classifier — projectcontext voor Claude Code

Pipeline die per DICOM-serie modaliteit en body part bepaalt uit **alleen pixeldata**, en dat vergelijkt
met de DICOM-header. Gebouwd en getest op Linux (CPU, Python 3.13). Huidige taak: **Windows-versie**.

## Werkafspraken
- Communiceer in het **Nederlands**. Code-identifiers en commentaar mogen Engels/Nederlands blijven zoals ze zijn.
- Claims over bibliotheken, licenties en standaarden: **met bron**. Geen bron → zeg dat het een inschatting is.
- Rekenwerk en metrics: **verifiëren** (tests tegen scikit-learn of een handberekening), niet aannemen.
- Na elke wijziging: `python -m pytest -q tests/` moet groen blijven (nu 44 tests).
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
| `infer.py` | Voorspellen + vergelijken met header → CSV met status per serie |
| `metrics.py` | P/R/F1, confusion matrix (geverifieerd tegen scikit-learn) |
| `tests/` | `make_synthetic.py` (synthetische DICOM, 12 typen) + `test_core.py` (44 tests) |
| `run_smoke_test.sh` | End-to-end rooktest (bash) |

## Ontwerpbeslissingen — niet zonder overleg wijzigen
- Model krijgt **geen semantische header-tags** als input (Modality, BodyPartExamined, beschrijvingen).
  Alleen decodeertags: PhotometricInterpretation, SamplesPerPixel, NumberOfFrames.
- RescaleSlope/Intercept worden niet toegepast: percentiel-normalisatie is daarvoor invariant (getest).
- Body part is multi-label; onbekend = alle waarden −1 en gemaskeerd in de loss.
- Validatie per ziekenhuis (`InstitutionName`, `--site-tag`), fallback per patiënt met waarschuwing.
- `recalibrate_bn()` na elke epoch: zonder dit scoorde het model 100% in train-modus en 17% in eval-modus
  op dezelfde data (achterlopende BatchNorm-statistieken). Niet verwijderen.
- PatientID alleen als SHA-256-hash (16 tekens). Manifest en review.csv zijn wel vertrouwelijk (paden, InstitutionName).

## Status
- Getest op Linux CPU: 44 unit tests groen; rooktest 72 series in ~1,5 min (2 cores).
- Getest met multiprocessing start method **spawn** (zoals Windows), 2 dataloader-workers: werkt.
- Niet getest: echte data, TotalSegmentator-run (alleen API tegen 2.18.0), GPU, Windows, macOS,
  gecomprimeerde transfer syntaxes, echte ImageNet/RadImageNet-gewichtenbestanden.
- `TOTALSEG_REGION_RULES`-volumedrempels hebben **geen bron**: startwaarden, moeten gekalibreerd.

## Taak: Windows-versie
Doel eerst: draaien vanaf de command line op Windows 10/11 (PowerShell), native en niet via WSL.
1. `run_smoke_test.ps1` maken, gelijkwaardig aan `run_smoke_test.sh` (venv-pad `.venv\Scripts\python.exe`,
   stoppen bij de eerste fout: `$ErrorActionPreference = 'Stop'` + exitcode-check na elk python-commando).
2. Installatie op Windows doorlopen en vastleggen: venv, PyTorch (CPU en CUDA via pytorch.org-selector),
   `requirements.txt`, `pytest`. Alle 44 tests moeten slagen.
3. Windows-specifieke risico's controleren en zo nodig oplossen:
   - Padlengte > 260 tekens bij diepe DICOM-mappen (melden of `\\?\`-prefix / LongPathsEnabled documenteren).
   - `DataLoader` met `num_workers > 0` (spawn): `if __name__ == "__main__"`-guards staan er al; controleren
     op echte Windows. Eventueel default `--workers` verlagen als het traag start.
   - `tempfile` + SimpleITK/TotalSegmentator: bestandslocks bij opruimen van tijdelijke mappen.
   - Hoofdlettergevoeligheid en `\` vs `/` in paden in `manifest.jsonl` (paden worden als string opgeslagen;
     een manifest van Linux werkt dus niet op Windows en omgekeerd — documenteren).
   - TotalSegmentator op native Windows: werkt dat? Zo niet, WSL2 als route documenteren.
4. README en de handleiding bijwerken met een Windows-sectie (activeren venv, PowerShell-commando's, bekende verschillen).
5. Optioneel: GitHub Actions-workflow met `windows-latest` + `ubuntu-latest` die de tests draait.

Pas daarna, en alleen op verzoek: Windows-service of installer.

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
