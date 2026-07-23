[CmdletBinding()]
param(
    [string]$PythonExecutable = $env:TRACELOG_PYTHON
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$desktopDir = $PSScriptRoot
$projectRoot = Split-Path -Parent $desktopDir
$sourceIcon = Join-Path $projectRoot 'frontend\public\brand\tracelog-icon-transparent-1024.png'
$engineDist = Join-Path $desktopDir 'dist\engine'
$engineWork = Join-Path $desktopDir 'build\pyinstaller'
$engineSpec = Join-Path $desktopDir 'engine.spec'
$engineExecutable = Join-Path $engineDist 'tracelog-engine\tracelog-engine.exe'
$iconOutput = Join-Path $desktopDir 'build\icon.ico'

if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $condaPython = Join-Path $env:USERPROFILE '.conda\envs\tracelog\python.exe'
    if (Test-Path -LiteralPath $condaPython -PathType Leaf) {
        $PythonExecutable = $condaPython
    } else {
        $PythonExecutable = (Get-Command python -ErrorAction Stop).Source
    }
}
if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
    throw "Python executable not found: $PythonExecutable"
}

$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if ($null -eq $npmCommand) {
    $npmCommand = Get-Command npm -ErrorAction Stop
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory)]
        [string]$FilePath,
        [Parameter(Mandatory)]
        [string[]]$ArgumentList
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath exited with code $LASTEXITCODE"
    }
}

Push-Location $projectRoot
try {
    Invoke-Checked $npmCommand.Source @('--prefix', 'frontend', 'ci')
    Invoke-Checked $npmCommand.Source @('--prefix', 'frontend', 'run', 'build')

    & $PythonExecutable -c 'import PyInstaller'
    if ($LASTEXITCODE -ne 0) {
        throw (
            "PyInstaller is missing. Install it with: " +
            "& '$PythonExecutable' -m pip install -r desktop\requirements-build.txt"
        )
    }

    Invoke-Checked $PythonExecutable @(
        '-m',
        'PyInstaller',
        '--clean',
        '--noconfirm',
        '--distpath',
        $engineDist,
        '--workpath',
        $engineWork,
        $engineSpec
    )

    Invoke-Checked $PythonExecutable @(
        (Join-Path $desktopDir 'scripts\smoke_engine.py'),
        $engineExecutable
    )
    Invoke-Checked $PythonExecutable @(
        (Join-Path $desktopDir 'scripts\make_icon.py'),
        $sourceIcon,
        $iconOutput
    )

    Invoke-Checked $npmCommand.Source @('--prefix', 'desktop', 'ci')
    Invoke-Checked $npmCommand.Source @('--prefix', 'desktop', 'run', 'dist:win')
    Invoke-Checked $PythonExecutable @(
        (Join-Path $desktopDir 'scripts\smoke_shell.py'),
        (Join-Path $desktopDir 'dist\shell\win-unpacked\TraceLog 拾迹.exe')
    )
}
finally {
    Pop-Location
}

Write-Host "Windows desktop artifacts are in $(Join-Path $desktopDir 'dist\shell')"
