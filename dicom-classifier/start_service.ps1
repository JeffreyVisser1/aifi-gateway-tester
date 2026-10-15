# Start de DICOM-dienst (ontvanger + HTTP-API voor de gateway-add-on) in dit venster.
#
#   .\start_service.ps1                         # gebruikt .\classifier.yaml
#   .\start_service.ps1 -Config D:\cls\classifier.yaml
#   .\start_service.ps1 -Check                  # alleen configuratie en model controleren
#
# De eerste keer wordt classifier.yaml aangemaakt; vul model.checkpoint in en start opnieuw.
param(
    [string]$Config = ".\classifier.yaml",
    [switch]$Check
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { $Python = "python" }

if (-not (Test-Path -LiteralPath $Config)) {
    & $Python dicom_service.py --init-config $Config
    exit $LASTEXITCODE
}
$extra = @()
if ($Check) { $extra += "--check" }
& $Python dicom_service.py --config $Config @extra
exit $LASTEXITCODE
