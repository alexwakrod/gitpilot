#!/usr/bin/env bash
set -euo pipefail

echo "==> Checking Python 3.11+"
PYTHON=$(command -v python3.11 || command -v python3 || command -v python)
if [ -z "$PYTHON" ]; then
    echo "Python 3.11 or higher is required. Install it and rerun."
    exit 1
fi
$PYTHON -c "import sys; assert sys.version_info >= (3,11)" || {
    echo "Python 3.11+ not found. Please upgrade."
    exit 1
}

echo "==> Installing GitPilot"
$PYTHON -m pip install --user gitpilot

echo "==> Installing background service"
$PYTHON -m gitpilot.cli.main install-service

echo "==> Running initial setup"
$PYTHON -m gitpilot.cli.main setup

echo "GitPilot is ready. Type 'gitpilot' to launch the dashboard."