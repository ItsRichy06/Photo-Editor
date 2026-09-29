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
#
# One more lesson, found the hard way while first setting this up rather
# than carried over from anywhere: $feedDir turned out to NOT be exclusive
# to this app -- a completely unrelated app (Video Editor) is also
# publishing into it, under a manifest filename ("latest.json") generic
# enough to collide with this app's own first attempt at one. Hence the
# "photoeditor-" prefix on $manifestName below, and the matching sanity
# check in app/updater.py that refuses to trust a manifest whose named
# installer doesn't look like this app's -- a hash check alone doesn't
# protect against reading the wrong app's (individually valid, correctly-
# hashed) feed.
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

# This share has a confirmed, recurring quirk (hit repeatedly across this
# project's history, not a one-off): a file already there can become
# unwritable to a *later* write attempt, even one from the very same
# session that created it -- ownership assigned on create apparently
# doesn't reliably stay writable afterward. A version-suffixed fallback
# name isn't enough on its own -- confirmed live, re-running this script
# after a failed attempt hit the SAME problem on that fallback name, since
# it had already been claimed (and already gone stale) by the failed
# attempt. A timestamp suffix is guaranteed fresh on every single run,
# sidestepping the whole problem rather than just moving it one name over.
# The manifest names whatever filename actually landed, so the client
# (app/updater.py) doesn't care which one it turns out to be -- this is
# exactly why it reads the name from the manifest rather than assuming it.
$installerName = "PhotoEditor-Setup.exe"
$installerDest = Join-Path $feedDir $installerName
Write-Host "Copying installer to $installerDest ..."
try {
    Copy-Item $installer $installerDest -Force -ErrorAction Stop
} catch {
    Write-Host "Couldn't write $installerDest ($($_.Exception.Message))"
    $installerName = "PhotoEditor-Setup-$Version-$(Get-Date -Format 'yyyyMMddHHmmss').exe"
    $installerDest = Join-Path $feedDir $installerName
    Write-Host "Falling back to $installerDest ..."
    Copy-Item $installer $installerDest -Force
}

Write-Host "Verifying the copy actually matches before publishing the manifest ..."
$copiedHash = (Get-FileHash $installerDest -Algorithm SHA256).Hash.ToLower()
if ($copiedHash -ne $sha256) {
    throw "Copied installer's hash ($copiedHash) doesn't match the source ($sha256) -- NOT publishing the manifest. Re-run once the share copy is confirmed good; a manifest published now would advertise a broken update to every installed copy."
}

# Manifest is built only now, using whichever installer filename actually
# made it onto the share, then goes out LAST -- once the installer it
# names is confirmed present and byte-correct -- so nothing can ever
# observe a manifest that's ahead of (or names something other than) the
# file it points to.
$manifest = [ordered]@{
    version     = $Version
    installer   = $installerName
    sha256      = $sha256
    releaseDate = (Get-Date -Format "yyyy-MM-dd")
    notes       = "See CHANGELOG.md"
} | ConvertTo-Json

$manifestName = "photoeditor-latest.json"  # keep in sync with MANIFEST_NAME in app/updater.py
$manifestPath = Join-Path $env:TEMP $manifestName
# WriteAllText with an explicit no-BOM UTF8 encoding: Windows PowerShell
# 5.1's Set-Content/Out-File -Encoding utf8 always writes a BOM, and
# there's no reason to make app/updater.py's json.loads() deal with one
# when it's this easy to avoid at the source.
[System.IO.File]::WriteAllText($manifestPath, $manifest, (New-Object System.Text.UTF8Encoding $false))

try {
    Copy-Item $manifestPath (Join-Path $feedDir $manifestName) -Force -ErrorAction Stop
} catch {
    # Unlike the installer, the manifest's filename is NOT dynamic -- the
    # client always looks for exactly this name -- so there's no automatic
    # fallback available here. If this share's write-once quirk ever hits
    # this specific file, it has to be resolved by hand (delete the stale
    # copy on the share, or ask whoever owns it to).
    throw "Could not write $manifestName to $feedDir ($($_.Exception.Message)) -- the installer copy above succeeded, but the update feed itself did NOT get published, so no installed copy will see $Version as available. Clear whatever's blocking $feedDir\$manifestName and re-run."
}

Write-Host "Published Photo Editor $Version -> $feedDir"
Write-Host "  installer: $installerName ($([math]::Round((Get-Item $installer).Length / 1GB, 2)) GB)"
Write-Host "  sha256:    $sha256"

# Mirror the release notes to the app portal, which renders them at
# https://nas.taila13a0a.ts.net/apps/photo-editor. The portal reads this copy, not
# the one in this repo -- the NAS cannot see this machine's filesystem -- so
# without this step the site keeps showing whatever the notes said the last
# time someone copied them by hand. (It sat on 1.9.0 for three weeks that way
# while 1.14.1 downloaded fine.)
#
# Deliberately non-fatal and last: the installer and the update manifest are
# already published by this point, so a release that succeeded must not be
# reported as failed just because the portal copy did not land. The version
# the site *shows* is read from the installer's filename, not from here, so
# the worst case is notes that lag by one release.
$portalNotes = "\\NAS\SSD\FrontEnd\apps\photo-editor\CHANGELOG.md"
try {
    Copy-Item $changelog $portalNotes -Force -ErrorAction Stop
    Write-Host "  notes:     -> $portalNotes"
} catch {
    Write-Warning "Portal changelog copy failed ($portalNotes): $($_.Exception.Message) -- the release itself published fine; re-copy it by hand to update the site's notes."
}
