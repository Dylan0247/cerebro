# Generic launcher: no account name, local path or machine identifier.
param(
    [string]$Distribution = 'Ubuntu',
    [Parameter(Mandatory = $true)][string]$LinuxLauncher
)
$ErrorActionPreference = 'Stop'
while ($true) {
    & "$env:SystemRoot\System32\wsl.exe" -d $Distribution -- /bin/bash $LinuxLauncher
    $cerebroExit = $LASTEXITCODE
    if ($cerebroExit -eq 0) { break }
    Start-Sleep -Seconds 30
}

