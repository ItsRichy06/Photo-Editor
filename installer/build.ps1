# Builds both installers for the version in app\version.py:
#   installer_output\PhotoEditor-Setup.exe   full install, ~2.7 GB (bundled runtime)
#   installer_output\PhotoEditor-Update.exe  app code only, a few MB, for installs
#                                            whose runtime matches (see runtime_id.py)
#
#   powershell -ExecutionPolicy Bypass -File installer\build.ps1
#
# Then publish both with installer\publish.ps1 -Version <same version>.
$ErrorActionPreference = "Stop"

$repo = Split-Path $PSScriptRoot -Parent
$iscc = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) { throw "Inno Setup compiler not found at $iscc" }
$python = Join-Path $repo ".venv\Scripts\python.exe"

$versionLine = Select-String -Path (Join-Path $repo "app\version.py") -Pattern '__version__ = "([\d.]+)"'
if (-not $versionLine) { throw "couldn't read __version__ from app\version.py" }
$version = $versionLine.Matches[0].Groups[1].Value

Write-Host "Fingerprinting the bundled runtime ..."
$runtimeId = (& $python (Join-Path $PSScriptRoot "runtime_id.py")).Trim()
if ($LASTEXITCODE -ne 0 -or $runtimeId -notmatch '^[0-9a-f]{16}$') { throw "runtime_id.py failed: $runtimeId" }
Write-Host "  runtime_id $runtimeId"

Push-Location $PSScriptRoot
try {
    Write-Host "Building full installer $version (a few minutes) ..."
    & $iscc /Q "/DMyAppVersion=$version" PhotoEditor.iss
    if ($LASTEXITCODE -ne 0) { throw "full installer build failed ($LASTEXITCODE)" }
    Write-Host "Building update-only installer $version ..."
    & $iscc /Q "/DMyAppVersion=$version" "/DRuntimeId=$runtimeId" PhotoEditor-Update.iss
    if ($LASTEXITCODE -ne 0) { throw "update installer build failed ($LASTEXITCODE)" }
} finally {
    Pop-Location
}

$out = Join-Path $repo "installer_output"
foreach ($name in "PhotoEditor-Setup.exe", "PhotoEditor-Update.exe") {
    $f = Get-Item (Join-Path $out $name)
    Write-Host ("  {0,-24} {1,10:N1} MB" -f $name, ($f.Length / 1MB))
}
# publish.ps1 reads this to put runtime_id into the manifest, so the
# published value is always the one the update installer was built with.
Set-Content (Join-Path $out "runtime_id.txt") $runtimeId -Encoding ascii
Write-Host "Built Photo Editor $version (runtime $runtimeId)"
