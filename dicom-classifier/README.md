# DICOM-classifier: modaliteit en body part uit pixeldata

Eén pipeline voor alle modaliteiten. Eén model met een gedeelde encoder, twee outputs, en
per serie een vergelijking met wat de DICOM-header zegt.

```
DICOM-serie ─► sample K beelden ─► percentiel-normalisatie ─► gedeelde encoder (per beeld)
           ─► gemiddelde over K ─┬─► modaliteit-head  (CT MR DX MG US NM PT XA OT, softmax)
                                 └─► body-part-head   (9 regio's, multi-label, sigmoid) ─► CID 4031-code
           ─► vergelijking met header ─► akkoord / afwijking / lage zekerheid
```

## Bestanden

| Bestand | Rol |
|---|---|
| `taxonomy.py` | Klassen, regio's, SNOMED-codes uit DICOM CID 4031, synoniemen voor BodyPartExamined (EN + NL), TotalSegmentator-regels. **Hier pas je aan per ziekenhuis.** |
| `dicom_io.py` | Series groeperen, slices sorteren, K beelden samplen (slices en/of frames), MONOCHROME1 en RGB afhandelen |
| `autolabel_totalseg.py` | Body-part-labels voor CT (en optioneel MR) via TotalSegmentator |
| `build_dataset.py` | Stap 1: map met DICOM ➜ `manifest.jsonl` + `review.csv` |
| `data.py` | Dataset en MONAI-transforms |
| `model.py` | Encoder (torchvision ResNet/DenseNet) + twee heads; laadt ImageNet- of RadImageNet-gewichten |
| `train.py` | Stap 2: training met validatie op aparte ziekenhuizen |
| `infer.py` | Stap 3: voorspellen en vergelijken met de header ➜ CSV |
| `metrics.py` | P/R/F1, confusion matrix (geverifieerd tegen scikit-learn) |
| `tests/` | Synthetische DICOM-generator en 44 unit tests |

## Gebruik

```bash
pip install -r requirements.txt

# 1. Manifest maken. Met --totalseg worden CT-body-parts door TotalSegmentator bepaald.
python build_dataset.py --input /data/dicom --out work/ --totalseg --device gpu

# 2. Review: open work/review.csv, vul corrected_modality / corrected_regions in waar nodig.
#    Regio's: head neck chest abdomen pelvis spine upper_limb lower_limb breast (gescheiden met ;)
#    of 'unknown'. Ongeldige waarden geven een foutmelding, ze worden niet stilletjes genegeerd.

# 3. Trainen
python train.py --manifest work/manifest.jsonl --overrides work/review.csv --out runs/v1 \
    --encoder resnet50 --init imagenet --epochs 30
#    RadImageNet: --init /pad/naar/RadImageNet-ResNet50.pt  (log toont % gematchte tensors; moet ~100% zijn)

# 4. Voorspellen
python infer.py --ckpt runs/v1/best.pt --input /data/nieuw --out voorspellingen.csv
```

Rooktest op synthetische data: `bash run_smoke_test.sh`.

## Ontwerpkeuzes

- **Alleen pixels als input.** Uit de header gebruikt het model alleen tags die nodig zijn om de
  pixels te decoderen (PhotometricInterpretation, SamplesPerPixel, NumberOfFrames). Modality en
  BodyPartExamined zijn labels en vergelijkingsmateriaal, geen input.
- **Percentiel-normalisatie (1–99%).** Werkt zonder te weten welke modaliteit het is, en is
  invariant voor RescaleSlope/Intercept. Dat is getest.
- **Body part is multi-label.** Een CT thorax-abdomen is `{chest, abdomen}` en wordt
  `416550000 Chest and Abdomen`. Onbekende labels worden gemaskeerd in de loss in plaats van als
  "negatief" geteld.
- **Validatie per ziekenhuis** (`--split-by site`, op InstitutionName). Zo meet je generalisatie
  naar onbekende scanners. Met één site valt het terug op een split per patiënt, met waarschuwing.
- **BatchNorm-herkalibratie na elke epoch.** In de rooktest scoorde het model zonder deze stap
  100% in train-modus en 17% in eval-modus op dezelfde data, door achterlopende running stats.
- **Klassegewichten 1/√frequentie**, zodat zeldzame modaliteiten niet wegvallen.

## Wat is getest en wat niet

Getest (in deze omgeving):
- 44 unit tests: metrics tegen scikit-learn, loss en klassegewichten tegen handberekening,
  normalisatie-invariantie, MONOCHROME1, RGB, sampling (altijd exact K beelden), taxonomie,
  CID 4031-mapping, NIfTI-conversie (geometrie), laden van externe gewichten, site-split.
- End-to-end op synthetische DICOM (12 typen, 3 "ziekenhuizen", incl. multiframe, RGB, SR die
  overgeslagen moet worden): build ➜ train ➜ infer, met bewust foute headers die correct worden
  gemarkeerd.

**Niet** getest:
- **Echte data.** De synthetische beelden zijn kunstmatig makkelijk; de scores zeggen niets.
- **TotalSegmentator zelf.** De API (`totalsegmentator(...)`, `class_map`) is gecontroleerd tegen
  versie 2.18.0, maar het model is hier niet gedraaid (geen GPU, geen gewichten).
- **De volumedrempels in `TOTALSEG_REGION_RULES` hebben geen bron.** Het zijn startwaarden.
  Kalibreer ze op je eigen data via `review.csv`.
- ImageNet-gewichten downloaden (`--init imagenet`) en RadImageNet-bestanden: het laadmechanisme is
  getest, de echte bestanden niet.

## Privacy en veiligheid

- PatientID wordt alleen als SHA-256-hash (16 tekens) opgeslagen, voor de patiënt-split.
- `manifest.jsonl` en `review.csv` bevatten wel bestandspaden, InstitutionName en
  beschrijvingen. Behandel ze als vertrouwelijk.
- `--init` met een volledig gepickled model voert code uit dat bestand uit (`torch.load`).
  Alleen gebruiken met bestanden uit een bron die je vertrouwt.

## Bronnen

- DICOM PS3.16, CID 4031 Common Anatomic Region:
  https://dicom.nema.org/medical/dicom/current/output/chtml/part16/sect_CID_4031.html
- TotalSegmentator: https://github.com/wasserth/TotalSegmentator
- RadImageNet: https://github.com/BMEII-AI/RadImageNet
- MONAI: https://monai.io
