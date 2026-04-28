# The Stone of Osgiliath

*That Which Looks Far Away*

A real-time price monitoring, deal tracking, and collection management tool for trading card games and collectibles. Monitors Discord deal channels, tracks TCGPlayer and ManaPool prices alongside eBay sold comps, aggregates deals across retailers, and sends push notifications when something interesting hits.

Built for Magic: The Gathering, Pokemon, Yu-Gi-Oh, Riftbound, Lorcana, and more.

## Features

### Discord Deal Feed
- Monitors multiple Discord channels for deal alerts in real-time via browser-based DOM scraping (no bot required)
- **Game detection** — auto-identifies MTG, Pokemon, Riftbound, Yu-Gi-Oh, Star Wars, Lorcana, FAB, One Piece, Digimon, Dragon Ball
- **Retailer extraction** — parses Amazon, Walmart, Target, Best Buy, TCGPlayer, GameStop from embed data and URLs
- **Deal scoring** — compares Discord deal prices against your TCGPlayer market data ("27% below market")
- **Checkout link extraction** — captures ATC/add-to-cart links from Moonitor, Zephr, Refract and surfaces them on deal cards
- **Blocked retailers** — filter out specific domains (e.g. amazon.ca)
- **Multi-game filter** — checkbox dropdown to show any combination of games
- **Channel management** — add/remove monitored channels from the UI with auto-resolved channel names
- **Keyword management** — manual + auto-generated keywords, ignore patterns, min price filter
- **Audit log** — search all messages (shown and filtered) with reason tracking
- **DM notifications** — push notifications via Discord bot DM when deals match your watchlist

### TCGPlayer Price Tracking
- Monitors market prices, lowest listings, and seller quantities for singles and sealed product
- **Full listing data** — captures every seller's price, shipping cost, and quantity via API pagination
- **Price includes shipping** — all prices reflect true cost (price + shipping)
- **Empty box filter** — automatically removes listings for empty boxes/packaging only
- **Price distribution histograms** — IQR-based outlier pruning removes fraudulent/erroneous sales from stats and charts
- **Historical snapshots** — stores full listing distributions over time with playback slider
- **TCGPlayer sales data** — captures daily price history buckets (90 days) and recent individual sales
- **eBay sold comps** — searches eBay completed listings for side-by-side market comparison
- **Trend indicators** — price trend arrows (vs 7-day average) and supply trend indicators on cards
- **Card and Table views** — toggle between full cards and compact table with drag-and-drop reordering

### ManaPool Integration
- Fetches current market price, lowest active listing, and available quantity for singles and sealed via the ManaPool API
- **Condition + finish filters** — specify `?conditions=NM,LP&finish=foil` on any ManaPool URL
- **Reference line** — MP low price shown as a dashed line on the sold transactions chart
- **Stats in histogram modal** — MP LOW / MP MKT / MP QTY shown alongside TCGPlayer data
- **30-minute bulk cache** — a single API token covers all monitored products; cache refreshes every 30 min
- Requires a ManaPool API token (set in Settings or `config.json`)

### Graded Collectibles
- Monitors eBay/130point.com sold listings for graded items (comics, Pokemon cards, baseball cards, etc.)
- Grade breakdowns: CGC, CBCS, PSA, Raw
- Sales review modal with grade classification and ignore/learn system
- Facsimile edition auto-filtering
- Configurable check interval (default 7 days)
- Card and Table views with drag-and-drop reordering

### Watchlist (Product Hub)
- Centralized registry for products you're watching, independent of the TCGPlayer monitor
- Link TCGPlayer items, retailer URLs, and Discord deals to a single watchlist entry
- Compact card layout with image, tags, and retailer links
- Assign/unassign deals, add/remove retailer URLs from the UI

### Deal Aggregation
- Auto-groups Discord messages about the same product using Jaccard similarity matching
- Cards and Table views with deal scoring and checkout links
- Tag, merge, dismiss, or assign deals to Watchlist items

### Retailer Intelligence
- Automatically built from Discord feed data — no manual setup
- **Retailer universe** — all known retailers with sighting counts, price ranges, games carried
- **Product × Retailer matrix** — which retailers carry each product with price comparison
- **Restock timing patterns** — hour-of-day and day-of-week heatmaps per retailer
- **Toggle absolute/percentage** view on timing heatmaps
- **Product filter** — drill down by specific game

### Reddit Feed
- Configurable subreddits (e.g. sealedmtgdeals, pokemontcg, baseballcards)
- Multiple subreddits supported, managed via Settings
- Stale-feed guard: skips posts more than 1 hour older than the newest known post (protects against Reddit API glitches that resurface 30-day-old content)
- Configurable poll interval

### AI Research Agent
- Scheduled weekly (or on-demand) research runs using the Anthropic Claude API
- Analyzes your price history, eBay sold comps, and Discord deal data to surface trends
- Results shown in the Research tab

### Portfolio
- Track collection purchases with cost basis
- Market values from linked TCGPlayer data

---

## Tabs

| Tab | Purpose |
|-----|---------|
| **Watchlist** | Products you're tracking — link TCGPlayer data, retailer URLs, and Discord deals |
| **Deals** | Auto-aggregated deal alerts from Discord, with scoring and checkout links |
| **TCGPlayer** | Singles and sealed product monitoring with histograms, trend data, and ManaPool comparison |
| **Graded** | Graded collectibles (comics, cards) with eBay sold comps and grade breakdowns |
| **Portfolio** | Collection tracking with purchase prices and market values |
| **Intelligence** | Retailer universe, product × retailer matrix, and restock timing heatmaps |
| **Research** | AI-generated research reports on your monitored products |

---

## Requirements

- **Python 3.10+** (uses `dict | None` type hint syntax)
- **Chrome or Chromium browser** (for TCGPlayer, eBay, and Discord scraping via Patchright/Playwright)
- **Discord account** (email + password — the app opens Discord in a browser window)
- **Discord bot token** *(optional)* — for DM push notifications
- **ManaPool API token** *(optional)* — for ManaPool price data
- **Anthropic API key** *(optional)* — for the AI research agent

---

## Quick Start (Windows)

1. Install [Python 3.10+](https://www.python.org/downloads/) — **check "Add Python to PATH"** during installation
2. Download or clone this repo:
   ```
   git clone https://github.com/wsnich/stone-of-osgiliath.git
   ```
3. Double-click **`setup.bat`** — installs all Python dependencies and the Chromium browser automatically
4. Double-click **`start.bat`** to launch the app
5. Open **http://localhost:8888** in your browser
6. The first-run setup wizard will guide you through Discord login and basic configuration

## Installation (Manual / Mac / Linux)

```bash
git clone https://github.com/wsnich/stone-of-osgiliath.git
cd stone-of-osgiliath

# Install Python dependencies
pip install -r requirements.txt

# Install headless Chromium for Patchright
python -m patchright install chromium
```

If `patchright` browser install fails, Playwright works as a fallback:
```bash
pip install playwright
python -m playwright install chromium
```

---

## Configuration

Copy the example config before editing:
```bash
cp config.example.json config.json
```

Then edit `config.json`, or use the **in-app Settings panel** (gear icon) — most options are configurable from the UI without touching the file.

### Discord Setup

**Browser Login** (required for channel monitoring):
1. Start the app — a Chromium window will open at Discord's login page
2. Log in with your Discord email, password, and 2FA if enabled
3. The session is saved automatically; you won't need to log in again unless Discord expires it
4. The app opens one browser tab per monitored channel and watches for new messages in real-time

**Bot Token** *(optional, for DM push notifications)*:
1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. Create a New Application → Bot → Reset Token
3. Paste the token into Settings → Discord → Bot Token
4. Invite the bot to a server you share with your main account
5. Enter your User ID in Settings → Discord → DM User ID

**Channel IDs**:
- Right-click a channel in Discord → Copy Message Link
- Use the full URL: `https://discord.com/channels/SERVER_ID/CHANNEL_ID`
- Or the short form: `SERVER_ID/CHANNEL_ID`
- Add/remove channels from Settings or directly in `config.json`

### ManaPool Setup

1. Create an account at [manapool.com](https://manapool.com) and generate an API token from your profile
2. Paste the token into Settings → ManaPool API Token (or set `manapool_api_token` in `config.json`)
3. Add ManaPool URLs to TCGPlayer products in the format:
   ```
   https://manapool.com/card/{set}/{collector-number}/{slug}?conditions=NM,LP&finish=foil
   ```
   - `conditions` — comma-separated list: `NM`, `LP`, `MP`, `HP`, `DMG`
   - `finish` — `foil` or `nonfoil`

### Research Agent Setup

1. Get an [Anthropic API key](https://console.anthropic.com/)
2. Set it in Settings → Research → API Key (or via the `ANTHROPIC_API_KEY` environment variable)
3. Enable scheduled research in Settings → Research → Enabled

---

## Config Reference

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `check_interval_seconds` | int | 300 | Global interval between TCGPlayer/retailer checks |
| `graded_interval_seconds` | int | 604800 | Graded item check interval (default 7 days) |
| `data_dir` | string/null | null | Custom directory for all data files. Null = project root |
| `manapool_api_token` | string | "" | ManaPool API bearer token |
| `stealth.jitter_pct` | int | 20 | Random jitter % added to intervals to avoid patterns |
| `stealth.headless` | bool | true | Run browsers headless |
| `stealth.user_agent` | string/null | null | Custom User-Agent. Null = Chrome 124 default |
| `stealth.browser_channel` | string/null | null | Browser: `"chrome"`, `"msedge"`, or null for bundled Chromium |
| `stealth.page_timeout_ms` | int | 30000 | Browser page load timeout (ms) |
| `stealth.network_timeout_seconds` | int | 15 | HTTP request timeout (seconds) |
| `discord.enabled` | bool | false | Enable Discord channel monitoring |
| `discord.email` | string | "" | Discord login email |
| `discord.password` | string | "" | Discord login password |
| `discord.bot_token` | string/null | null | Bot token for DM notifications |
| `discord.dm_user_id` | string/null | null | Your Discord User ID for DM notifications |
| `discord.channels_to_monitor` | array | [] | Channel URLs or `server_id/channel_id` pairs |
| `discord.keywords` | array | [] | Manual keywords (auto-keywords generated from products) |
| `discord.min_price` | number | 0 | Minimum price filter (0 = no filter) |
| `discord.ignored_patterns` | array | [] | Substrings that cause a message to be filtered |
| `discord.blocked_retailers` | array | [] | Domain patterns to block (e.g. `"amazon.ca"`) |
| `reddit.subreddits` | array | ["sealedmtgdeals"] | Subreddits to monitor |
| `reddit.poll_interval_seconds` | int | 60 | Reddit polling interval |
| `schedule.enabled` | bool | false | Enable active-hours schedule |
| `schedule.start` | string | "07:00" | Active hours start (HH:MM) |
| `schedule.end` | string | "23:00" | Active hours end (HH:MM) |
| `research.enabled` | bool | false | Enable scheduled AI research agent |
| `research.interval_hours` | int | 168 | Hours between research runs (default weekly) |

---

## Usage

```bash
# Start the app (opens browser automatically)
python main.py

# Custom port
python main.py --port 9000

# Don't auto-open browser
python main.py --no-browser

# Bind to all interfaces (for network access from other devices)
python main.py --host 0.0.0.0
```

---

## Data Files

All stored in the project root (or `data_dir` if configured). All are git-ignored.

| File | Purpose |
|------|---------|
| `config.json` | Your configuration (includes secrets — keep private) |
| `price_history.db` | SQLite: price snapshots, TCGPlayer history, eBay sold, Discord audit log, retailer intelligence |
| `app_state.json` | Current product state (prices, availability, sales cache) |
| `tracked_deals.json` | Aggregated Discord deals |
| `products_hub.json` | Watchlist entries |
| `portfolio.json` | Portfolio items |
| `web/images/` | Cached product images |

---

## Tech Stack

- **Backend**: Python 3.10+, FastAPI, uvicorn, aiosqlite
- **Frontend**: Single HTML file with embedded CSS/JS, Chart.js
- **Browser Automation**: Patchright (CDP-stealth Playwright fork)
- **HTTP Client**: curl-cffi (TLS fingerprint matching), aiohttp
- **Real-time**: WebSocket for live UI updates
- **Database**: SQLite via aiosqlite
- **AI**: Anthropic Claude API (research agent)

---

## Architecture

```
stone-of-osgiliath/
  main.py                   # App launcher (uvicorn + browser open)
  db.py                     # SQLite database layer
  config.example.json       # Template config (copy to config.json)
  requirements.txt          # Python dependencies
  setup.bat                 # Windows: one-click dependency install
  start.bat                 # Windows: one-click launcher
  web/
    app.py                  # FastAPI backend — REST API, WebSocket, monitor loops
    state.py                # Shared state, data models, deal tracker, product hub
    index.html              # Entire frontend (single file — HTML + CSS + JS)
  monitors/
    tcgplayer_monitor.py    # TCGPlayer price + listing scraper
    ebay_monitor.py         # eBay/130point sold listing scraper
    manapool_monitor.py     # ManaPool API bulk price + quantity fetcher
    discord_gateway.py      # Discord browser-based DOM scraper (one tab per channel)
    discord_monitor.py      # Discord message filtering + DM sender
    defaults.py             # Configurable defaults (UA, timeouts, browser)
  research/
    agent.py                # AI research agent (Anthropic SDK)
    tools.py                # Agent tool definitions
    queries.py              # Read-only DB access for agent
    prompts/                # System + weekly research prompts
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT License. See [LICENSE](LICENSE).

## Disclaimer

This tool is for personal price tracking and market research. Not affiliated with TCGPlayer, ManaPool, eBay, Discord, Wizards of the Coast, The Pokemon Company, or any retailer. Use responsibly and respect rate limits.
