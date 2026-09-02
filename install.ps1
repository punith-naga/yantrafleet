# YantraFleet one-shot installer for Windows (PowerShell 5.1+).
#
#   powershell -ExecutionPolicy Bypass -File install.ps1        # install only
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Run   # ... then start the demo
#
# Idempotent: safe to re-run; an existing .venv is reused and installs are
# refreshed in place. No activation needed — the venv's python.exe is
# called directly throughout.

param([switch]$Run)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = Join-Path $RepoRoot ".venv"
if ($env:INSTALL_VENV_DIR) { $VenvDir = $env:INSTALL_VENV_DIR }
$VenvPy = Join-Path $VenvDir "Scripts\python.exe"

function Say([string]$msg) { Write-Host "`n== $msg" }
function Fail([string]$msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

# Returns $true if the given interpreter reports Python >= 3.10.
function Test-PyVersion([string]$exe, [string[]]$extraArgs) {
    try {
        $py_args = @()
        if ($extraArgs) { $py_args += $extraArgs }
        $py_args += @("-c", "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)")
        & $exe @py_args 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

# ---------------------------------------------------------------------------
# 1) Find a Python >= 3.10
# ---------------------------------------------------------------------------
$PyExe = $null       # interpreter executable
$PyArgs = @()        # extra launcher args (e.g. -3.12 for py.exe)

# 1a. Ask the py launcher for its installed interpreter paths (py -0p) and
#     probe each listed python.exe directly.
$pyLauncher = Get-Command "py" -ErrorAction SilentlyContinue
if ($pyLauncher) {
    # try/catch: in PS 5.1 with ErrorActionPreference=Stop, a native command
    # writing to a redirected stderr can raise NativeCommandError.
    $listing = $null
    try { $listing = & py -0p 2>$null } catch { $listing = $null }
    if ($listing) {
        foreach ($line in $listing) {
            # Lines look like: " -V:3.12 *   C:\Python312\python.exe"
            if ("$line" -match '([A-Za-z]:\\[^*]*python\.exe)\s*$') {
                $candidate = $Matches[1].Trim()
                if ((Test-Path $candidate) -and (Test-PyVersion $candidate @())) {
                    $PyExe = $candidate
                    break
                }
            }
        }
    }
    # 1b. Fall back to asking the launcher for specific versions.
    if (-not $PyExe) {
        foreach ($ver in @("-3.13", "-3.12", "-3.11", "-3.10", "-3")) {
            if (Test-PyVersion "py" @($ver)) {
                $PyExe = "py"
                $PyArgs = @($ver)
                break
            }
        }
    }
}

# 1c. Last resort: plain 'python' on PATH.
if (-not $PyExe) {
    $plain = Get-Command "python" -ErrorAction SilentlyContinue
    if ($plain -and (Test-PyVersion $plain.Source @())) {
        $PyExe = $plain.Source
    }
}

if (-not $PyExe) {
    Fail ("no Python 3.10+ found. YantraFleet needs Python 3.10 or newer.`n" +
          "Download it from https://www.python.org/downloads/windows/ " +
          "(check 'Add python.exe to PATH' in the installer), then re-run:`n" +
          "  powershell -ExecutionPolicy Bypass -File install.ps1")
}
$verArgs = @(); if ($PyArgs) { $verArgs += $PyArgs }; $verArgs += "--version"
$verText = (& $PyExe @verArgs 2>&1) -join " "
$launcher = ("$PyExe $($PyArgs -join ' ')").Trim()
Say "using $verText via '$launcher'"

# ---------------------------------------------------------------------------
# 2) Create (or reuse) the virtual environment
# ---------------------------------------------------------------------------
if (Test-Path $VenvPy) {
    Say "reusing existing venv at $VenvDir"
} else {
    Say "creating venv at $VenvDir"
    $venvArgs = @(); if ($PyArgs) { $venvArgs += $PyArgs }
    $venvArgs += @("-m", "venv", $VenvDir)
    & $PyExe @venvArgs
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPy)) {
        Fail "could not create a venv at $VenvDir"
    }
}

# ---------------------------------------------------------------------------
# 3) Install the packages (editable) + copilot requirements
# ---------------------------------------------------------------------------
Say "upgrading pip"
& $VenvPy -m pip install --quiet --upgrade pip
if ($LASTEXITCODE -ne 0) { Fail "pip upgrade failed" }

Say "installing YantraFleet packages (editable)"
& $VenvPy -m pip install --quiet `
    -e (Join-Path $RepoRoot "core") `
    -e (Join-Path $RepoRoot "sim") `
    -e (Join-Path $RepoRoot "connector") `
    -e (Join-Path $RepoRoot "detector") `
    -e (Join-Path $RepoRoot "notifier") `
    -e (Join-Path $RepoRoot "ops")
if ($LASTEXITCODE -ne 0) { Fail "editable package install failed" }

Say "installing copilot (sarathi) requirements"
& $VenvPy -m pip install --quiet -r (Join-Path $RepoRoot "copilot\requirements.txt")
if ($LASTEXITCODE -ne 0) { Fail "copilot requirements install failed" }

# ---------------------------------------------------------------------------
# 4) Sanity check
# ---------------------------------------------------------------------------
Say "verifying: python -m yantraops --help"
& $VenvPy -m yantraops --help | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "yantraops did not import cleanly" }

# Optional deeper check; this subcommand may not exist in every version —
# ignore any failure (including PS 5.1's NativeCommandError on stderr output).
try { & $VenvPy -m yantraops doctor 2>$null | Out-Null } catch { }

# ---------------------------------------------------------------------------
# 5) Done
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "============================================================"
Write-Host " YantraFleet installed."
Write-Host ""
Write-Host " Start the full loopback demo (no cloud, no keys) with:"
Write-Host ""
Write-Host "   .venv\Scripts\python.exe -m yantraops up --loopback" -ForegroundColor Green
Write-Host ""
Write-Host " It prints the console URL and opens your browser. Ctrl-C"
Write-Host " stops everything. Re-run install.ps1 -Run to start it"
Write-Host " immediately after installing."
Write-Host "============================================================"

if ($Run) {
    Say "starting the loopback demo (-Run)"
    & $VenvPy -m yantraops up --loopback
    exit $LASTEXITCODE
}
