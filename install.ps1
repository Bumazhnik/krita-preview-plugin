# Installs the plugin into Krita's user pykrita folder.
# The package folder is linked (junction), so edits in this repo apply after a Krita restart.
$ErrorActionPreference = 'Stop'
$pykrita = Join-Path $env:APPDATA 'krita\pykrita'
New-Item -ItemType Directory -Force $pykrita | Out-Null

$link = Join-Path $pykrita 'realtime_overview'
if (Test-Path $link) { (Get-Item $link).Delete() }   # removes the junction/dir entry only
New-Item -ItemType Junction -Path $link -Target (Join-Path $PSScriptRoot 'realtime_overview') | Out-Null
Copy-Item (Join-Path $PSScriptRoot 'realtime_overview.desktop') $pykrita -Force

Write-Host "Installed to $pykrita"
Write-Host "Enable it: Settings > Configure Krita > Python Plugin Manager > 'Realtime Overview', then restart Krita."
