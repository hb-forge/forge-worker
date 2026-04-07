#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Forge Worker — One-line installer
#
# Downloads and installs the latest Forge Worker.app from GitHub Releases.
# Run from Terminal on any Mac that will be a forge worker machine.
#
# Usage (one-liner from dashboard):
#   curl -fsSL https://raw.githubusercontent.com/hb-forge/forge-worker/main/install.sh | bash
#
# Or locally:
#   ./install.sh
# ─────────────────────────────────────────────────────────────────────────────
set -e

GITHUB_REPO="hb-forge/forge-worker"
RELEASE_API="https://api.github.com/repos/${GITHUB_REPO}/releases/latest"
APP_NAME="Forge Worker"
INSTALL_DIR="/Applications"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Forge Worker — Installer"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Check macOS ──
if [[ "$(uname)" != "Darwin" ]]; then
  echo "  ❌  This installer is for macOS only."
  exit 1
fi

# ── Check Python 3 ──
if ! command -v python3 &>/dev/null; then
  echo "  ❌  python3 not found."
  echo "  Install it from: https://www.python.org/downloads/"
  exit 1
fi
echo "  ✓  Python: $(python3 --version)"

# ── Check Claude CLI ──
if command -v claude &>/dev/null; then
  echo "  ✓  Claude CLI: $(claude --version 2>&1 | head -1)"
else
  echo "  ⚠  Claude CLI not found — installing…"
  npm install -g @anthropic-ai/claude-code || {
    echo "  ❌  Could not install Claude CLI. Install Node.js first: https://nodejs.org"
    echo "  Then run: npm install -g @anthropic-ai/claude-code"
  }
fi

# ── Check gh CLI ──
if command -v gh &>/dev/null; then
  echo "  ✓  GitHub CLI: $(gh --version | head -1)"
else
  echo "  ⚠  GitHub CLI not found."
  if command -v brew &>/dev/null; then
    echo "  Installing via Homebrew…"
    brew install gh
  else
    echo "  Install from: https://cli.github.com"
  fi
fi

echo ""
echo "  Fetching latest release from GitHub…"

# ── Get download URL ──
if command -v curl &>/dev/null; then
  RELEASE_JSON=$(curl -fsSL "$RELEASE_API" 2>/dev/null || echo "{}")
else
  echo "  ❌  curl not found. Cannot download."
  exit 1
fi

DOWNLOAD_URL=$(echo "$RELEASE_JSON" | python3 -c "
import json, sys
data = json.load(sys.stdin)
assets = data.get('assets', [])
for a in assets:
    if a['name'].endswith('.app.zip'):
        print(a['browser_download_url'])
        break
" 2>/dev/null || echo "")

if [[ -z "$DOWNLOAD_URL" ]]; then
  echo "  ❌  No release found. Build locally:"
  echo "      cd apps/forge/worker-app && ./build.sh"
  echo ""
  echo "  Then copy to Applications:"
  echo "      cp -r 'dist/Forge Worker.app' /Applications/"
  exit 1
fi

VERSION=$(echo "$RELEASE_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin).get('tag_name','?'))" 2>/dev/null || echo "?")
echo "  Found version: $VERSION"
echo "  Downloading…"

# ── Download + install ──
TMP=$(mktemp -d)
curl -fsSL "$DOWNLOAD_URL" -o "$TMP/ForgeWorker.app.zip"
echo "  Extracting…"
unzip -q "$TMP/ForgeWorker.app.zip" -d "$TMP"

if [[ -d "$TMP/Forge Worker.app" ]]; then
  if [[ -d "$INSTALL_DIR/$APP_NAME.app" ]]; then
    echo "  Removing previous version…"
    rm -rf "$INSTALL_DIR/$APP_NAME.app"
  fi
  echo "  Installing to $INSTALL_DIR…"
  cp -r "$TMP/Forge Worker.app" "$INSTALL_DIR/"
  rm -rf "$TMP"

  echo ""
  echo "  ✅  Forge Worker installed!"
  echo ""
  echo "  Launch it now:"
  echo "      open \"/Applications/Forge Worker.app\""
  echo ""
  echo "  It will appear in your menu bar (⚒)."
  echo "  Add your Claude API key and GitHub account via the menu."
  echo ""
else
  echo "  ❌  Extraction failed — .app not found in zip"
  rm -rf "$TMP"
  exit 1
fi

# ── Offer to open ──
read -rp "  Open Forge Worker now? [Y/n] " choice
if [[ "${choice:-y}" =~ ^[Yy]$ ]]; then
  open "/Applications/Forge Worker.app"
fi

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
