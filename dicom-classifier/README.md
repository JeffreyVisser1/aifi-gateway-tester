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
| `infer.py` | Stap 3: voorspellen en vergelijken met de header ➜ CSV, of JSON met één oordeel (`--format json`) |
| `engine.py` | Model één keer laden, series/studies classificeren, oordeel per studie; gedeeld door `infer.py` en de dienst |
| `dicom_service.py` | DICOM-dienst: C-STORE ontvangen ➜ classificeren ➜ SR terug / doorsturen; HTTP-API voor de gateway-add-on |
| `metrics.py` | P/R/F1, confusion matrix (geverifieerd tegen scikit-learn) |
| `tests/` | Synthetische DICOM-generator, 44 kerntests en 24 tests voor paden, JSON, SR en de dienst |
| `run_smoke_test.ps1` / `.sh` | Rooktest (Windows PowerShell / Linux) |
| `start_service.ps1`, `windows\` | Dienst starten in een venster, of installeren als Windows-service (WinSW) |

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

Rooktest op synthetische data: `.\run_smoke_test.ps1` (Windows) of `bash run_smoke_test.sh` (Linux).

## Windows

Native op Windows 10/11 met PowerShell; WSL is niet nodig. Python 3.10 of nieuwer (getest: 3.13).

```powershell
cd C:\dicom-classifier
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1          # geblokkeerd? eenmalig: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install --upgrade pip
# PyTorch eerst, met het commando van https://pytorch.org/get-started/locally/ , bijvoorbeeld:
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu      # alleen CPU
# NVIDIA-GPU: het CUDA-commando van dezelfde pagina (index-url .../whl/cuXXX)
pip install -r requirements-test.txt     # of requirements.txt zonder de testpakketten
python -m pytest -q tests\               # 68 tests
.\run_smoke_test.ps1                     # rooktest: synthetisch ➜ manifest ➜ training ➜ inferentie
```

De scripts gebruiken `.venv\Scripts\python.exe` als die er is, anders `python` uit het PATH,
en stoppen bij de eerste fout (ook als een python-commando met een foutcode eindigt).

**Verschillen met Linux en wat er is aangepast:**

| Onderwerp | Wat er gebeurt |
|---|---|
| **Paden langer dan 260 tekens** | Diepe PACS-exportmappen halen die grens. `dicom_io` opent lange paden met de prefix `\\?\` (en `\\?\UNC\` voor netwerkshares), en meldt mappen die `os.walk` niet kan lezen in plaats van ze stil over te slaan. Beter nog: zet de Windows-instelling *LongPathsEnabled* aan ([Python-documentatie](https://docs.python.org/3/using/windows.html#removing-the-max-path-limitation)). SimpleITK (TotalSegmentator-labeling) krijgt paden zonder prefix; lange paden zijn daar niet getest. |
| **Manifest tussen Windows en Linux** | `manifest.jsonl` bewaart paden nu relatief aan de `--input`-map, met `/`, plus die map als `root`. Staan de DICOM-bestanden op de andere machine elders: `train.py --data-root D:\export\dicom`. Oude manifesten (absolute paden) werken nog, maar alleen op de machine waar ze gemaakt zijn. Hoofdlettergebruik in paden moet op Linux exact kloppen; op Windows niet. |
| **Excel en `review.csv`** | `review.csv` en de voorspellings-CSV worden in UTF-8 met BOM geschreven, zodat Excel é/ü goed toont. Bij het inlezen (`--overrides`) worden ook `;` als scheidingsteken (Excel met Nederlandse landinstellingen) en Windows-1252 geaccepteerd. |
| **Dataloader-workers** | Windows start workers met *spawn*: elk proces laadt PyTorch opnieuw. Standaard `--workers` is daarom 2 op Windows (4 op Linux), en workers blijven tussen epochs bestaan (`persistent_workers`). Traag bij het starten of geheugenproblemen: `--workers 0`. |
| **Tijdelijke bestanden** | TotalSegmentator-labeling leest het resultaat volledig in vóór het opruimen; een bestand dat Windows nog vergrendeld houdt laat de labeling niet meer mislukken. |
| **TotalSegmentator** | De [TotalSegmentator-README](https://github.com/wasserth/TotalSegmentator) zegt: "works on Ubuntu, Mac, and Windows and on CPU and GPU" (gecontroleerd 15-10-2026); hier niet gedraaid. Lukt het niet native, draai dan alleen `build_dataset.py --totalseg` in WSL2 en de rest op Windows (manifest is uitwisselbaar, zie hierboven). |
| **Stoppen** | De dienst stopt netjes op Ctrl+C, Ctrl+Break en een service-stop. |

## DICOM-dienst: in de beeldstroom

`dicom_service.py` maakt van de classifier een DICOM-knooppunt (C-STORE SCP + C-ECHO):

```text
PACS / modaliteit / AIFI Gateway Tester ──C-STORE──► dicom-classifier (AE AIFICLS : 11300)
      spool ─► na completeAfterSeconds stilte: classificeren ─► results\results.jsonl + results.csv
                 ├─► resultTarget: oordeel als Basic Text SR (nieuwe serie in dezelfde studie) ─► PACS / tester
                 └─► forward: originele beelden door (store-and-forward); andere oordelen ─► held\
```

```powershell
.\start_service.ps1                      # maakt de eerste keer classifier.yaml aan
#  vul model.checkpoint in, en zet resultTarget / forward aan waar nodig
.\start_service.ps1 -Check               # configuratie en model controleren
.\start_service.ps1                      # starten; Ctrl+C stopt
.\windows\install-service.ps1           # als Windows-service (WinSW), als Administrator
```

- **Oordeel per studie:** `akkoord` (alle beeldseries kloppen met de header), `onzeker` (lage
  zekerheid, gedeeltelijke overlap of header zonder waarde), `afwijking` (modaliteit of body
  part wijkt af), `geen_beeld` (alleen SR/KO/PR, ...).
- **SR:** Basic Text SR met het oordeel en per serie een samenvatting, de status en het
  lichaamsdeel als SNOMED-code uit CID 4031 (concept *Target Region*, DCM 123014). Het SR
  verwijst naar alle geclassificeerde beelden en neemt patiënt- en studiegegevens over, zodat
  het in de juiste studie belandt. Eén vaste SR-serie per studie (SeriesNumber 9901).
- **Betrouwbaar:** elk beeld wordt eerst onder een tijdelijke naam weggeschreven en daarna
  hernoemd; pas na SR en doorsturen verdwijnt de studie uit de spool. Mislukt versturen, dan
  volgt een nieuwe poging met oplopende wachttijd; na `retry.maxAttempts` gaat de studie naar
  `failed\` (met `state.json` en de laatste fout). Na een herstart wordt de spool hervat.
- **Testen met de AIFI Gateway Tester:** zet in `tester.yaml` een target op de classifier
  (`port: 11300`, `aeTitle: AIFICLS`) en in `classifier.yaml` `resultTarget` op de ontvanger
  van de tester (`port: 11200`, `aeTitle: AIFITEST`), en `matching.resultModalities: [SR]`.
  Elke test is dan geslaagd zodra het SR terugkomt (hier getest: 3 van 3 geslaagd, ~3 s).
- De log noemt studies alleen met een korte hash. `results\`, `held\`, `failed\` en
  `spool\` bevatten wel patiëntgegevens of UID's: `install-service.ps1` beperkt de toegang.

## Add-on in de AIFI-gateway

De AIFI Anonymization Gateway (vanaf 2.1) kan elke gepseudonimiseerde studie door de
classifier laten bekijken voordat hij haar aan de DICOM Web Proxy aanbiedt, en in
`mode: enforce` een verkeerd gerouteerde studie tegenhouden (bijvoorbeeld: de thorax-route
accepteert alleen studies met een CT-serie waarop de thorax staat). Volledige uitleg:
handleiding van de gateway, §11. Twee varianten:

```yaml
# gateway.yaml - via de dienst (model blijft geladen; aanbevolen)
addons:
  - name: classifier
    url: http://127.0.0.1:8765/classify
    token: "<api.token uit classifier.yaml>"
    routes: [ai-thorax]
    mode: observe                  # eerst kijken, later enforce
    requireModalities: [CT]
    requireRegions: [chest]

# ... of zonder dienst: de gateway start per studie infer.py
#  - name: classifier
#    type: command
#    command: ['C:\dicom-classifier\.venv\Scripts\python.exe', 'C:\dicom-classifier\infer.py',
#              --ckpt, 'C:\dicom-classifier\runs\v1\best.pt',
#              --input, '{studyDir}', --out, '{resultFile}', --format, json]
```

```yaml
# classifier.yaml - alleen als add-on
scp: {enabled: false}
api:
  bindAddress: 127.0.0.1
  port: 8765
  token: "<lang willekeurig wachtwoord>"
  allowedRoots: ['C:\aifi-gateway\pool']   # alleen de pool van de gateway mag geclassificeerd worden
```

HTTP-API: `POST /classify` met `{"path": "<map>"}` ➜ `{"verdict": ..., "series": [...], "classifier": {...}}`;
`GET /health`. Met `api.token` is de header `X-Api-Token` verplicht. Buiten 127.0.0.1 weigert
de dienst te starten zonder token en `allowedRoots`.

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

Getest (Linux, Python 3.13, CPU):
- 68 unit tests (44 kern + 24 nieuw): Windows-padlogica (`\\?\`, UNC) gesimuleerd met `ntpath`,
  portable manifest, CSV zoals Excel hem bewaart (BOM, `;`, Windows-1252), JSON-oordeel, SR
  (geldig, juiste studie, verwijzingen), de dienst end-to-end over echte DICOM-associaties
  (ontvangen ➜ SR ➜ doorsturen, held, retry ➜ failed, onbekende AE geweigerd) en de HTTP-API
  (token, allowedRoots, foutcodes).
- `run_smoke_test.ps1` en `start_service.ps1` gedraaid met PowerShell 7.4 (op Linux, met `/` in
  de paden): rooktest groen, en een fout stopt het script met de exitcode.
- Keten met de echte AIFI Gateway Tester: 3 CT-studies ➜ dienst ➜ SR terug ➜ 3 van 3 geslaagd.
- Gateway-add-on tegen de echte dienst en `infer.py --format json`: `aifi-gateway check` OK
  voor beide typen; enforce laat een CT door en houdt een MR tegen op een CT-route.

**Niet** getest: echte Windows (PowerShell 5.1, `.venv\Scripts\python.exe`, spawn-workers op
Windows, `install-service.ps1`/WinSW), lange paden op echte Windows, GPU.

Eerder getest (versie 1.0):
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
