#!/usr/bin/env sh
set -eu

# Raincurve CLI installer
# Usage: curl -fsSL https://raincurve.com/install.sh | sh

REPO="raincurve/raincurve-cli"
PACKAGE="raincurve"
MIN_PYTHON="3.11"
BOLD="\033[1m"
GREEN="\033[32m"
RED="\033[31m"
YELLOW="\033[33m"
RESET="\033[0m"

info()  { printf "${BOLD}${GREEN}==>${RESET} ${BOLD}%s${RESET}\n" "$*"; }
warn()  { printf "${YELLOW}warning:${RESET} %s\n" "$*"; }
error() { printf "${RED}error:${RESET} %s\n" "$*" >&2; exit 1; }

check_command() {
    command -v "$1" >/dev/null 2>&1
}

version_gte() {
    # returns 0 if $1 >= $2 (dotted version comparison)
    printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1 | grep -qx "$2"
}

detect_python() {
    for cmd in python3 python; do
        if check_command "$cmd"; then
            ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null) || continue
            if version_gte "$ver" "$MIN_PYTHON"; then
                echo "$cmd"
                return 0
            fi
        fi
    done
    return 1
}

main() {
    printf "\n"
    info "Installing Raincurve CLI"
    printf "\n"

    # ── Check Python ───────────────────────────────────────────────
    PYTHON=$(detect_python) || error "Python >= $MIN_PYTHON is required but not found. Install it from https://python.org"
    py_ver=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
    info "Found Python $py_ver ($PYTHON)"

    # ── Check Docker (warn only — not needed for install) ─────────
    if ! check_command docker; then
        warn "Docker not found. Raincurve needs Docker at runtime — install it from https://docs.docker.com/get-docker/"
    fi

    # ── Install via pipx (preferred) or pip ────────────────────────
    if check_command pipx; then
        info "Installing with pipx"
        pipx install "$PACKAGE" --force
    elif "$PYTHON" -m pipx --version >/dev/null 2>&1; then
        info "Installing with python -m pipx"
        "$PYTHON" -m pipx install "$PACKAGE" --force
    else
        info "pipx not found — installing with pip"
        warn "Consider installing pipx (https://pipx.pypa.io) for isolated installs"
        "$PYTHON" -m pip install --user "$PACKAGE"
    fi

    # ── Verify ─────────────────────────────────────────────────────
    printf "\n"
    if check_command raincurve; then
        info "Raincurve CLI installed successfully!"
        printf "\n"
        printf "  Get started:\n"
        printf "    ${BOLD}raincurve init${RESET}       Configure your LLM provider\n"
        printf "    ${BOLD}raincurve sandbox${RESET}    Spin up a local replica\n"
        printf "    ${BOLD}raincurve doctor${RESET}     Check system readiness\n"
        printf "\n"
    else
        warn "Installation succeeded but 'raincurve' is not on PATH."
        printf "\n"
        printf "  If you installed with pip --user, add this to your shell profile:\n"
        printf "    export PATH=\"\$HOME/.local/bin:\$PATH\"\n"
        printf "\n"
        printf "  Then restart your shell and run: ${BOLD}raincurve --help${RESET}\n"
        printf "\n"
    fi
}

main
