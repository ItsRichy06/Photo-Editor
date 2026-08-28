# Publishes a new installer + update-feed manifest to the NAS distribution
# share, so already-installed copies of the app can discover and offer it
# (see app/updater.py). Modeled on Richard's other desktop app's own
# site/publish.ps1 -- same core idea (installer + a small version-manifest
# file, both required, both must actually agree), simplified since this app
# has one build target and no separate download page to keep in sync.
#
# Run after building the installer:
#   1. Bump app/version.py's __version__ and add a matching top entry to
#      CHANGELOG.md.
#   2. Rebuild: ISCC.exe installer\PhotoEditor.iss
#   3. powershell -ExecutionPolicy Bypass -File installer\publish.ps1 -Version 1.9.0
#
# Learned from that other app's history, not rediscovered here: a manifest
# that names a file which isn't actually the one that got uploaded fails
# COMPLETELY SILENTLY on the client (electron-updater there, app/updater.py
# here) -- no dialog, no visible error anywhere, just a swallowed exception
# in a background thread, for as long as nobody happens to check the logs.
# This script re-hashes the file it just copied and refuses to publish the
# manifest unless that hash matches what it's about to advertise.
param([Parameter(Mandatory = $true)][string]$Version)

$ErrorActionPreference = "Stop"

$repo = Split-Path $PSScriptRoot -Parent
$installer = Join-Path $repo "installer_output\PhotoEditor-Setup.exe"
$versionFile = Join-Path $repo "app\version.py"
$changelog = Join-Path $repo "CHANGELOG.md"
# Keep this in sync with UPDATE_FEED_DIR in app/updater.py -- a UNC path,
# not a drive letter, for the same reason: Z: is just how this machine
# happens to have \\NAS\FamilyStorage mapped right now.
$feedDir = "\\NAS\FamilyStorage\shares\Personal\fotoeditor"

if (-not (Test-Path $installer)) {
    throw "Installer not found: $installer -- build it first (ISCC.exe installer\PhotoEditor.iss)"
}
if (-not (Get-Content $versionFile -Raw).Contains("__version__ = ""$Version""")) {
    throw "app/version.py does not say $Version -- bump it first"
}
if (-not (Get-Content $changelog -Raw).Contains("## [$Version]")) {
    throw "CHANGELOG.md has no entry for $Version -- add one first"
}
if (-not (Test-Path $feedDir)) {
    throw "Distribution share not reachable: $feedDir"
}

Write-Host "Hashing $installer ..."
$sha256 = (Get-FileHash $installer -Algorithm SHA256).Hash.ToLower()

$installerName = "PhotoEditor-Setup.exe"
$manifest = [ordered]@{
    version     = $Version
    installer   = $installerName
    sha256      = $sha256
    releaseDate = (Get-Date -Format "yyyy-MM-dd")
    notes       = "See CHANGELOG.md"
} | ConvertTo-Json

$manifestPath = Join-Path $env:TEMP "latest.json"
# WriteAllText with an explicit no-BOM UTF8 encoding: Windows PowerShell
# 5.1's Set-Content/Out-File -Encoding utf8 always writes a BOM, and
# there's no reason to make app/updater.py's json.loads() deal with one
# when it's this easy to avoid at the source.
[System.IO.File]::WriteAllText($manifestPath, $manifest, (New-Object System.Text.UTF8Encoding $false))

Write-Host "Copying installer to $feedDir ..."
Copy-Item $installer (Join-Path $feedDir $installerName) -Force

Write-Host "Verifying the copy actually matches before publishing the manifest ..."
$copiedHash = (Get-FileHash (Join-Path $feedDir $installerName) -Algorithm SHA256).Hash.ToLower()
if ($copiedHash -ne $sha256) {
    throw "Copied installer's hash ($copiedHash) doesn't match the source ($sha256) -- NOT publishing the manifest. Re-run once the share copy is confirmed good; a manifest published now would advertise a broken update to every installed copy."
}

# The manifest goes out LAST, only once the installer it names is confirmed
# present and byte-correct on the share -- so nothing can ever observe a
# manifest that's ahead of the file it points to.
Copy-Item $manifestPath (Join-Path $feedDir "latest.json") -Force

Write-Host "Published Photo Editor $Version -> $feedDir"
Write-Host "  installer: $installerName ($([math]::Round((Get-Item $installer).Length / 1GB, 2)) GB)"
Write-Host "  sha256:    $sha256"
