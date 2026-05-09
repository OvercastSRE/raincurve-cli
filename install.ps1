# Raincurve CLI installer for Windows
# Usage: irm https://raincurve.com/install.ps1 | iex

$ErrorActionPreference = "Stop"
$MinPython = [version]"3.11"

function Write-Info($msg)  { Write-Host "==> $msg" -ForegroundColor Green }
function Write-Warn($msg)  { Write-Host "warning: $msg" -ForegroundColor Yellow }
function Write-Err($msg)   { Write-Host "error: $msg" -ForegroundColor Red; exit 1 }

function Find-Python {
    foreach ($cmd in @("python3", "python", "py")) {
        try {
            $ver = & $cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $ver) {
                $parsed = [version]$ver
                if ($parsed -ge $MinPython) {
                    return @{ Cmd = $cmd; Version = $ver }
                }
            }
        } catch {}
    }
    return $null
}

Write-Host ""
Write-Info "Installing Raincurve CLI"
Write-Host ""

# ── Check Python ──────────────────────────────────────────────────
$py = Find-Python
if (-not $py) {
    Write-Err "Python >= $MinPython is required but not found. Install it from https://python.org"
}
Write-Info "Found Python $($py.Version) ($($py.Cmd))"

# ── Check Docker (warn only) ─────────────────────────────────────
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Warn "Docker not found. Raincurve needs Docker at runtime — install from https://docs.docker.com/get-docker/"
}

# ── Install via pipx (preferred) or pip ───────────────────────────
$hasPipx = $false
try { pipx --version 2>$null; if ($LASTEXITCODE -eq 0) { $hasPipx = $true } } catch {}

if (-not $hasPipx) {
    try { & $py.Cmd -m pipx --version 2>$null; if ($LASTEXITCODE -eq 0) { $hasPipx = $true } } catch {}
}

if ($hasPipx) {
    Write-Info "Installing with pipx"
    if (Get-Command pipx -ErrorAction SilentlyContinue) {
        pipx install raincurve --force
    } else {
        & $py.Cmd -m pipx install raincurve --force
    }
} else {
    Write-Info "pipx not found — installing with pip"
    Write-Warn "Consider installing pipx (https://pipx.pypa.io) for isolated installs"
    & $py.Cmd -m pip install --user raincurve
}

# ── Verify ────────────────────────────────────────────────────────
Write-Host ""
if (Get-Command raincurve -ErrorAction SilentlyContinue) {
    Write-Info "Raincurve CLI installed successfully!"
    Write-Host ""
    Write-Host "  Get started:"
    Write-Host "    raincurve init       Configure your LLM provider"
    Write-Host "    raincurve sandbox    Spin up a local replica"
    Write-Host "    raincurve doctor     Check system readiness"
    Write-Host ""
} else {
    Write-Warn "Installation succeeded but 'raincurve' is not on PATH."
    Write-Host ""
    Write-Host "  You may need to restart your terminal, or add Python Scripts to PATH."
    Write-Host "  Then run: raincurve --help"
    Write-Host ""
}
