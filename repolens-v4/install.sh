#!/usr/bin/env bash
# =============================================================================
#   REPOLENS  ·  v4.0 (GUI Edition)
#   One-command installer for Linux
#
#   Install:      sudo bash install.sh
#   Uninstall:    sudo bash install.sh --uninstall
#   Other Debian: sudo bash install.sh --force
# =============================================================================
set -u

# re-exec under bash when launched with sh/dash
if [ -z "${BASH_VERSION:-}" ]; then
    exec bash "$0" "$@"
fi

APP="repolens"
APP_TITLE="RepoLens"
VERSION="4.0"
OPT_DIR="/opt/repolens"
BIN_RL="/usr/local/bin/repolens"
BIN_FULL="/usr/local/bin/rl"
DESKTOP_FILE="/usr/share/applications/repolens.desktop"
ICON_FILE="/usr/share/pixmaps/repolens.png"

# ------------------------------------------------------------------ output --
if [ -t 1 ]; then
    ICE="\033[38;5;159m"; CYAN="\033[38;5;51m"; BLUE="\033[38;5;39m"
    GREEN="\033[38;5;114m"; RED="\033[38;5;203m"; YELL="\033[38;5;221m"
    GREY="\033[38;5;245m"; WHITE="\033[97m"; B="\033[1m"; R="\033[0m"
else
    ICE=""; CYAN=""; BLUE=""; GREEN=""; RED=""; YELL=""; GREY=""; WHITE=""; B=""; R=""
fi

ok()   { printf "  ${GREEN}[+]${R} %s\n" "$1"; }
warn() { printf "  ${YELL}[!]${R} %s\n" "$1"; }
die()  { printf "  ${RED}[x]${R} %s\n" "$1" >&2; exit 1; }
step() { printf "\n${B}${ICE} ==> %s${R}\n" "$1"; }

banner() {
    printf "${B}${CYAN}"
    printf "   █▀▀ █▀█ █▄░█ █▀▀ █▀▀ █▀█   █▀▀ █▀█ █ █  \n"
    printf "   █▄▄ █▄█ █░▀█ █▄▄ ██▄ █▀▄   █▀▀ █▄█ ▄▀▄  \n"
    printf "${GREY}  Account & Repository Inspector  ·  v%s  ·  Linux installer${R}\n" "$VERSION"
    printf "\n"
}

# ------------------------------------------------- environment sanity check --
if [ -n "${PREFIX:-}" ] && case "${PREFIX}" in *com.termux*) true ;; *) false ;; esac; then
    die "Termux / NetHunter-rootless detected — no desktop GUI here.\n        Use the terminal edition instead:   python3 repolens-cli.py"
fi

# ------------------------------------------------------------------ args ---
DO_UNINSTALL=0; FORCE=0
for a in "$@"; do
    case "$a" in
        --uninstall) DO_UNINSTALL=1 ;;
        --force)     FORCE=1 ;;
        -h|--help)   sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) warn "ignored option: $a" ;;
    esac
done

clear 2>/dev/null || true
banner

# --------------------------------------------------------------- uninstall --
if [ "$DO_UNINSTALL" -eq 1 ]; then
    step "Uninstalling $APP_TITLE"
    [ "$(id -u)" -eq 0 ] || die "Run with sudo: sudo bash install.sh --uninstall"
    rm -f "$BIN_RL" "$BIN_FULL"                  && ok "removed repolens / rl commands"
    rm -f "$DESKTOP_FILE"                        && ok "removed menu launcher"
    rm -f "$ICON_FILE"                           && ok "removed icon"
    rm -rf "$OPT_DIR"                            && ok "removed $OPT_DIR"
    printf "\n  ${GREY}Config (~/.config/repolens) and reports (~/repolens-reports) were kept.${R}\n"
    printf "  ${GREY}Bye. Happy searching.${R}\n\n"
    exit 0
fi

# -------------------------------------------------------------- root / kali --
step "Checking privileges"
if [ "$(id -u)" -eq 0 ]; then
    ok "running as root"
elif command -v sudo >/dev/null 2>&1; then
    ok "using sudo"
    SUDO="sudo"
else
    die "root privileges required (try: sudo bash install.sh)"
fi
SUDO="${SUDO:-}"

step "Checking operating system"
LINUX=0
if [ -r /etc/os-release ]; then
    . /etc/os-release
    case "${ID:-}${ID_LIKE:-}" in
        *kali*|*debian*|*ubuntu*|*arch*|*fedora*|*rhel*|*suse*|*linux*) LINUX=1 ;;
    esac
fi
if [ "$LINUX" -eq 1 ]; then
    ok "Linux detected  (${PRETTY_NAME:-})"
else
    if [ "$FORCE" -eq 1 ]; then
        warn "unrecognised OS (${PRETTY_NAME:-unknown}) — continuing (--force)"
    else
        die "this installer is built for Linux only (re-run with --force to override)"
    fi
fi

# ---------------------------------------------------------------- packages --
step "Installing required packages (python3, python3-tk, git, xdg-utils)"
export DEBIAN_FRONTEND=noninteractive
if ! command -v python3 >/dev/null 2>&1 || ! python3 -c "import tkinter" 2>/dev/null; then
    if ! $SUDO apt-get update -y; then
        die "apt-get update failed.\n        Check internet. Linux mirror busy? Try again or run: sudo apt update\n        Behind a proxy/VPN? Disable it and retry."
    fi
    if ! $SUDO apt-get install -y python3 python3-tk git xdg-utils; then
        die "apt-get install failed.\n        Try manually:  sudo apt install -y python3 python3-tk git xdg-utils"
    fi
    ok "packages installed"
else
    ok "python3 + tkinter already present"
fi
command -v git >/dev/null 2>&1      || $SUDO apt-get install -y git        >/dev/null 2>&1 || true
command -v xdg-open >/dev/null 2>&1 || $SUDO apt-get install -y xdg-utils   >/dev/null 2>&1 || true
ok "git: $(git --version 2>/dev/null | awk '{print $3}' || echo 'missing')"

# ------------------------------------------------------------------- files --
step "Installing $APP_TITLE to $OPT_DIR"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
for f in repolens_core.py repolens_gui.py repolens-cli.py; do
    [ -f "$SRC/$f" ] || die "missing file: $SRC/$f (run this script from the project folder)"
done
$SUDO mkdir -p "$OPT_DIR/assets"
$SUDO install -m 644 "$SRC/repolens_core.py" "$OPT_DIR/"
$SUDO install -m 644 "$SRC/repolens_gui.py"  "$OPT_DIR/"
$SUDO install -m 755 "$SRC/repolens-cli.py"  "$OPT_DIR/"
# Install every runtime image asset. The PNG is required for the fast startup splash.
for asset in repolens-icon.png repolens-logo.png repolens-splash.png; do
    if [ -f "$SRC/assets/$asset" ]; then
        $SUDO install -m 644 "$SRC/assets/$asset" "$OPT_DIR/assets/"
    elif [ "$asset" = "repolens-splash.png" ]; then
        warn "missing $asset — startup splash artwork will not be available"
    fi
done
ok "application files and available assets installed"

# ----------------------------------------------------------------- commands --
step "Creating the 'repolens' command"
$SUDO tee "$BIN_RL" >/dev/null <<'EOF'
#!/bin/sh
# RepoLens v4.0 — launcher (GUI with automatic checks)
if ! command -v python3 >/dev/null 2>&1; then
    echo "[x] python3 missing — fix: sudo apt install -y python3 python3-tk"
    exit 1
fi
if [ "${1:-}" = "--cli" ]; then
    shift
    exec python3 /opt/repolens/repolens-cli.py "$@"
fi
if [ "${1:-}" != "--cli" ]; then
    if ! python3 -c "import tkinter" 2>/dev/null; then
        echo "[x] tkinter missing — GUI cannot start."
        echo "    Fix: sudo apt install -y python3-tk"
        echo "    Terminal mode instead:  repolens --cli"
        exit 1
    fi
    if [ -z "${DISPLAY:-}" ]; then
        echo "[x] no display detected (SSH / headless?)."
        echo "    Run on the Linux desktop, or use terminal mode:  repolens --cli"
        exit 1
    fi
fi
exec python3 /opt/repolens/repolens_gui.py "$@"
EOF
$SUDO chmod 755 "$BIN_RL"
$SUDO ln -sf "$BIN_RL" "$BIN_FULL"
ok "created: repolens   (same as: rl)"

# ------------------------------------------------------------------ desktop --
step "Creating Applications menu entry"
$SUDO tee "$DESKTOP_FILE" >/dev/null <<'EOF'
[Desktop Entry]
Type=Application
Version=4.0
Name=RepoLens
GenericName=GitHub Inspector
Comment=GitHub Account & Repository Inspector — v4.0
Exec=/usr/local/bin/repolens
Icon=repolens
Terminal=false
Categories=Network;Utility;
Keywords=github;osint;recon;search;repository;
StartupNotify=true
EOF
if [ -f "$SRC/assets/repolens-icon.png" ]; then
    $SUDO install -m 644 "$SRC/assets/repolens-icon.png" "$ICON_FILE"
fi
ok "menu: Applications → Internet → RepoLens"

# ------------------------------------------------------------------ verify --
step "Verifying installation"
if python3 -c "import tkinter" 2>/dev/null; then
    ok "tkinter OK"
else
    die "tkinter still missing — run: sudo apt install python3-tk"
fi
[ -x "$BIN_RL" ] && ok "command ready: repolens"

cat <<EOF

  ${B}${ICE}─────────────────────────────────────────────────────${R}
   ${B}${WHITE}$APP_TITLE $VERSION installed!${R}

     ${CYAN}repolens${R}        start the GUI
     ${CYAN}repolens octocat${R}      search a username directly
     ${CYAN}repolens --cli${R}        old-school terminal mode
     ${CYAN}repolens --help${R}       help
     ${CYAN}rl${R}              short alias for repolens
  ${B}${ICE}─────────────────────────────────────────────────────${R}

  ${GREY}Tips: add a GitHub token in Settings (higher rate limits).${R}
  ${GREY}Reports are saved to ~/repolens-reports${R}

EOF
printf "  ${B}${GREEN}Run it now:  repolens   (or just:  rl)${R}\n\n"
