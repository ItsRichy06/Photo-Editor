# Publishes a release to the portal's release directory on the NAS SSD, so
# installed copies can discover it (app/updater.py, over HTTPS) and the
# download page can offer it (https://nas.taila13a0a.ts.net/apps/photo-editor).
#
#   1. Bump app/version.py's __version__ and add a matching top entry to
#      CHANGELOG.md.
#   2. powershell -ExecutionPolicy Bypass -File installer\build.ps1
#   3. powershell -ExecutionPolicy Bypass -File installer\publish.ps1 -Version 1.18.0
#
# Publishes, per release:
#   PhotoEditor-Setup-<ver>.exe   full installer (~2.7 GB) -- new installs, and
#                                 any install whose runtime differs
#   PhotoEditor-Update-<ver>.exe  app code only (~2 MB) -- installs whose
#                                 runtime fingerprint matches (runtime_id.py)
#   photoeditor-latest.json       {version, installer, sha256, update_installer,
#                                  update_sha256, runtime_id, releaseDate, notes}
# The portal serves the manifest from feed/ and each installer from
# download/<name> -- only the names the manifest gives -- so the full
# installer must be published every release: the download page's main row
# is exactly what "installer" names. Old versioned installers are never
# served again once the manifest moves on, and can be deleted at will.
#
# This used to publish to \\NAS\FamilyStorage\shares\Personal\fotoeditor, a
# share that only resolves on the home LAN (so remote installs never saw an
# update) and that left every file owned by the NAS guest account, which
# forced timestamped filenames. The SSD share keeps files owned by the
# publishing user, so versioned names can simply be overwritten on a
# re-publish.
#
# Learned from Richard's other desktop app, not rediscovered here: a manifest
# naming a file that isn't the one actually uploaded fails COMPLETELY
# SILENTLY on the client. So every file is re-hashed on the share after
# copying, and the manifest is written last, only once everything it names is
# confirmed present and byte-correct.
#
# -FeedDir / -PortalNotes exist so the logic can be tested against local
# folders; normal runs leave them at the real NAS locations.
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$FeedDir = "\\NAS\SSD\FrontEnd\releases\photo-editor",
    [string]$PortalNotes = "\\NAS\SSD\FrontEnd\apps\photo-editor\CHANGELOG.md"
)

$ErrorActionPreference = "Stop"

$repo = Split-Path $PSScriptRoot -Parent
$out = Join-Path $repo "installer_output"
$fullSource = Join-Path $out "PhotoEditor-Setup.exe"
$updateSource = Join-Path $out "PhotoEditor-Update.exe"
$builtRuntimeIdFile = Join-Path $out "runtime_id.txt"
$versionFile = Join-Path $repo "app\version.py"
$changelog = Join-Path $repo "CHANGELOG.md"
$manifestName = "photoeditor-latest.json"  # keep in sync with MANIFEST_NAME in app/updater.py

function Copy-OverwritingStale([string]$Source, [string]$Dest) {
    # Overwrites $Dest. If the share refuses (the old HDD share's guest-owned
    # files did; the SSD share shouldn't), rename the existing copy aside --
    # a rename only needs rights on the folder -- and write a fresh file. If
    # even that fails, put the old copy back rather than leave nothing there.
    # Returns the aside name, or $null if a plain overwrite worked.
    try {
        Copy-Item $Source $Dest -Force -ErrorAction Stop
        return $null
    } catch {
        if (-not (Test-Path $Dest)) { throw }
        $aside = "$(Split-Path $Dest -Leaf).replaced-$(Get-Date -Format 'yyyyMMddHHmmss')"
        Write-Host "Couldn't overwrite $Dest ($($_.Exception.Message)) -- renaming the old copy to $aside and retrying ..."
        Rename-Item $Dest $aside -ErrorAction Stop
        try {
            Copy-Item $Source $Dest -ErrorAction Stop
        } catch {
            Rename-Item (Join-Path (Split-Path $Dest -Parent) $aside) (Split-Path $Dest -Leaf) -ErrorAction SilentlyContinue
            throw
        }
        return $aside
    }
}

function Publish-Verified([string]$Source, [string]$Name) {
    $dest = Join-Path $FeedDir $Name
    Write-Host "Hashing $Source ..."
    $sha = (Get-FileHash $Source -Algorithm SHA256).Hash.ToLower()
    Write-Host "Copying to $dest ..."
    [void](Copy-OverwritingStale $Source $dest)
    Write-Host "Verifying the copy on the share ..."
    $copied = (Get-FileHash $dest -Algorithm SHA256).Hash.ToLower()
    if ($copied -ne $sha) {
        throw "$Name on the share hashes to $copied, not $sha -- NOT publishing the manifest. A manifest published now would advertise a broken update to every installed copy."
    }
    return $sha
}

# --- refuse anything that doesn't line up -----------------------------------
foreach ($f in $fullSource, $updateSource, $builtRuntimeIdFile) {
    if (-not (Test-Path $f)) { throw "Missing $f -- build first: installer\build.ps1" }
}
if (-not (Get-Content $versionFile -Raw).Contains("__version__ = ""$Version""")) {
    throw "app/version.py does not say $Version -- bump it first"
}
if (-not (Get-Content $changelog -Raw).Contains("## [$Version]")) {
    throw "CHANGELOG.md has no entry for $Version -- add one first"
}
foreach ($f in $fullSource, $updateSource) {
    $built = "$((Get-Item $f).VersionInfo.ProductVersion)".Trim()  # Inno pads it with spaces
    if ($built -ne $Version) {
        throw "$(Split-Path $f -Leaf) was built as version '$built', not $Version -- rebuild with installer\build.ps1"
    }
}
# The update-only installer is only correct for the runtime it was built
# against; if the runtime folder changed since the build, the fingerprint in
# the manifest would no longer describe what the full installer contains.
$runtimeId = (Get-Content $builtRuntimeIdFile -Raw).Trim()
$python = Join-Path $repo ".venv\Scripts\python.exe"
$currentId = (& $python (Join-Path $PSScriptRoot "runtime_id.py") --check).Trim()
if ($LASTEXITCODE -ne 0 -or $currentId -ne $runtimeId) {
    throw "runtime_build\runtime changed since the build (built $runtimeId, now $currentId) -- rebuild with installer\build.ps1"
}
if (-not (Test-Path $FeedDir)) {
    throw "Release directory not reachable: $FeedDir"
}

# --- installers first, verified --------------------------------------------
$fullName = "PhotoEditor-Setup-$Version.exe"
$updateName = "PhotoEditor-Update-$Version.exe"
$fullSha = Publish-Verified $fullSource $fullName
$updateSha = Publish-Verified $updateSource $updateName

# --- manifest last -----------------------------------------------------------
$manifest = [ordered]@{
    version          = $Version
    installer        = $fullName
    sha256           = $fullSha
    update_installer = $updateName
    update_sha256    = $updateSha
    runtime_id       = $runtimeId
    releaseDate      = (Get-Date -Format "yyyy-MM-dd")
    notes            = "See CHANGELOG.md"
} | ConvertTo-Json
$manifestPath = Join-Path $env:TEMP $manifestName
# No-BOM UTF-8: Windows PowerShell 5.1's Set-Content -Encoding utf8 writes a BOM.
[System.IO.File]::WriteAllText($manifestPath, $manifest, (New-Object System.Text.UTF8Encoding $false))
try {
    $manifestAside = Copy-OverwritingStale $manifestPath (Join-Path $FeedDir $manifestName)
} catch {
    throw "Could not write $manifestName to $FeedDir ($($_.Exception.Message)) -- the installers are on the share, but the update feed was NOT published, so no installed copy will see $Version. Clear whatever's blocking it and re-run."
}
$published = Get-Content (Join-Path $FeedDir $manifestName) -Raw | ConvertFrom-Json
if ($published.version -ne $Version -or $published.installer -ne $fullName -or $published.sha256 -ne $fullSha -or
    $published.update_installer -ne $updateName -or $published.update_sha256 -ne $updateSha -or $published.runtime_id -ne $runtimeId) {
    throw "$manifestName on the share doesn't read back as what was just written -- check $FeedDir by hand."
}

Write-Host "Published Photo Editor $Version -> $FeedDir"
Write-Host ("  full:      {0} ({1:N2} GB)" -f $fullName, ((Get-Item $fullSource).Length / 1GB))
Write-Host ("  update:    {0} ({1:N1} MB, runtime {2})" -f $updateName, ((Get-Item $updateSource).Length / 1MB), $runtimeId)
if ($manifestAside) { Write-Host "  (previous manifest kept as $manifestAside)" }

# Mirror the release notes to the portal, which renders them on the app's
# page. Non-fatal and last: the release is already published by this point,
# so a failed notes copy must not report it as failed.
try {
    $notesAside = Copy-OverwritingStale $changelog $PortalNotes
    Write-Host "  notes:     -> $PortalNotes$(if ($notesAside) { " (previous kept as $notesAside)" })"
} catch {
    Write-Warning "Portal changelog copy failed ($PortalNotes): $($_.Exception.Message) -- the release itself published fine; re-copy it by hand to update the site's notes."
}
