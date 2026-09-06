param(
    [string]$GamePath = (Join-Path $PSScriptRoot '..\dwarfFortressItself')
)

$ErrorActionPreference = 'Stop'
$taskGame = (Resolve-Path -LiteralPath $GamePath).Path
$taskExecutable = Join-Path $taskGame 'Dwarf Fortress.exe'
if (-not (Test-Path -LiteralPath $taskExecutable)) {
    throw 'GamePath must contain Dwarf Fortress.exe.'
}
if (Get-Process -Name 'Dwarf Fortress' -ErrorAction SilentlyContinue) {
    throw 'Dwarf Fortress is already running. Close it before starting another instance.'
}

# DFHooks resolves its companion DLL relative to the working directory.
# Launching only the exe path can show DF while silently leaving DFHack unloaded.
# The visible game window is the operator's view of this local experiment.
$taskProcess = Start-Process -FilePath $taskExecutable -WorkingDirectory $taskGame -WindowStyle Normal -PassThru
Write-Output ('Started Dwarf Fortress, process ' + $taskProcess.Id)
