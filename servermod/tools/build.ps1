[CmdletBinding()]
param(
    [string]$ServerRoot = 'F:\src\livonia',
    [string]$MissionRelativePath = 'mpmissions\enoch_rb.enoch',
    [string]$ServerId = 'livonia-1',
    [string]$PythonCommand = 'python',
    [string]$AddonBuilderPath = '',
    [switch]$RefreshCatalog,
    [switch]$PreflightOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ExpectedDayZVersion = '1.29.163709'
$ServerModRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$MissionPath = Join-Path $ServerRoot $MissionRelativePath
$CatalogPath = Join-Path $ServerModRoot 'config\livonia.generated.json'
$MonitorCatalogPath = Join-Path $ServerModRoot 'config\livonia.world.generated.json'
$GeneratorPath = Join-Path $ServerModRoot 'tools\generate_event_catalog.py'
$SourcePath = Join-Path $ServerModRoot 'source\RB_Telemetry'
$ModMetadataPath = Join-Path $ServerModRoot 'package\mod.cpp'

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Program,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Description
    )
    Write-Host "[preflight] $Description"
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

if (-not (Test-Path -LiteralPath $MissionPath -PathType Container)) {
    throw "Mission directory not found: $MissionPath"
}

$RequiredMissionFiles = @(
    'db\events.xml',
    'cfgeventspawns.xml',
    'cfgeventgroups.xml',
    'cfgeffectarea.json',
    'cfgplayerspawnpoints.xml',
    'cfgenvironment.xml'
)
foreach ($RelativePath in $RequiredMissionFiles) {
    $FullPath = Join-Path $MissionPath $RelativePath
    if (-not (Test-Path -LiteralPath $FullPath -PathType Leaf)) {
        throw "Required mission file not found: $FullPath"
    }
}
Write-Host "[preflight] mission XML present: $MissionPath"

$LatestRpt = Get-ChildItem -LiteralPath $ServerRoot -Recurse -File -Filter '*.RPT' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if (-not $LatestRpt) {
    throw "No RPT file found below $ServerRoot; cannot verify DayZ $ExpectedDayZVersion baseline"
}
$RptHeader = Get-Content -LiteralPath $LatestRpt.FullName -TotalCount 32
if (-not ($RptHeader -match "^Version $([regex]::Escape($ExpectedDayZVersion))$")) {
    throw "Latest RPT does not declare Version ${ExpectedDayZVersion}: $($LatestRpt.FullName)"
}
Write-Host "[preflight] DayZ baseline ${ExpectedDayZVersion}: $($LatestRpt.FullName)"

$Python = Get-Command $PythonCommand -ErrorAction Stop
$GeneratorArguments = @(
    $GeneratorPath,
    '--mission-dir', $MissionPath,
    '--output', $CatalogPath,
    '--monitor-output', $MonitorCatalogPath,
    '--server-id', $ServerId
)
if (-not $RefreshCatalog) {
    $GeneratorArguments += '--check'
}
Invoke-Checked -Program $Python.Source -Arguments $GeneratorArguments -Description $(
    if ($RefreshCatalog) { 'refresh deterministic event catalog' } else { 'verify deterministic event catalog' }
)

$PreviousNoBytecode = $env:PYTHONDONTWRITEBYTECODE
try {
    $env:PYTHONDONTWRITEBYTECODE = '1'
    Invoke-Checked -Program $Python.Source -Arguments @(
        '-m', 'unittest', 'discover',
        '-s', (Join-Path $ServerModRoot 'tests'),
        '-v'
    ) -Description 'run serverMod tests'
}
finally {
    $env:PYTHONDONTWRITEBYTECODE = $PreviousNoBytecode
}

$BuilderCandidates = @()
if ($AddonBuilderPath) {
    $BuilderCandidates += $AddonBuilderPath
}
if ($env:DAYZ_ADDON_BUILDER) {
    $BuilderCandidates += $env:DAYZ_ADDON_BUILDER
}
$BuilderCandidates += @(
    'C:\Program Files (x86)\Steam\steamapps\common\DayZ Tools\Bin\AddonBuilder\AddonBuilder.exe',
    'C:\Program Files\Steam\steamapps\common\DayZ Tools\Bin\AddonBuilder\AddonBuilder.exe',
    'P:\Tools\Bin\AddonBuilder\AddonBuilder.exe'
)

$Builder = $BuilderCandidates |
    Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } |
    Select-Object -First 1
if (-not $Builder) {
    throw 'DayZ Tools Addon Builder was not found. Install DayZ Tools or pass -AddonBuilderPath / set DAYZ_ADDON_BUILDER.'
}
Write-Host "[preflight] Addon Builder: $Builder"

if ($PreflightOnly) {
    Write-Host '[preflight] all checks passed; no package was built'
    return
}

$DistRoot = Join-Path $ServerModRoot 'dist'
$ModRoot = Join-Path $DistRoot '@RedBastionTelemetry'
$AddonsPath = Join-Path $ModRoot 'Addons'
$ProfileConfigPath = Join-Path $DistRoot 'profiles\RBTelemetry'
$MonitorConfigPath = Join-Path $DistRoot 'monitor'
New-Item -ItemType Directory -Force -Path $AddonsPath, $ProfileConfigPath, $MonitorConfigPath | Out-Null
Copy-Item -LiteralPath $ModMetadataPath -Destination (Join-Path $ModRoot 'mod.cpp') -Force

Invoke-Checked -Program $Builder -Arguments @(
    $SourcePath,
    $AddonsPath,
    '-packonly',
    '-clear',
    '-prefix=RB_Telemetry'
) -Description 'build RB_Telemetry PBO'

$PboPath = Join-Path $AddonsPath 'RB_Telemetry.pbo'
if (-not (Test-Path -LiteralPath $PboPath -PathType Leaf)) {
    throw "Addon Builder returned success but did not create $PboPath"
}
Copy-Item -LiteralPath $CatalogPath -Destination (Join-Path $ProfileConfigPath 'config.json') -Force
Copy-Item -LiteralPath $MonitorCatalogPath -Destination (Join-Path $MonitorConfigPath 'livonia.world.json') -Force

$PboHash = (Get-FileHash -LiteralPath $PboPath -Algorithm SHA256).Hash.ToLowerInvariant()
$ConfigHash = (Get-FileHash -LiteralPath (Join-Path $ProfileConfigPath 'config.json') -Algorithm SHA256).Hash.ToLowerInvariant()
$MonitorHash = (Get-FileHash -LiteralPath (Join-Path $MonitorConfigPath 'livonia.world.json') -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Host "[build] PBO: $PboPath (sha256:$PboHash)"
Write-Host "[build] config: $(Join-Path $ProfileConfigPath 'config.json') (sha256:$ConfigHash)"
Write-Host "[build] monitor catalog: $(Join-Path $MonitorConfigPath 'livonia.world.json') (sha256:$MonitorHash)"
Write-Host '[build] artifacts created locally; nothing was deployed or added to a server launch command'
