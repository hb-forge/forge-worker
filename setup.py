"""
py2app build configuration for Forge Worker menu bar app.

Usage:
  python setup.py py2app

Output:
  dist/Forge Worker.app

To create a distributable zip:
  cd dist && zip -r "Forge Worker.app.zip" "Forge Worker.app"
"""
from setuptools import setup

APP         = ["app.py"]
APP_NAME    = "Forge Worker"
BUNDLE_ID   = "com.hawthornbloom.forge-worker"
VERSION     = "1.0.0"
ICON        = "icons/forge-worker.icns"  # provide your icon here

OPTIONS = {
    "app_name": APP_NAME,
    "iconfile": ICON if __import__("pathlib").Path(ICON).exists() else None,
    "plist": {
        "CFBundleName":                APP_NAME,
        "CFBundleDisplayName":         APP_NAME,
        "CFBundleIdentifier":          BUNDLE_ID,
        "CFBundleVersion":             VERSION,
        "CFBundleShortVersionString":  VERSION,
        "LSUIElement":                 True,      # No Dock icon (menu bar only)
        "NSHighResolutionCapable":     True,
        "LSMinimumSystemVersion":      "12.0",
    },
    "packages": ["rumps", "supabase", "psutil"],
    "includes": ["dotenv"],
    "argv_emulation": False,
    "semi_standalone": False,
}

setup(
    name=APP_NAME,
    version=VERSION,
    app=APP,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
