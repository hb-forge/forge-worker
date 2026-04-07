# Forge Worker

The Hawthorn Bloom Forge Worker — macOS menu bar app.

Monitors active forge sessions, starts/kills workers, switches Claude accounts, and tracks API usage. No terminal required.

## Install (one-liner)

```bash
curl -fsSL https://raw.githubusercontent.com/hb-forge/forge-worker/main/install.sh | bash
```

Or [download the latest .app](https://github.com/hb-forge/forge-worker/releases/latest) and drag to Applications.

## What it does

- **⚒ menu bar icon** — shows active worker count
- **Session monitoring** — polls Supabase for your machine's assignments
- **Kill switch** — stops all workers with one click
- **Claude account switcher** — swap API keys on the fly
- **GitHub identity** — links your GitHub username for commit attribution
- **Installation checks** — verifies Claude CLI, gh, psutil are installed

## Dev setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Build .app

```bash
./build.sh           # → dist/Forge Worker.app
./build.sh --release # → dist/Forge Worker.app.zip (for GitHub Releases)
```

## Release

Tag a version to trigger the GitHub Actions build:

```bash
git tag v1.0.0 && git push origin v1.0.0
```
