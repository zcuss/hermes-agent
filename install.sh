#!/bin/bash
# ============================================================================
# Hermes Agent Installer (zcuss fork)
# ============================================================================
# One-liner installer for the zcuss fork. This is a thin shim: it picks an
# install directory (FHS layout for root, user dir otherwise), ensures the
# repo is cloned/updated, then hands off to ./setup-hermes.sh which handles
# uv, Python venv, dependency install, CLI symlink, and the setup wizard.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/zcuss/hermes-agent/main/install.sh | bash
#
# Or with options:
#   curl -fsSL ... | bash -s -- --skip-setup --branch feat/foo
#
# All heavy lifting (uv, venv, deps, wizard) lives in setup-hermes.sh. This
# shim's only job is "where does the code live, and is it on disk yet?".
# ============================================================================

set -e

# Same guard as upstream: a pre-set PYTHONPATH/PYTHONHOME from the calling
# shell can force the installer's pip/entrypoints to import a different
# checkout than the one being installed.
if [ -n "${PYTHONPATH:-}" ]; then
    echo "⚠ Ignoring inherited PYTHONPATH during install to avoid module shadowing"
    unset PYTHONPATH
fi
if [ -n "${PYTHONHOME:-}" ]; then
    echo "⚠ Ignoring inherited PYTHONHOME during install"
    unset PYTHONHOME
fi

export UV_NO_CONFIG=1

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
BOLD='\033[1m'
NC='\033[0m'

# Fork identity — keep these at the top so a future re-fork only edits one block.
REPO_URL_SSH="git@github.com:zcuss/hermes-agent.git"
REPO_URL_HTTPS="https://github.com/zcuss/hermes-agent.git"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

# INSTALL_DIR is resolved AFTER arg parsing so --dir wins over the FHS default.
if [ -n "${HERMES_INSTALL_DIR:-}" ]; then
    INSTALL_DIR="$HERMES_INSTALL_DIR"
    INSTALL_DIR_EXPLICIT=true
else
    INSTALL_DIR=""
    INSTALL_DIR_EXPLICIT=false
fi

# Options
BRANCH="main"
INSTALL_COMMIT=""

print_banner() {
    echo ""
    echo -e "${MAGENTA}${BOLD}"
    echo "┌─────────────────────────────────────────────────────────┐"
    echo "│       ⚕ Hermes Agent Installer (zcuss fork)            │"
    echo "├─────────────────────────────────────────────────────────┤"
    echo "│  An open source AI agent by Nous Research.              │"
    echo "│  This fork adds CockroachDB-backed durable state.       │"
    echo "└─────────────────────────────────────────────────────────┘"
    echo -e "${NC}"
}

log_info()    { echo -e "${CYAN}→${NC} $1"; }
log_success() { echo -e "${GREEN}✓${NC} $1"; }
log_warn()    { echo -e "${YELLOW}⚠${NC} $1"; }
log_error()   { echo -e "${RED}✗${NC} $1"; }

is_termux() {
    [ -n "${TERMUX_VERSION:-}" ] || [[ "${PREFIX:-}" == *"com.termux/files/usr"* ]]
}

is_root() {
    [ "$(id -u)" -eq 0 ]
}

resolve_install_layout() {
    # FHS layout for root on Linux: code under /usr/local/lib, command on PATH
    # for all shells. Matches upstream's Claude Code / Codex CLI convention
    # and keeps Docker bind-mounted /root/ volumes lean. For non-root or
    # Termux, code lives inside HERMES_HOME so the user owns the tree.
    if [ "$INSTALL_DIR_EXPLICIT" = true ]; then
        return
    fi
    if is_termux; then
        INSTALL_DIR="$HERMES_HOME/hermes-agent"
    elif is_root; then
        INSTALL_DIR="/usr/local/lib/hermes-agent"
    else
        INSTALL_DIR="$HERMES_HOME/hermes-agent"
    fi
}

ensure_prereqs() {
    local missing=()
    for cmd in git curl; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            missing+=("$cmd")
        fi
    done
    if [ ${#missing[@]} -ne 0 ]; then
        log_error "Missing required commands: ${missing[*]}"
        log_info "Install them with your package manager, e.g.:"
        log_info "  Debian/Ubuntu: apt-get install -y ${missing[*]}"
        log_info "  RHEL/Alma:     dnf install -y ${missing[*]}"
        log_info "  Alpine:        apk add ${missing[*]}"
        log_info "  macOS:         brew install ${missing[*]}"
        exit 1
    fi
}

clone_or_update_repo() {
    if [ -d "$INSTALL_DIR/.git" ]; then
        log_info "Existing checkout at $INSTALL_DIR — updating"
        if ! git -C "$INSTALL_DIR" fetch --quiet origin "$BRANCH" 2>/dev/null; then
            log_warn "Fetch failed; continuing with current checkout"
            return
        fi
        # Fast-forward only; refuse if local history diverged (user has
        # unpushed work). setup-hermes.sh won't help with that.
        if ! git -C "$INSTALL_DIR" merge --ff-only "origin/$BRANCH" 2>/dev/null; then
            log_warn "Local checkout has diverged from origin/$BRANCH — leaving as-is"
            log_warn "Resolve manually, then re-run install.sh"
        fi
    elif [ -d "$INSTALL_DIR" ]; then
        log_error "$INSTALL_DIR exists but is not a git checkout"
        log_info "Move it aside (mv $INSTALL_DIR ${INSTALL_DIR}.bak) and re-run"
        exit 1
    else
        log_info "Cloning $REPO_URL_HTTPS ($BRANCH) -> $INSTALL_DIR"
        mkdir -p "$(dirname "$INSTALL_DIR")"
        if ! git clone --branch "$BRANCH" --depth 1 "$REPO_URL_HTTPS" "$INSTALL_DIR"; then
            # Fall back to SSH for users with GitHub auth but no token.
            log_warn "HTTPS clone failed; trying SSH"
            git clone --branch "$BRANCH" --depth 1 "$REPO_URL_SSH" "$INSTALL_DIR"
        fi
    fi

    if [ -n "$INSTALL_COMMIT" ]; then
        log_info "Pinning to commit $INSTALL_COMMIT"
        git -C "$INSTALL_DIR" checkout --quiet "$INSTALL_COMMIT"
    fi
}

# ---- arg parsing -------------------------------------------------------------

while [ $# -gt 0 ]; do
    case "$1" in
        --branch|-Branch)
            BRANCH="$2"
            shift 2
            ;;
        --commit|-Commit)
            INSTALL_COMMIT="$2"
            shift 2
            ;;
        --dir)
            INSTALL_DIR="$2"
            INSTALL_DIR_EXPLICIT=true
            shift 2
            ;;
        --hermes-home)
            HERMES_HOME="$2"
            shift 2
            ;;
        -h|--help)
            echo "Hermes Agent Installer (zcuss fork)"
            echo ""
            echo "Usage: install.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --branch NAME           Git branch to install (default: main)"
            echo "  --commit SHA            Pin checkout to a specific commit"
            echo "  --dir PATH              Install directory"
            echo "                            default (non-root):  ~/.hermes/hermes-agent"
            echo "                            default (root):      /usr/local/lib/hermes-agent"
            echo "  --hermes-home PATH      Data directory (default: ~/.hermes)"
            echo "  -h, --help              Show this help"
            echo ""
            echo "Non-interactive bootstrap:"
            echo "  echo 'n' | curl -fsSL ... | bash    # answer 'no' to the wizard"
            echo ""
            echo "After install:"
            echo "  1. Set up CockroachDB:  hermes db init  (see README §Database setup)"
            echo "  2. Start the gateway:   hermes gateway run"
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            echo "Run with --help for usage."
            exit 1
            ;;
    esac
done

# ---- main --------------------------------------------------------------------

print_banner
resolve_install_layout
ensure_prereqs
clone_or_update_repo

log_info "Handing off to setup-hermes.sh"
exec "$INSTALL_DIR/setup-hermes.sh"

# setup-hermes.sh sets up the venv, installs deps, symlinks the hermes
# CLI into ~/.local/bin (or $PREFIX/bin on Termux), and prompts for the
# setup wizard. When invoked as root, it also installs into /usr/local/bin.
