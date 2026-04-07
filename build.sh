#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Forge Worker — Build macOS .app bundle
#
# Usage: ./build.sh [--release]
#
# Output:
#   dist/Forge Worker.app         ← the packaged app
#   dist/Forge Worker.app.zip     ← distributable archive (with --release)
# ─────────────────────────────────────────────────────────────────────────────
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

RELEASE=false
[[ "${1:-}" == "--release" ]] && RELEASE=true

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Forge Worker — macOS App Builder"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Python venv ──
VENV="$DIR/.venv"
if [[ ! -f "$VENV/bin/activate" ]]; then
  echo "  Creating Python venv…"
  python3 -m venv "$VENV"
fi
source "$VENV/bin/activate"

echo "  Installing dependencies…"
pip install -q -r requirements.txt py2app

# ── Icon ── (generate placeholder if missing)
if [[ ! -f "icons/forge-worker.icns" ]]; then
  echo "  Generating placeholder icon… (replace icons/forge-worker.icns for release)"
  mkdir -p icons
  # Create a simple 1024x1024 PNG placeholder using sips if ImageMagick not available
  if command -v convert &>/dev/null; then
    convert -size 1024x1024 xc:'#1a1a2e' -fill '#6c63ff' \
      -font "Helvetica-Bold" -pointsize 500 -gravity center \
      -annotate 0 '⚒' icons/forge-worker-1024.png 2>/dev/null || true
  fi
  # Make .icns from PNG if we have one
  if [[ -f "icons/forge-worker-1024.png" ]]; then
    ICONSET="icons/forge-worker.iconset"
    mkdir -p "$ICONSET"
    for size in 16 32 64 128 256 512; do
      sips -z $size $size icons/forge-worker-1024.png --out "$ICONSET/icon_${size}x${size}.png" &>/dev/null
      sips -z $((size*2)) $((size*2)) icons/forge-worker-1024.png --out "$ICONSET/icon_${size}x${size}@2x.png" &>/dev/null
    done
    iconutil -c icns "$ICONSET" -o "icons/forge-worker.icns"
    rm -rf "$ICONSET"
    echo "  ✓ Icon created"
  fi
fi

# ── Build ──
echo "  Building app bundle…"
rm -rf build dist
python setup.py py2app --quiet

APP_PATH="dist/Forge Worker.app"
if [[ -d "$APP_PATH" ]]; then
  echo ""
  echo "  ✅  Built: $APP_PATH"
  echo "  Size: $(du -sh "$APP_PATH" | cut -f1)"

  if [[ "$RELEASE" == "true" ]]; then
    echo "  Creating release archive…"
    cd dist
    zip -r "Forge Worker.app.zip" "Forge Worker.app" -q
    cd ..
    echo "  ✅  Release: dist/Forge Worker.app.zip"
    echo "  Size: $(du -sh "dist/Forge Worker.app.zip" | cut -f1)"
  fi
else
  echo "  ❌  Build failed — check output above"
  exit 1
fi

echo ""
echo "  To run:    open \"$APP_PATH\""
echo "  To install: cp -r \"$APP_PATH\" /Applications/"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
