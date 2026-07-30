Write-Host "==> Checking Python 3.11+"
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Error "Python is not installed. Please install Python 3.11+ from https://python.org"
    exit 1
}
$ver = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ([version]$ver -lt [version]"3.11") {
    Write-Error "Python 3.11+ required. Current: $ver"
    exit 1
}

Write-Host "==> Installing GitPilot"
python -m pip install gitpilot-ai

Write-Host "==> Installing Windows service (requires Administrator)"
python -m gitpilot.cli.main install-service

Write-Host "==> Running interactive setup"
python -m gitpilot.cli.main setup

Write-Host "GitPilot is ready. Type 'gitpilot' to launch the dashboard."