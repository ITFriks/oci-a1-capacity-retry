<#
.SYNOPSIS
    Install oci-a1-capacity-retry for the current Windows user (no administrator rights).

.DESCRIPTION
    - finds Python 3.11+ (py launcher or python.exe)
    - installs with pipx if available, otherwise into a private virtual environment
      under %LOCALAPPDATA%\oci-a1-capacity-retry\venv
    - creates %APPDATA%\oci-a1-capacity-retry\config.toml from the example if missing
    - explains the OCI config requirements and runs 'oci-a1-retry doctor'
    Nothing system-wide is changed and PATH is not modified.

.PARAMETER Venv
    Always use the private virtual environment, even if pipx is installed.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#>
[CmdletBinding()]
param(
    [switch]$Venv
)

# 'Continue', not 'Stop': Windows PowerShell 5.1 turns harmless stderr output of native
# commands (pip, pipx) into terminating errors when output is redirected. Every native
# command's exit code is checked explicitly instead; cmdlets use -ErrorAction Stop.
$ErrorActionPreference = 'Continue'
$App = 'oci-a1-capacity-retry'
$RepoDir = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $env:LOCALAPPDATA "$App\venv"
$ConfigDir = Join-Path $env:APPDATA $App
$ConfigFile = Join-Path $ConfigDir 'config.toml'

function Find-Python {
    $candidates = @(
        @('py', '-3.13'), @('py', '-3.12'), @('py', '-3.11'), @('python'), @('python3')
    )
    foreach ($candidate in $candidates) {
        $exe = $candidate[0]
        $pyArgs = @($candidate | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        & $exe @pyArgs -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { return , $candidate }
    }
    return $null
}

function Invoke-Checked {
    param([string]$What, [scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)." }
}

$python = Find-Python
if (-not $python) {
    Write-Host 'Python 3.11 or newer was not found.' -ForegroundColor Red
    Write-Host 'Install it from https://www.python.org/downloads/windows/ (keep the "py launcher" option) and re-run this script.'
    exit 1
}
$pyExe = $python[0]
$pyArgs = @($python | Select-Object -Skip 1)
Write-Host "Using: $($python -join ' ') ($(& $pyExe @pyArgs --version))"

$pipx = Get-Command pipx -ErrorAction SilentlyContinue
if ($pipx -and -not $Venv) {
    Write-Host 'Installing with pipx...'
    Invoke-Checked 'pipx install' { pipx install --force $RepoDir }
    $Exe = Join-Path $HOME '.local\bin\oci-a1-retry.exe'
    if ($env:PIPX_BIN_DIR) { $Exe = Join-Path $env:PIPX_BIN_DIR 'oci-a1-retry.exe' }
}
else {
    Write-Host "Installing into a private virtual environment: $VenvDir"
    Invoke-Checked 'Creating the virtual environment' { & $pyExe @pyArgs -m venv --clear $VenvDir }
    $venvPython = Join-Path $VenvDir 'Scripts\python.exe'
    Invoke-Checked 'Upgrading pip' { & $venvPython -m pip install --quiet --upgrade pip }
    Invoke-Checked 'pip install' { & $venvPython -m pip install --quiet $RepoDir }
    $Exe = Join-Path $VenvDir 'Scripts\oci-a1-retry.exe'
}
Write-Host "Installed: $Exe" -ForegroundColor Green

if (Test-Path $ConfigFile) {
    Write-Host "Keeping existing config: $ConfigFile"
}
else {
    New-Item -ItemType Directory -Force -Path $ConfigDir -ErrorAction Stop | Out-Null
    Copy-Item -Path (Join-Path $RepoDir 'config.example.toml') -Destination $ConfigFile -ErrorAction Stop
    Write-Host "Created $ConfigFile - open it (notepad `"$ConfigFile`") and set stack_ocid."
}

$ociConfig = Join-Path $env:USERPROFILE '.oci\config'
Write-Host ''
Write-Host 'OCI authentication'
Write-Host '------------------'
Write-Host "This tool uses the standard OCI config file: $ociConfig"
Write-Host '(profile DEFAULT unless you pass --profile). It never stores credentials itself.'
if (-not (Test-Path $ociConfig)) {
    Write-Host 'No OCI config found yet. To create one:'
    Write-Host '  * OCI Console > Profile (top right) > My profile > API keys > Add API key.'
    Write-Host "    Save the private key as $env:USERPROFILE\.oci\oci_api_key.pem and paste the"
    Write-Host "    shown snippet into $ociConfig with key_file=~/.oci/oci_api_key.pem"
    Write-Host '  * or, with the OCI CLI installed: oci setup config'
}
Write-Host "Your user needs IAM permission to 'read orm-stacks' and 'manage orm-jobs'."
Write-Host ''
Write-Host "Running 'oci-a1-retry doctor' now:"
& $Exe doctor
if ($LASTEXITCODE -ne 0) {
    Write-Host '(doctor reported problems - fix them, then run doctor again)' -ForegroundColor Yellow
}

Write-Host ''
Write-Host 'Next steps (PowerShell):'
Write-Host "  & `"$Exe`" doctor"
Write-Host "  & `"$Exe`" status"
Write-Host "  & `"$Exe`" once"
Write-Host "  & `"$Exe`" run"
Write-Host 'Optional start at logon: scripts\install-windows-task.ps1'
exit 0
