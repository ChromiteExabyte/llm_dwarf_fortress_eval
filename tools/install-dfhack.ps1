param(
    [string]$GamePath = (Join-Path $PSScriptRoot '..\dwarfFortressItself')
)

$ErrorActionPreference = 'Stop'
$taskGame = (Resolve-Path -LiteralPath $GamePath).Path
$taskRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if (-not (Test-Path -LiteralPath (Join-Path $taskGame 'Dwarf Fortress.exe'))) {
    throw 'GamePath must contain Dwarf Fortress.exe.'
}
if (Test-Path -LiteralPath (Join-Path $taskGame 'hack')) {
    throw 'DFHack is already present. This installer only supports a fresh installation.'
}
$taskReleaseNotes = Get-Content -LiteralPath (Join-Path $taskGame 'release notes.txt') -Raw
$taskReleaseHeading = [regex]::Match($taskReleaseNotes, '(?m)^Release notes for (\d+\.\d+)\b')
if (-not $taskReleaseHeading.Success -or $taskReleaseHeading.Groups[1].Value -ne '53.16') {
    throw 'This pinned DFHack package supports Dwarf Fortress 53.16.'
}
$taskRunning = Get-Process -Name 'Dwarf Fortress' -ErrorAction SilentlyContinue
if ($taskRunning) { throw 'Close Dwarf Fortress before installing DFHack.' }

$taskVersion = '53.16-r1.1'
$taskUrl = 'https://github.com/DFHack/dfhack/releases/download/53.16-r1.1/dfhack-53.16-r1.1-Windows-64bit.zip'
$taskExpectedHash = '44cf1e015feabc7d4f7a195f856d1e2e114e40b404839e0b7b9a904d18c9081f'
$taskSetup = Join-Path $taskRoot ('runs\setup\' + [guid]::NewGuid().ToString('N'))
$taskStage = Join-Path $taskSetup 'package'
$taskBackup = Join-Path $taskSetup 'original-files'
New-Item -ItemType Directory -Path $taskSetup -Force | Out-Null
$taskArchive = Join-Path $taskSetup ('dfhack-' + $taskVersion + '-Windows-64bit.zip')
Invoke-WebRequest -Uri $taskUrl -OutFile $taskArchive
$taskHash = (Get-FileHash -LiteralPath $taskArchive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($taskHash -ne $taskExpectedHash) { throw 'DFHack archive checksum mismatch; nothing was installed.' }

# Reject archive paths that could escape the staging directory.
Add-Type -AssemblyName System.IO.Compression.FileSystem
$taskZip = [System.IO.Compression.ZipFile]::OpenRead($taskArchive)
try {
    foreach ($taskEntry in $taskZip.Entries) {
        $taskCandidate = [System.IO.Path]::GetFullPath((Join-Path $taskStage $taskEntry.FullName))
        if (-not $taskCandidate.StartsWith($taskStage + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw ('Unsafe archive entry: ' + $taskEntry.FullName)
        }
    }
} finally { $taskZip.Dispose() }
Expand-Archive -LiteralPath $taskArchive -DestinationPath $taskStage
if (-not (Test-Path -LiteralPath (Join-Path $taskStage 'hack'))) {
    throw 'Unexpected DFHack archive layout; nothing was installed.'
}

# Preserve every pre-existing file replaced by the distribution.
$taskInstalled = @()
foreach ($taskFile in Get-ChildItem -LiteralPath $taskStage -File -Recurse) {
    $taskRelative = $taskFile.FullName.Substring($taskStage.Length + 1)
    $taskDestination = Join-Path $taskGame $taskRelative
    if (Test-Path -LiteralPath $taskDestination) {
        $taskOriginal = Join-Path $taskBackup $taskRelative
        New-Item -ItemType Directory -Path (Split-Path $taskOriginal -Parent) -Force | Out-Null
        Copy-Item -LiteralPath $taskDestination -Destination $taskOriginal
    }
    New-Item -ItemType Directory -Path (Split-Path $taskDestination -Parent) -Force | Out-Null
    Copy-Item -LiteralPath $taskFile.FullName -Destination $taskDestination -Force
    $taskInstalled += $taskRelative
}
$taskManifest = [ordered]@{
    version = $taskVersion
    source = $taskUrl
    sha256 = $taskHash
    game_path = $taskGame
    backup_path = $taskBackup
    installed_at_utc = [DateTime]::UtcNow.ToString('o')
    files = $taskInstalled
}
$taskManifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $taskSetup 'installation.json') -Encoding utf8
Write-Output ('Installed DFHack ' + $taskVersion + ' into ' + $taskGame)
Write-Output ('Verified SHA256: ' + $taskHash)
Write-Output ('Installation record and original files: ' + $taskSetup)
