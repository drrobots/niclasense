<#
.SYNOPSIS
    Builds the Windows installer: fetches the runtime, stages the tree, compiles the setup.

.DESCRIPTION
    Three steps, each skippable, so a failed compile does not mean downloading everything
    again:

        fetch    the embeddable CPython, the pyserial wheel, and WinSW, into downloads\
        stage    unpack those plus python\ and packaging\ into dist\stage\
        compile  run the Inno Setup compiler over dist\stage\, producing dist\*.exe

    The staged tree is exactly what gets installed, so `dist\stage\python\python.exe
    dist\stage\service\supervise.py capture --config ...` runs the installed thing without
    installing it. That is the fastest way to find out whether a packaging problem is in the
    packaging or in the app.

    Nothing is fetched on the target machine. The installer carries the interpreter, so a
    Nicla box with no internet -- which is most of them -- installs the same as any other.

.PARAMETER Verify
    Check every download against packaging\hashes.txt and fail on a mismatch. Off by
    default only because the file has to be written once from a trusted build; see -Record.

.PARAMETER Record
    Write packaging\hashes.txt from what was just downloaded, then stop. Do this once, from
    a build you trust, and commit the result -- after which every build can use -Verify.
#>
[CmdletBinding()]
param(
    [string] $PythonVersion = "3.12.10",
    [string] $PyserialVersion = "3.5",
    [string] $WinswVersion = "2.12.0",
    [string] $AppVersion = "1.0.0",
    [switch] $Verify,
    [switch] $Record,
    [switch] $SkipFetch,
    [switch] $SkipCompile
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$PackagingDir = $PSScriptRoot
$RepoDir      = Split-Path -Parent $PackagingDir
$DownloadDir  = Join-Path $PackagingDir "downloads"
$DistDir      = Join-Path $PackagingDir "dist"
$StageDir     = Join-Path $DistDir "stage"
$HashFile     = Join-Path $PackagingDir "hashes.txt"

$PythonZip  = "python-$PythonVersion-embed-amd64.zip"
$PythonUrl  = "https://www.python.org/ftp/python/$PythonVersion/$PythonZip"
$WinswExe   = "WinSW-x64-$WinswVersion.exe"
$WinswUrl   = "https://github.com/winsw/winsw/releases/download/v$WinswVersion/WinSW-x64.exe"

function Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }

function Get-File($url, $destination) {
    if (Test-Path $destination) {
        Write-Host "    have $(Split-Path -Leaf $destination)"
        return
    }
    Write-Host "    fetching $url"
    # Tls12 explicitly: Windows PowerShell 5.1 still defaults to something older, and
    # python.org refuses it.
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $destination -UseBasicParsing
}

function Read-Hashes {
    $table = @{}
    if (Test-Path $HashFile) {
        foreach ($line in Get-Content $HashFile) {
            if ($line -match '^\s*#' -or $line -match '^\s*$') { continue }
            $parts = $line -split '\s+', 2
            $table[$parts[1].Trim()] = $parts[0].Trim().ToUpper()
        }
    }
    return $table
}

# ---------------------------------------------------------------- fetch

if (-not $SkipFetch) {
    Step "fetch"
    New-Item -ItemType Directory -Force -Path $DownloadDir | Out-Null

    Get-File $PythonUrl (Join-Path $DownloadDir $PythonZip)
    Get-File $WinswUrl  (Join-Path $DownloadDir $WinswExe)

    # pyserial comes through pip rather than a hardcoded PyPI URL, so the filename, the
    # index and the integrity checking are pip's problem rather than this script's. It is
    # a pure-Python wheel, so the build machine's own interpreter is a fine one to resolve
    # it with -- nothing about the wheel depends on which Python unpacks it.
    Write-Host "    fetching pyserial==$PyserialVersion"
    & python -m pip download "pyserial==$PyserialVersion" --only-binary ":all:" `
        --dest $DownloadDir --no-deps --quiet
    if ($LASTEXITCODE -ne 0) { throw "pip download failed" }
}

$artifacts = Get-ChildItem $DownloadDir -File | Sort-Object Name

if ($Record) {
    Step "record"
    $lines = @(
        "# SHA256 of every downloaded build input. Written by build.ps1 -Record from a",
        "# build somebody trusted; checked by build.ps1 -Verify on every build after.",
        "# Regenerate deliberately, and only when a version above is changed on purpose."
    )
    foreach ($file in $artifacts) {
        $sum = (Get-FileHash $file.FullName -Algorithm SHA256).Hash
        $lines += "$sum  $($file.Name)"
    }
    Set-Content -Path $HashFile -Value $lines -Encoding ASCII
    Write-Host "    wrote $HashFile"
    return
}

if ($Verify) {
    Step "verify"
    $expected = Read-Hashes
    if ($expected.Count -eq 0) { throw "no $HashFile to verify against; run with -Record first" }
    foreach ($file in $artifacts) {
        if (-not $expected.ContainsKey($file.Name)) {
            throw "$($file.Name) is not in hashes.txt"
        }
        $sum = (Get-FileHash $file.FullName -Algorithm SHA256).Hash
        if ($sum -ne $expected[$file.Name]) {
            throw "$($file.Name) hash mismatch: got $sum, expected $($expected[$file.Name])"
        }
        Write-Host "    ok $($file.Name)"
    }
}

# ---------------------------------------------------------------- stage

Step "stage"
if (Test-Path $StageDir) { Remove-Item $StageDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path $StageDir | Out-Null

$stagePython  = Join-Path $StageDir "python"
$stageApp     = Join-Path $StageDir "app"
$stageService = Join-Path $StageDir "service"

Expand-Archive -Path (Join-Path $DownloadDir $PythonZip) -DestinationPath $stagePython

# The app is python\ minus the parts that are not the program: the test suite (which the
# CI runs before this script, from the repo), the benchmarks (which need a second sketch
# flashed), and anything left behind by running it.
New-Item -ItemType Directory -Force -Path $stageApp | Out-Null
Copy-Item (Join-Path $RepoDir "python\*.py") $stageApp
Copy-Item (Join-Path $RepoDir "python\web") $stageApp -Recurse
Copy-Item (Join-Path $RepoDir "python\example.conf") $stageApp
Copy-Item (Join-Path $RepoDir "python\requirements.txt") $stageApp

# The archive viewer's launcher and its example settings. Shipped alongside rather than in
# their own installer: the .py files they need are already here, and a machine that captures
# is also a machine somebody might want to read the archive from.
Copy-Item (Join-Path $RepoDir "python\viewer.cmd") $stageApp
Copy-Item (Join-Path $RepoDir "python\viewer.conf.example") $stageApp

# The wheel is a zip. Unpacking it beats installing it: no pip inside the embeddable
# runtime, no bootstrap, nothing written outside this directory, and the result is
# inspectable. pyserial is pure Python, so there is nothing to compile or select.
$wheel = Get-ChildItem $DownloadDir -Filter "pyserial-*.whl" | Select-Object -First 1
if (-not $wheel) { throw "no pyserial wheel in $DownloadDir" }
$sitePackages = Join-Path $stagePython "Lib\site-packages"
New-Item -ItemType Directory -Force -Path $sitePackages | Out-Null
$wheelZip = Join-Path $DownloadDir ($wheel.BaseName + ".zip")
Copy-Item $wheel.FullName $wheelZip -Force
Expand-Archive -Path $wheelZip -DestinationPath $sitePackages -Force
Remove-Item $wheelZip

# The embeddable build's path file is the whole of its import configuration, and it does
# not include site-packages -- that is what "embeddable" means. Both additions are needed:
# one for pyserial, one so supervise.py can import main and webdash by name.
$pthName = "python" + ($PythonVersion -replace '^(\d+)\.(\d+).*$', '$1$2') + "._pth"
$pth = Join-Path $stagePython $pthName
if (-not (Test-Path $pth)) { throw "expected $pthName in the embeddable package" }
@(
    ($pthName -replace '\._pth$', '.zip'),
    ".",
    "Lib\site-packages",
    "..\app",
    "",
    "# site stays off. Nothing here needs it, and leaving it off keeps a machine-wide",
    "# Python installation from reaching into this one through PYTHONPATH or a .pth file.",
    "#import site"
) | Set-Content -Path $pth -Encoding ASCII

New-Item -ItemType Directory -Force -Path $stageService | Out-Null
Copy-Item (Join-Path $PackagingDir "service\supervise.py") $stageService
Copy-Item (Join-Path $PackagingDir "service\nicla-capture.xml") $stageService
Copy-Item (Join-Path $PackagingDir "service\dashboard-task.ps1") $stageService
# WinSW takes its configuration from the .xml sharing its base name, so the name of the
# executable is load-bearing rather than cosmetic.
Copy-Item (Join-Path $DownloadDir $WinswExe) (Join-Path $stageService "nicla-capture.exe")

Copy-Item (Join-Path $PackagingDir "nicla.conf") $StageDir
Copy-Item (Join-Path $RepoDir "README.md") $StageDir
Copy-Item (Join-Path $PackagingDir "README.md") (Join-Path $StageDir "PACKAGING.md")

Write-Host "    staged $((Get-ChildItem $StageDir -Recurse -File | Measure-Object).Count) files"

# A staged tree that cannot import its own program is a build failure, and it is much
# cheaper to find here than after an install. This exercises the real _pth, the unpacked
# wheel and the app layout in one go.
Step "smoke"
& (Join-Path $stagePython "python.exe") -c @"
import serial, main, webdash, retention, tiles
print('    imports ok: pyserial %s' % serial.VERSION)
"@
if ($LASTEXITCODE -ne 0) { throw "the staged runtime cannot import the app" }

# ---------------------------------------------------------------- compile

if ($SkipCompile) { Step "skipping compile"; return }

Step "compile"
$iscc = Get-Command "iscc.exe" -ErrorAction SilentlyContinue
if (-not $iscc) {
    foreach ($candidate in @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )) {
        if (Test-Path $candidate) { $iscc = $candidate; break }
    }
} else {
    $iscc = $iscc.Source
}
if (-not $iscc) {
    throw "Inno Setup 6 not found. Install it (winget install JRSoftware.InnoSetup) or run with -SkipCompile."
}

& $iscc "/DAppVersion=$AppVersion" "/DStageDir=$StageDir" "/O$DistDir" (Join-Path $PackagingDir "nicla.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }

Step "done"
Get-ChildItem $DistDir -Filter "*.exe" | ForEach-Object { Write-Host "    $($_.FullName)" }
