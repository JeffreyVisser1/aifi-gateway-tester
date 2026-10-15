<#
.SYNOPSIS
  Installs the dicom-classifier (dicom_service.py) as a Windows service.
.DESCRIPTION
  Run from an elevated PowerShell in the dicom-classifier folder, after the venv (.venv) and
  classifier.yaml exist (README, section Windows):

    .\windows\install-service.ps1                          # service runs as LocalSystem
    .\windows\install-service.ps1 -ServiceUser 'DOMAIN\svc-aifi'

  - Downloads WinSW v2.12.0 (WinSW-x64.exe) once and checks its SHA-256 (the same pin as the
    AIFI gateway build). Without internet: put WinSW-x64.exe as dicom-classifier-service.exe
    in this folder yourself; the hash is still checked.
  - spool\, held\, failed\, results\ and logs\ (patient data and UIDs) get an ACL without
    inheritance: only Administrators, SYSTEM and the service account.
  - The service account password is asked for here (or taken from AIFICLS_SVC_PASSWORD),
    written into the service XML only for the install, and removed again in a finally.
  Keep this file ASCII-only: Windows PowerShell 5.1 reads BOM-less scripts in the ANSI code page.
#>
[CmdletBinding()]
param(
    [string]$ServiceUser = 'LocalSystem',
    [string]$Config = 'classifier.yaml'
)
$ErrorActionPreference = 'Stop'
$Base = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$xmlSrc = Join-Path $PSScriptRoot 'dicom-classifier-service.xml'
$xmlPath = Join-Path $Base 'dicom-classifier-service.xml'
$exePath = Join-Path $Base 'dicom-classifier-service.exe'
$python = Join-Path $Base '.venv\Scripts\python.exe'
$winswUrl = 'https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe'
$winswSha256 = '05B82D46AD331CC16BDC00DE5C6332C1EF818DF8CEEFCD49C726553209B3A0DA'

if (-not (Test-Path -LiteralPath $python)) { throw "Missing $python - create the venv first (README, section Windows)." }
if (-not (Test-Path -LiteralPath (Join-Path $Base $Config))) {
    throw "Missing $Config - run .\start_service.ps1 once to create it, fill in model.checkpoint, then run this again."
}
& $python (Join-Path $Base 'dicom_service.py') --config (Join-Path $Base $Config) --check
if ($LASTEXITCODE -ne 0) { throw "Configuration or model check failed (exit $LASTEXITCODE)." }

if (-not (Test-Path -LiteralPath $exePath)) {
    Write-Host "Downloading WinSW v2.12.0..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -Uri $winswUrl -OutFile $exePath
}
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $exePath).Hash
if ($hash -ne $winswSha256) {
    Remove-Item -LiteralPath $exePath
    throw "WinSW SHA-256 mismatch ($hash); file removed."
}

Write-Host "Restricting access to spool, held, failed, results and logs..."
foreach ($sub in @('spool', 'held', 'failed', 'results', 'logs')) {
    $dir = Join-Path $Base $sub
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
    # *S-1-5-32-544 = BUILTIN\Administrators, *S-1-5-18 = SYSTEM (language-independent SIDs)
    $grants = @('*S-1-5-32-544:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F')
    if ($ServiceUser -ne 'LocalSystem') { $grants += "${ServiceUser}:(OI)(CI)M" }
    & icacls $dir /inheritance:r /grant:r $grants /T /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls failed on $dir (exit $LASTEXITCODE)." }
}

$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$original = [System.IO.File]::ReadAllText($xmlSrc, $utf8NoBom)
if ($Config -ne 'classifier.yaml') { $original = $original.Replace('classifier.yaml', $Config) }
try {
    $doc = New-Object System.Xml.XmlDocument
    $doc.LoadXml($original)
    if ($ServiceUser -ne 'LocalSystem') {
        $password = $env:AIFICLS_SVC_PASSWORD
        if (-not $password) {
            $secure = Read-Host "Password for $ServiceUser" -AsSecureString
            $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
            try { $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
            finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
        }
        if (-not $password) { throw "No password given for $ServiceUser." }
        $svc = $doc.SelectSingleNode('/service')
        $acct = $doc.CreateElement('serviceaccount')
        $u = $doc.CreateElement('user');              $u.InnerText = $ServiceUser; [void]$acct.AppendChild($u)
        $p = $doc.CreateElement('password');          $p.InnerText = $password;    [void]$acct.AppendChild($p)
        $a = $doc.CreateElement('allowservicelogon'); $a.InnerText = 'true';       [void]$acct.AppendChild($a)
        [void]$svc.AppendChild($acct)
    }
    $writer = New-Object System.IO.StreamWriter($xmlPath, $false, $utf8NoBom)
    try { $doc.Save($writer) } finally { $writer.Close() }
    Write-Host "Installing service..."
    & $exePath install
    if ($LASTEXITCODE -ne 0) { throw "WinSW install failed (exit $LASTEXITCODE)." }
}
finally {
    # The installed service keeps its account; the XML on disk never keeps the password.
    $clean = $original
    [System.IO.File]::WriteAllText($xmlPath, $clean, $utf8NoBom)
    Remove-Item -ErrorAction SilentlyContinue Env:\AIFICLS_SVC_PASSWORD
}
Write-Host "Starting service..."
& $exePath start
if ($LASTEXITCODE -ne 0) { throw "Service start failed (exit $LASTEXITCODE). See $Base\logs\." }
Write-Host "Service running. Log: $Base\logs\dicom_service.log"
Write-Host "Stop / start / remove: .\dicom-classifier-service.exe stop | start | uninstall"
