# Contributing to The Stone of Osgiliath

Thanks for your interest in contributing! This project is a personal price monitoring tool that's grown into something others might find useful.

## Getting Started

1. Fork the repo and clone it locally
2. Follow the installation steps in [README.md](README.md)
3. Copy `config.example.json` to `config.json` and add your Discord token
4. Run `python main.py` to verify everything works

## Project Structure

- **Single-file frontend** — `web/index.html` contains all HTML, CSS, and JavaScript (~6000 lines). This is intentional for simplicity.
- **FastAPI backend** — `web/app.py` handles all API routes, WebSocket, and the monitor loop
- **Shared state** — `web/state.py` contains data models and persistence
- **Monitors** — `monitors/` contains scrapers for each data source
- **Database** — `db.py` manages SQLite schema and queries

## Development

```bash
# Run with auto-reload for backend changes
uvicorn web.app:app --reload --port 8888

# Or use the launcher
python main.py --no-browser
```

For frontend changes, just edit `web/index.html` and refresh the browser. No build step needed.

## Code Style

- Python: Standard library conventions, type hints where helpful
- JavaScript: Vanilla JS, no framework, no build tools
- CSS: CSS custom properties for theming, embedded in the HTML
- Keep it simple — this is a personal tool, not an enterprise app

## What to Contribute

**Good first contributions:**
- Bug fixes
- New game detection patterns (add to `_GAME_PATTERNS` in index.html)
- New retailer detection (add to `_discordExtractRetailer` in index.html and `_ingest_retailer_sighting` in app.py)
- UI polish and responsive design improvements
- Documentation improvements

**Larger contributions:**
- New monitor integrations (new data sources)
- Portfolio features (P&L calculations, market value tracking)
- Mobile-friendly layout
- Export functionality (CSV, PDF)
- Price alerts and notification rules

## Pull Request Guidelines

1. Keep PRs focused — one feature or fix per PR
2. Test that the app starts and basic functionality works
3. Don't commit `config.json`, database files, or cached images
4. If adding a new config option, update `config.example.json` too

## Security

- **Never commit tokens or credentials** — all secrets go in `config.json` which is git-ignored
- If you find a security issue, please report it privately rather than opening a public issue
- Discord user tokens are sensitive — treat them like passwords

## Legacy Code

The `monitors/` directory contains legacy monitor files for Walmart, Amazon, Target, and Best Buy. These are no longer actively used (the app now relies on Discord feeds for deal detection) but are kept as reference for potential future direct-monitoring features.

## Questions?

Open an issue for bugs, feature requests, or questions about the codebase.
