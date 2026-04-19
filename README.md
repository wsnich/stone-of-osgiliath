# The Stone of Osgiliath

*That Which Looks Far Away*

A real-time price monitoring, deal tracking, and collection management tool for trading card games and collectibles. Monitors Discord deal channels, tracks TCGPlayer and eBay prices, aggregates deals across retailers, and sends you push notifications when something interesting hits.

Built for Magic: The Gathering, Pokemon, Yu-Gi-Oh, Riftbound, Lorcana, and more.

## Features

### Discord Deal Feed
- Monitors multiple Discord channels for deal alerts in real-time
- **Game detection** — auto-identifies MTG, Pokemon, Riftbound, Yu-Gi-Oh, Star Wars, Lorcana, FAB, One Piece, Digimon, Dragon Ball
- **Retailer extraction** — parses Amazon, Walmart, Target, Best Buy, TCGPlayer, GameStop from embed data and URLs
- **Deal scoring** — compares Discord deal prices against your TCGPlayer market data ("27% below market")
- **Checkout link extraction** — captures ATC/add-to-cart links from Moonitor, Zephr, Refract and surfaces them on deals
- **Multi-game filter** — checkbox dropdown to show any combination of games
- **Channel management** — add/remove monitored channels from the UI with auto-resolved channel names
- **Keyword management** — manual + auto-generated keywords, ignore patterns, min price filter
- **Audit log** — search all messages (shown and filtered) with reason tracking
- **DM notifications** — get push notifications on your phone via Discord bot DM when deals are found

### TCGPlayer Price Tracking
- Monitors market prices, lowest listings, and seller quantities for singles and sealed product
- **Full listing data** — captures every seller's price, shipping cost, and quantity via API pagination
- **Price includes shipping** — all prices reflect true cost (price + shipping)
- **Empty box filter** — automatically removes listings for empty boxes/packaging only
- **Price distribution histograms** with IQR-based outlier pruning
- **Historical snapshots** — stores full listing distributions over time with playback slider
- **TCGPlayer sales data** — captures daily price history buckets (90 days) and recent individual sales
- **eBay sold comps** — searches eBay completed listings for market comparison
- **Trend indicators** — price trend arrows (vs 7-day average) and supply trend indicators on cards
- **Card and Table views** — toggle between full cards and compact table with drag-and-drop reordering

### Comics Tracking
- Monitors eBay/130point.com sold listings with grade breakdowns (CGC, CBCS, PSA, Raw)
- Sales review modal with grade classification, ignore/learn system
- Facsimile edition auto-filtering
- Card and Table views with drag-and-drop reordering

### Product Hub
- Centralized product registry linking TCGPlayer data, retailer URLs, and Discord deals
- Auto-created when adding TCGPlayer items
- Compact card layout with image, price data, retailer links, and deal chips
- Assign/unassign deals, link TCGPlayer items, add retailer URLs

### Deal Aggregation
- Auto-groups Discord messages about the same product using Jaccard similarity matching
- Cards and Table views with deal scoring
- Tag, merge, dismiss, or assign deals to Products
- Checkout links surfaced on deal cards

### Retailer Intelligence (Overview Tab)
- Automatically built from Discord feed data
- Retailer universe — all known retailers carrying TCG products with sighting counts, price ranges, games
- Product x Retailer matrix — which retailers carry each product with price comparison
- Game filter — drill down by specific game
- Continuously learns from new Discord messages

### Reddit Feed
- Live feed from r/sealedmtgdeals
- Collapsible with preview posts

### Portfolio
- Track collection purchases with cost basis
- Market values from linked TCGPlayer data

### Settings
- All configuration editable from the UI (gear icon)
- Monitor interval, browser settings, timeouts, user agent
- Discord token management, bot token for DM notifications
- Data directory for custom file locations

## Requirements

- **Python 3.10+** (uses `dict | None` type hint syntax)
- **Chrome or Chromium browser** (for TCGPlayer and eBay scraping via Patchright/Playwright)
- **Discord user token** (for monitoring deal channels)
- **Discord bot token** (optional, for DM push notifications)

## Installation

```bash
# Clone the repository
git clone <repo-url>
cd mtg-monitor

# Install Python dependencies
pip install -r requirements.txt

# Install browser for Patchright (headless Chromium)
python -m patchright install chromium
```

If `patchright` browser install fails, you can use Playwright as a fallback:
```bash
pip install playwright
python -m playwright install chromium
```

## Configuration

1. Copy the example config:
   ```bash
   cp config.example.json config.json
   ```

2. Edit `config.json` with your settings (see Config Reference below)

### Discord Setup

**User Token** (required for monitoring channels):
1. Open Discord in your **web browser** (not the desktop app)
2. Press F12 > Network tab
3. Filter for `science` or `messages`
4. Click any request > Headers > find `Authorization:` value
5. Copy that token into `config.json` under `discord.token`

**Bot Token** (optional, for DM notifications):
1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Create a New Application > Bot > Reset Token
3. Copy the token into Settings > Discord > Bot Token
4. Invite the bot to a server you share with your main account
5. Enter your User ID in Settings > Discord > DM User ID

**Channel IDs**:
- Enable Developer Mode in Discord (Settings > Advanced)
- Right-click a channel > Copy Channel ID
- Add channels in the UI via Discord Feed > Options > Channels section

### Config Reference

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `check_interval_seconds` | int | 300 | Base interval between TCGPlayer/Comics checks |
| `data_dir` | string/null | null | Custom directory for all data files. Null = project root |
| `stealth.jitter_pct` | int | 20 | Random jitter percentage added to intervals |
| `stealth.headless` | bool | true | Run browsers in headless mode |
| `stealth.user_agent` | string/null | null | Custom User-Agent. Null = Chrome 124 default |
| `stealth.browser_channel` | string/null | null | Browser: "chrome", "msedge", or null for auto |
| `stealth.page_timeout_ms` | int | 30000 | Browser page load timeout (ms) |
| `stealth.network_timeout_seconds` | int | 15 | HTTP request timeout (seconds) |
| `discord.enabled` | bool | false | Enable Discord channel monitoring |
| `discord.token` | string | "" | Your Discord user token |
| `discord.bot_token` | string/null | null | Bot token for DM notifications |
| `discord.dm_user_id` | string/null | null | Your Discord User ID for DM notifications |
| `discord.poll_interval_seconds` | int | 30 | Discord polling interval (10-120 seconds) |
| `discord.channels_to_monitor` | array | [] | Discord channel IDs to monitor |
| `discord.keywords` | array | [] | Manual keywords (auto-keywords from products) |
| `discord.min_price` | number | 0 | Minimum price filter (0 = no filter) |
| `discord.ignored_patterns` | array | [] | Substrings to auto-filter |
| `schedule.enabled` | bool | false | Enable active-hours schedule |
| `schedule.start` | string | "07:00" | Active hours start (HH:MM) |
| `schedule.end` | string | "23:00" | Active hours end (HH:MM) |

## Usage

```bash
# Start the web UI (opens browser automatically)
python main.py

# Custom port
python main.py --port 9000

# Don't auto-open browser
python main.py --no-browser

# Bind to all interfaces (for network access)
python main.py --host 0.0.0.0
```

Open `http://localhost:8888` in your browser.

## Data Files

All stored in the project root (or `data_dir` if configured):

| File | Purpose | Git-ignored |
|------|---------|-------------|
| `config.json` | Your configuration with secrets | Yes |
| `price_history.db` | SQLite database (price checks, Discord log, retailer intelligence) | Yes |
| `app_state.json` | Current product state | Yes |
| `tracked_deals.json` | Aggregated Discord deals | Yes |
| `products_hub.json` | Product hub entries | Yes |
| `portfolio.json` | Portfolio items | Yes |
| `web/images/` | Cached product images | Yes |

## Tech Stack

- **Backend**: Python 3.10+, FastAPI, uvicorn, aiosqlite
- **Frontend**: Single HTML file with embedded CSS/JS, Chart.js
- **Browser Automation**: Patchright (CDP-stealth Playwright fork)
- **HTTP Client**: curl-cffi (TLS fingerprint matching)
- **Real-time**: WebSocket for live updates
- **Database**: SQLite (price_history, tcg_history, ebay_sold, discord_log, retailer_sightings)

## Architecture

```
mtg-monitor/
  main.py              # App launcher
  db.py                # SQLite database layer
  config.example.json  # Template config
  requirements.txt     # Python dependencies
  web/
    app.py             # FastAPI backend (~1800 lines)
    state.py           # Shared state, data models, deal tracker, product hub
    index.html         # Entire frontend (~6000 lines)
  monitors/
    tcgplayer_monitor.py  # TCGPlayer price + listing scraper
    ebay_monitor.py       # eBay/130point sold listing scraper
    discord_monitor.py    # Discord REST API poller + DM sender
    defaults.py           # Configurable defaults (UA, timeouts, browser)
    walmart_monitor.py    # (Legacy) Walmart monitor
    amazon_monitor.py     # (Legacy) Amazon monitor
    target_monitor.py     # (Legacy) Target monitor
    bestbuy_monitor.py    # (Legacy) Best Buy monitor
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT License. See [LICENSE](LICENSE).

## Disclaimer

This tool is for personal price tracking and market research. Not affiliated with TCGPlayer, eBay, Discord, Wizards of the Coast, The Pokemon Company, or any retailer. Use responsibly and respect rate limits.
