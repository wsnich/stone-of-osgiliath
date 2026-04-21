# The Stone of Osgiliath

*That Which Looks Far Away*

A real-time price monitoring, deal tracking, and collection management tool for trading card games and collectibles. Monitors Discord deal channels and marketplace (BST) channels, tracks TCGPlayer and eBay prices, aggregates deals across retailers, and sends you push notifications when something interesting hits.

Built for Magic: The Gathering, Pokemon, Yu-Gi-Oh, Riftbound, Lorcana, and more.

## Features

### Discord Deal Feed
- Monitors multiple Discord channels for deal alerts in real-time
- **Game detection** — auto-identifies MTG, Pokemon, Riftbound, Yu-Gi-Oh, Star Wars, Lorcana, FAB, One Piece, Digimon, Dragon Ball
- **Retailer extraction** — parses Amazon, Walmart, Target, Best Buy, TCGPlayer, GameStop from embed data and URLs
- **Deal scoring** — compares Discord deal prices against your TCGPlayer market data ("27% below market")
- **Checkout link extraction** — captures ATC/add-to-cart links from Moonitor, Zephr, Refract and surfaces them on deals
- **Blocked retailers** — filter out specific domains (e.g. amazon.ca)
- **Multi-game filter** — checkbox dropdown to show any combination of games
- **Channel management** — add/remove monitored channels from the UI with auto-resolved channel names
- **Keyword management** — manual + auto-generated keywords, ignore patterns, min price filter
- **Audit log** — search all messages (shown and filtered) with reason tracking
- **DM notifications** — get push notifications on your phone via Discord bot DM when deals are found

### Discord Marketplace (BST) Monitoring
- Monitors Buy/Sell/Trade channels for peer-to-peer listings
- **Auto-parses** free-text messages to extract items, prices, and quantities
- **WTS/WTB detection** — identifies selling vs buying intent
- **Auto-matches** against your tracked TCGPlayer products
- **Deal alerts** — DM notification when a seller's price is 15%+ below TCG market
- **Jump to message** — direct link to claim deals in Discord

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

### Graded Collectibles
- Monitors eBay/130point.com sold listings for graded items (comics, Pokemon cards, baseball cards, etc.)
- Grade breakdowns: CGC, CBCS, PSA, Raw
- Sales review modal with grade classification and ignore/learn system
- Facsimile edition auto-filtering
- Configurable check interval (default 7 days)
- Card and Table views with drag-and-drop reordering

### Watchlist (Product Hub)
- Centralized registry linking TCGPlayer data, retailer URLs, and Discord deals for products you're tracking
- Auto-created when adding TCGPlayer items
- Compact card layout with image, price data, retailer links, and deal chips
- Assign/unassign deals, link TCGPlayer items, add retailer URLs

### Deal Aggregation
- Auto-groups Discord messages about the same product using Jaccard similarity matching
- Cards and Table views with deal scoring
- Tag, merge, dismiss, or assign deals to Watchlist items
- Checkout links surfaced on deal cards

### Retailer Intelligence
- Automatically built from Discord feed data
- **Retailer universe** — all known retailers with sighting counts, price ranges, games carried
- **Product x Retailer matrix** — which retailers carry each product with price comparison
- **Restock timing patterns** — hour-of-day and day-of-week heatmaps per retailer
- **Toggle absolute/percentage** view on timing heatmaps
- **Product filter** — drill down by specific game
- Continuously learns from new Discord messages

### Reddit Feed
- Configurable subreddits (e.g. sealedmtgdeals, pokemontcg, baseballcards)
- Multiple subreddits supported, managed via Settings
- Configurable poll interval

### Portfolio
- Track collection purchases with cost basis
- Market values from linked TCGPlayer data

### Settings
- All configuration editable from the UI (gear icon)
- Monitor interval, browser settings, timeouts, user agent
- Discord token management, bot token for DM notifications
- Marketplace BST channel configuration
- Reddit subreddit management
- Graded check interval
- Data directory for custom file locations
- **First-run setup wizard** — guided setup for new users

## Tabs

| Tab | Purpose |
|-----|---------|
| **Watchlist** | Central hub linking TCGPlayer data, retailer URLs, and deals for tracked products |
| **Deals** | Auto-aggregated deal alerts from Discord, with scoring and checkout links |
| **TCGPlayer** | Singles and sealed product monitoring with histograms and trend data |
| **Graded** | Graded collectibles (comics, cards) with eBay sold comps and grade breakdowns |
| **Portfolio** | Collection tracking with purchase prices |
| **Intelligence** | Retailer universe, product matrix, and restock timing patterns |

## Requirements

- **Python 3.10+** (uses `dict | None` type hint syntax)
- **Chrome or Chromium browser** (for TCGPlayer, eBay scraping, and Discord monitoring via Patchright/Playwright)
- **Discord account** (email/password — the app opens Discord in a browser window)
- **Discord bot token** (optional, for DM push notifications)

## Quick Start (Windows)

1. Install [Python 3.10+](https://www.python.org/downloads/) — **check "Add Python to PATH"** during installation
2. Download this repo (Code → Download ZIP) and extract it, or `git clone https://github.com/wsnich/stone-of-osgiliath.git`
3. Double-click **`setup.bat`** — installs all dependencies automatically
4. Edit `config.json` with your Discord email/password (see Discord Setup below), or use the in-app setup wizard
5. Double-click **`start.bat`** to run the app
6. Open **http://localhost:8888** in your browser

## Installation (Manual / Mac / Linux)

```bash
# Clone the repository
git clone https://github.com/wsnich/stone-of-osgiliath.git
cd stone-of-osgiliath

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

2. Edit `config.json` with your settings, or use the first-run setup wizard in the browser

### Discord Setup

**Browser Login** (required for monitoring channels):
1. Start the app — a Chrome window will open showing Discord's login page
2. Log in with your Discord email, password, and 2FA if enabled
3. Your session is saved automatically for future restarts
4. The app opens a browser tab per monitored channel and watches for new messages in real-time

**Bot Token** (optional, for DM notifications):
1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Create a New Application > Bot > Reset Token
3. Copy the token into Settings > Discord > Bot Token
4. Invite the bot to a server you share with your main account
5. Enter your User ID in Settings > Discord > DM User ID

**Channel IDs**:
- Right-click a channel in Discord > Copy Message Link (or Copy Link)
- Use the full URL format: `https://discord.com/channels/SERVER_ID/CHANNEL_ID`
- Or use `SERVER_ID/CHANNEL_ID` format
- Add channels in Settings or directly in `config.json`

### Config Reference

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `check_interval_seconds` | int | 300 | Base interval between TCGPlayer checks |
| `graded_interval_seconds` | int | 604800 | Graded item check interval (default 7 days) |
| `data_dir` | string/null | null | Custom directory for all data files. Null = project root |
| `stealth.jitter_pct` | int | 20 | Random jitter percentage added to intervals |
| `stealth.headless` | bool | true | Run browsers in headless mode |
| `stealth.user_agent` | string/null | null | Custom User-Agent. Null = Chrome 124 default |
| `stealth.browser_channel` | string/null | null | Browser: "chrome", "msedge", or null for auto |
| `stealth.page_timeout_ms` | int | 30000 | Browser page load timeout (ms) |
| `stealth.network_timeout_seconds` | int | 15 | HTTP request timeout (seconds) |
| `discord.enabled` | bool | false | Enable Discord channel monitoring |
| `discord.email` | string | "" | Discord login email (for browser monitoring) |
| `discord.password` | string | "" | Discord login password |
| `discord.bot_token` | string/null | null | Bot token for DM notifications |
| `discord.dm_user_id` | string/null | null | Your Discord User ID for DM notifications |
| `discord.channels_to_monitor` | array | [] | Channel URLs or server_id/channel_id pairs |
| `discord.keywords` | array | [] | Manual keywords (auto-keywords from products) |
| `discord.min_price` | number | 0 | Minimum price filter (0 = no filter) |
| `discord.ignored_patterns` | array | [] | Substrings to auto-filter |
| `discord.blocked_retailers` | array | [] | Domain patterns to block (e.g. "amazon.ca") |
| `marketplace.enabled` | bool | false | Enable BST channel monitoring |
| `marketplace.sell_channels` | array | [] | Discord selling channel IDs |
| `marketplace.buy_channels` | array | [] | Discord buying channel IDs |
| `reddit.subreddits` | array | ["sealedmtgdeals"] | Subreddits to monitor |
| `reddit.poll_interval_seconds` | int | 60 | Reddit polling interval |
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
| `price_history.db` | SQLite database (price checks, Discord log, retailer intelligence, marketplace) | Yes |
| `app_state.json` | Current product state | Yes |
| `tracked_deals.json` | Aggregated Discord deals | Yes |
| `products_hub.json` | Watchlist entries | Yes |
| `portfolio.json` | Portfolio items | Yes |
| `web/images/` | Cached product images | Yes |

## Tech Stack

- **Backend**: Python 3.10+, FastAPI, uvicorn, aiosqlite
- **Frontend**: Single HTML file with embedded CSS/JS, Chart.js
- **Browser Automation**: Patchright (CDP-stealth Playwright fork)
- **HTTP Client**: curl-cffi (TLS fingerprint matching)
- **Real-time**: WebSocket for live updates
- **Database**: SQLite (price_history, tcg_history, ebay_sold, discord_log, retailer_sightings, marketplace_messages)

## Architecture

```
stone-of-osgiliath/
  main.py                # App launcher
  db.py                  # SQLite database layer
  config.example.json    # Template config
  requirements.txt       # Python dependencies
  setup.bat              # Windows one-click setup
  start.bat              # Windows one-click launcher
  web/
    app.py               # FastAPI backend
    state.py             # Shared state, data models, deal tracker, product hub
    index.html           # Entire frontend (single file)
  monitors/
    tcgplayer_monitor.py    # TCGPlayer price + listing scraper
    ebay_monitor.py         # eBay/130point sold listing scraper
    discord_gateway.py      # Discord browser-based DOM scraper (one tab per channel)
    discord_monitor.py      # Discord message filtering + DM sender
    marketplace_monitor.py  # BST channel parser + product matcher
    defaults.py             # Configurable defaults (UA, timeouts, browser)
  research/
    agent.py                # AI research agent (Anthropic SDK)
    tools.py                # Agent tool definitions
    queries.py              # Read-only DB access for agent
    prompts/                # System + weekly research prompts
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT License. See [LICENSE](LICENSE).

## Disclaimer

This tool is for personal price tracking and market research. Not affiliated with TCGPlayer, eBay, Discord, Wizards of the Coast, The Pokemon Company, or any retailer. Use responsibly and respect rate limits.
