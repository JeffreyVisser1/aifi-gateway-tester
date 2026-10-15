# Rooktest op Windows (PowerShell 5.1 of 7): synthetische DICOM -> manifest -> korte training ->
# inferentie (CSV en JSON) -> unit tests. Draait op CPU in een paar minuten.
#
#   .\run_smoke_test.ps1                 # werkmap .\smoke
#   .\run_smoke_test.ps1 -Work D:\smoke
#
# Gebruikt .venv\Scripts\python.exe als die bestaat, anders 'python' uit het PATH.
# Stopt bij de eerste fout (ook als een python-commando met een foutcode eindigt).
param(
    [string]$Work = ".\smoke"
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Set-Location -LiteralPath $PSScriptRoot

$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { $Python = "python" }

function Invoke-Py {
    # Native programma's zetten $ErrorActionPreference niet om in een fout: exitcode zelf controleren.
    & $Python @args
    if ($LASTEXITCODE -ne 0) { throw "Mislukt (exitcode $LASTEXITCODE): python $($args -join ' ')" }
}

if (Test-Path -LiteralPath $Work) { Remove-Item -LiteralPath $Work -Recurse -Force }

Invoke-Py tests\make_synthetic.py --out "$Work\synth" --per-class 6
Invoke-Py build_dataset.py --input "$Work\synth" --out "$Work\work"
Invoke-Py train.py --manifest "$Work\work\manifest.jsonl" --out "$Work\run" --encoder resnet18 --init none `
    --k 4 --img-size 96 --epochs 4 --batch-size 8 --lr 1e-3 --workers 2
Invoke-Py infer.py --ckpt "$Work\run\best.pt" --input "$Work\synth" --out "$Work\predictions.csv"
Invoke-Py infer.py --ckpt "$Work\run\best.pt" --input "$Work\synth\CT_CHEST_0" --out "$Work\ct_chest.json" --format json
Invoke-Py -m pytest -q tests\

Write-Host "Klaar. Zie $Work\predictions.csv, $Work\ct_chest.json en $Work\run\metrics.json"
